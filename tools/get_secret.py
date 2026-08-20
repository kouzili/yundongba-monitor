#!/usr/bin/env python3
"""一次性取出 secret_api，并顺带判定「查排期是否需要登录态」。

思路：签名密钥是小程序里的一个字符串常量。解包后把所有像密钥的字面量抽出来当
候选，逐个代入 sign_bymd5 打一次真实的查排期接口，用服务端返回当预言机。

已实测的服务端行为（这是分类规则的依据）：
  · 签名错误            → {"returnCode":"-1","returnMsg":"签名错误"}
  · 缺 sign 字段        → {"returnCode":"-1","returnMsg":"参数[sign]不能为空"}
  · 带假 appsessionid 与完全不带，返回一模一样 —— 登录态不在签名之前校验

所以只有三种结果：签名类错误 = 候选不对；returnCode 0 = 候选对且不需要登录；
其它错误 = 候选对但需要登录。
"""
import argparse
import json
import re
import sys
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests                      # noqa: E402

import reverse_wxapkg                # noqa: E402
import sign                          # noqa: E402

# 候选判定结果
HIT_NO_SESSION = "hit_no_session"
WRONG_SECRET = "wrong_secret"
HIT_NEEDS_SESSION = "hit_needs_session"

# 密钥候选：引号包裹的纯字母数字下划线短横线串
CANDIDATE_RE = re.compile(r"""["']([A-Za-z0-9_\-]{8,64})["']""")

# 签名相关锚点 —— 离这些越近越可能是密钥
ANCHORS = ("&key=", "key=", "signBymd5")

# 用来从一堆小程序包里认出韵动吧
TARGET_MARKER = "sports8"

DEFAULT_LIMIT = 300
DEFAULT_PAUSE = 0.5

ProbeResult = namedtuple("ProbeResult", "secret verdict tried")


def extract_candidates(js_texts: dict, limit: int = DEFAULT_LIMIT) -> tuple:
    """从 {文件名: 源码} 里抽密钥候选，按「离签名锚点的距离」升序。

    返回 (候选列表, 被丢弃的数量)。绝不静默截断 —— 丢了多少必须让调用方知道。
    """
    best = {}   # 候选 -> (最小距离, 首次出现的全局序号)
    order = 0
    for name in sorted(js_texts):
        text = js_texts[name]
        anchor_positions = [m.start() for a in ANCHORS
                            for m in re.finditer(re.escape(a), text)]
        for m in CANDIDATE_RE.finditer(text):
            value = m.group(1)
            distance = min((abs(m.start() - p) for p in anchor_positions),
                           default=float("inf"))
            order += 1
            if value not in best or distance < best[value][0]:
                previous_order = best[value][1] if value in best else order
                best[value] = (distance, previous_order)

    ranked = sorted(best.items(), key=lambda kv: (kv[1][0], kv[1][1]))
    candidates = [value for value, _ in ranked]
    if len(candidates) <= limit:
        return candidates, 0
    return candidates[:limit], len(candidates) - limit


# 微信 appid: wx + 16 位十六进制
APPID_RE = re.compile(r"^wx[0-9a-f]{16}$")


def guess_appid(path) -> str:
    """从缓存路径里推断 appid。

    微信桌面端的布局是 .../Applet/<appid>/<版本>/__APP__.wxapkg，appid 就写在
    路径里 —— 而解密 V1MMWX 恰好需要它。安卓上的包不加密，推不出来也无所谓。
    """
    for part in Path(path).parts:
        if APPID_RE.match(part):
            return part
    return None


def parse_adb_dirs(output: str) -> list:
    """从 adb shell 的输出里挑出真实存在的目录路径。

    adb shell 走 pty，行尾是 CRLF；目录不存在时可能吐 ls 的报错，也可能把
    通配符原样返回 —— 两种都不能当成路径。
    """
    dirs = []
    for line in output.replace("\r", "").splitlines():
        line = line.strip()
        if not line or not line.startswith("/") or "*" in line:
            continue
        dirs.append(line)
    return dirs


def is_target_package(js_texts: dict) -> bool:
    """判断这个包是不是韵动吧。

    手机上 appbrand/pkg/ 里放着你用过的所有小程序的包，文件名是一串数字，
    肉眼分不出来。认 API 域名最省事 —— 只有韵动吧的包会引用它。
    """
    return any(TARGET_MARKER in text for text in js_texts.values())


def merge_target_groups(entries: list) -> tuple:
    """entries: [(分组键, 包名, js_texts)] -> (合并后的 js_texts, 命中的包名列表)

    分组键就是 appid。同一个 appid 下只要有一个包引用了 API 域名，整组都收 ——
    小程序常把主包和分包拆开，域名写在主包、签名代码放在分包是常见情况，
    只按单个包筛会把签名代码丢掉。
    """
    hit_groups = {key for key, _, texts in entries if is_target_package(texts)}
    merged, matched = {}, []
    for index, (key, name, texts) in enumerate(entries):
        if key not in hit_groups:
            continue
        matched.append(name)
        for js_name, text in texts.items():
            # 前缀带上序号：不同包里同名的 app.js 不能互相覆盖
            merged[f"{key}/{index}/{js_name}"] = text
    return merged, matched


def classify(data: dict) -> str:
    """两套信封都要认：/api/ 用 returnCode/returnMsg，YDBCLUB/ 用 result_code/result_msg。

    只认一套会把 YDBCLUB 的「签名错误」误判成「密钥对但需要登录」—— 结论整个反过来。
    """
    code = data.get("returnCode", data.get("result_code"))
    message = str(data.get("returnMsg") or data.get("result_msg") or "")
    if str(code) == "0":
        return HIT_NO_SESSION
    if "签名" in message or "sign" in message.lower():
        return WRONG_SECRET
    return HIT_NEEDS_SESSION


def probe_candidates(candidates: list, probe, pause: float = DEFAULT_PAUSE) -> ProbeResult:
    """逐个候选打接口，命中即停。probe 抛异常时跳过该候选继续。"""
    tried = 0
    for index, candidate in enumerate(candidates):
        if index and pause:
            time.sleep(pause)
        tried += 1
        try:
            data = probe(candidate)
        except Exception:
            continue
        verdict = classify(data)
        if verdict != WRONG_SECRET:
            return ProbeResult(candidate, verdict, tried)
    return ProbeResult(None, None, tried)


# ============================================================
#  以下是接线部分：取包 / 解包 / 打真实接口 / 写配置
# ============================================================

SCHEDULE_ENDPOINT = "/api/ydb/stadium/apiGetStadiumShedule"
UPLOAD_PORT = 5101

UPLOAD_PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>上传 wxapkg</title>
<style>body{font-family:-apple-system,sans-serif;padding:24px;line-height:1.7}
code{background:#f4f4f5;padding:2px 5px;border-radius:4px;font-size:13px}
input,button{font-size:16px;margin-top:12px}</style>
<h3>上传韵动吧小程序包</h3>
<p>手机文件管理器里进：<br><code>Android/data/com.tencent.mm/MicroMsg/&lt;一串字母数字&gt;/appbrand/pkg/</code></p>
<p>分不清哪个是韵动吧就<b>全选</b>传上来，这边会自己认。</p>
<form method=post enctype=multipart/form-data>
  <input type=file name=pkg multiple required><br><button>上传</button>
</form>
"""


def receive_packages(save_dir: Path, host="0.0.0.0", port=UPLOAD_PORT) -> list:
    """起一个临时上传页，收到文件就关掉自己，返回保存路径列表。

    这里刻意绑 0.0.0.0 —— 手机必须能访问到。生命周期只有一次上传。
    """
    from flask import Flask, request
    from werkzeug.serving import make_server

    save_dir.mkdir(parents=True, exist_ok=True)
    received = []
    done = threading.Event()

    uploader = Flask(__name__)

    @uploader.route("/", methods=["GET"])
    def form():
        return UPLOAD_PAGE

    @uploader.route("/", methods=["POST"])
    def upload():
        files = [f for f in request.files.getlist("pkg") if f and f.filename]
        if not files:
            return "没收到文件", 400
        for f in files:
            # 文件名来自手机，只取 basename，不让它决定写到哪
            target = save_dir / Path(f.filename).name
            f.save(target)
            received.append(target)
        done.set()
        return f"收到 {len(files)} 个文件，可以关掉这个页面了"

    server = make_server(host, port, uploader)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"  等待上传… 手机浏览器打开 http://<这台机器的局域网IP>:{port}")
    print("  （Ctrl-C 取消）")
    try:
        done.wait()
    finally:
        server.shutdown()
    return received


WECHAT_PKG_GLOB = "/sdcard/Android/data/com.tencent.mm/MicroMsg/*/appbrand/pkg"

# 自带的官方 platform-tools。Debian 源里的 adb 是 29.x，没有 `adb pair`，
# 无线调试配不上，所以优先用这一份。
BUNDLED_ADB = Path(__file__).resolve().parent / "android" / "platform-tools" / "adb"


def resolve_adb(explicit, bundled: Path = BUNDLED_ADB) -> str:
    """选用哪个 adb：显式指定 > 自带的官方版 > PATH 里的。"""
    if explicit:
        return explicit
    if Path(bundled).exists():
        return str(bundled)
    return "adb"


def pull_via_adb(dest: Path, adb: str = "adb") -> list:
    """用 adb 把手机上的小程序包目录拉下来。

    adb shell 这个用户不受 scoped storage 限制，所以在安卓 11+ 上也能读
    /Android/data —— 这是第三方文件管理器进不去的那个目录。
    """
    import shutil
    import subprocess

    if not (shutil.which(adb) or Path(adb).exists()):
        raise SystemExit(
            "找不到 adb。下载官方 platform-tools（不需要 root）：\n"
            "  curl -sL -o /tmp/pt.zip "
            "https://dl.google.com/android/repository/platform-tools-latest-linux.zip\n"
            "  unzip -oq /tmp/pt.zip -d tools/android")

    devices = subprocess.run([adb, "devices"], capture_output=True, text=True).stdout
    attached = [l for l in devices.splitlines()[1:] if l.strip() and "\tdevice" in l]
    if not attached:
        raise SystemExit(
            "adb 没看到已授权的设备。\n"
            "\n"
            "无线调试（手机和本机同局域网时用这个，不用插线）：\n"
            "  手机 开发者选项 → 无线调试 → 打开 → 「使用配对码配对设备」\n"
            "  会显示一个 IP:端口 和 6 位配对码，然后在本机：\n"
            f"    {adb} pair <IP>:<配对端口> <配对码>\n"
            f"    {adb} connect <IP>:<调试端口>   ← 注意这个端口在无线调试主页面，"
            "和配对端口不是同一个\n"
            "\n"
            "USB：开发者选项打开「USB 调试」，插线后在手机弹窗点「允许」\n"
            "（显示 unauthorized 就是这一步没点）\n"
            f"\n当前 adb devices 输出：\n{devices}")
    print(f"  设备: {attached[0].split()[0]}")

    listing = subprocess.run([adb, "shell", f"ls -d {WECHAT_PKG_GLOB}"],
                             capture_output=True, text=True)
    dirs = parse_adb_dirs(listing.stdout)
    if not dirs:
        raise SystemExit(
            "手机上找不到小程序包目录。最常见的原因是微信还没缓存过韵动吧：\n"
            "  先在手机微信里打开韵动吧小程序，点进订场页，再重跑。\n"
            f"adb 输出：{listing.stdout.strip()} {listing.stderr.strip()}")

    dest.mkdir(parents=True, exist_ok=True)
    for i, remote in enumerate(dirs):
        print(f"  拉取 {remote}")
        subprocess.run([adb, "pull", remote, str(dest / f"acct{i}")],
                       capture_output=True, text=True)
    packages = sorted(dest.rglob("*.wxapkg"))
    print(f"  共拿到 {len(packages)} 个 .wxapkg")
    if not packages:
        raise SystemExit("目录拉下来了但里面没有 .wxapkg。"
                         "先在手机上打开韵动吧小程序让微信缓存代码包。")
    return packages


def unpack(pkg_path: Path, outdir: Path, appid: str = "", quiet: bool = False) -> Path:
    raw = pkg_path.read_bytes()
    if reverse_wxapkg.is_encrypted(raw):
        appid = appid or guess_appid(pkg_path)
        if not appid:
            raise SystemExit(
                f"{pkg_path.name} 是微信桌面端的加密包，需要 appid 才能解密。\n"
                "  路径里没有 wx+16位十六进制 的目录名，请用 --appid 手动指定。")
        if not quiet:
            print(f"  加密包，用 appid {appid} 解密")
    data = reverse_wxapkg.decrypt_wxapkg(appid, raw)
    files = reverse_wxapkg.unpack_wxapkg(data, outdir)
    if not quiet:
        print(f"  解包 {len(files)} 个文件 -> {outdir}")
    return outdir


def gather_js(pkg_paths: list, workdir: Path, appid: str = "") -> dict:
    """解包全部候选包，只留下属于韵动吧的 appid 分组，返回合并后的 {文件名: 源码}。"""
    entries, failed = [], []
    for index, pkg in enumerate(pkg_paths):
        group = appid or guess_appid(pkg) or pkg.stem
        try:
            # 解包目录必须唯一 —— 桌面端有大量同名的 __APP__.wxapkg
            unpacked = unpack(pkg, workdir / f"{group}_{index}", appid, quiet=True)
        except Exception as e:
            failed.append(f"{pkg.name}: {e}")
            continue
        entries.append((group, f"{group}/{pkg.name}", read_js_texts(unpacked)))

    print(f"  解包成功 {len(entries)} 个" +
          (f"，失败 {len(failed)} 个" if failed else ""))
    for line in failed[:5]:
        print(f"    跳过 {line}")
    if len(failed) > 5:
        print(f"    …另有 {len(failed) - 5} 个失败")

    merged, matched = merge_target_groups(entries)
    if not matched:
        raise SystemExit(
            f"\n❌ 所有包里都没有引用 {TARGET_MARKER} —— 没找到韵动吧的包。\n"
            "   先在微信里打开韵动吧小程序并点进订场页，让它把代码包缓存下来，\n"
            "   再重新取包。")
    print(f"\n命中 {len(matched)} 个包:")
    for name in matched:
        print(f"    {name}")
    return merged


def read_js_texts(unpacked: Path) -> dict:
    texts = {}
    for path in sorted(unpacked.rglob("*.js")):
        try:
            texts[str(path.relative_to(unpacked))] = path.read_text(
                encoding="utf-8", errors="replace")
        except Exception:
            continue
    return texts


def make_probe(stadiumid: str, date_ts: int, appsessionid: str = ""):
    """返回一个「代入密钥打一次查排期」的函数。"""
    def probe(secret):
        body = {"stadiumid": str(stadiumid), "date": date_ts, "userid": 0,
                "biz": "apiGetStadiumShedule", "method": sign.METHOD,
                "nonce": sign.make_nonce()}
        body["sign"] = sign.sign_bymd5(body, secret)
        r = requests.post(sign.API_BASE + SCHEDULE_ENDPOINT, json=body, timeout=20,
                          headers={"content-type": "application/json",
                                   "appsessionid": appsessionid})
        return r.json()
    return probe


def dump_sign_context(js_texts: dict) -> None:
    print("\n=== signBymd5 / key= 附近的源码 ===")
    for name, text in js_texts.items():
        for anchor in ANCHORS:
            for m in re.finditer(re.escape(anchor), text):
                start, end = max(0, m.start() - 200), m.start() + 200
                print(f"\n[{name} @{m.start()}]\n  ...{text[start:end]}...")
                break


def write_secret(config_file: Path, secret: str) -> None:
    import app  # 复用 DEFAULT_CONFIG，避免两处定义漂移

    cfg = dict(app.DEFAULT_CONFIG)
    if config_file.exists():
        cfg.update(json.loads(config_file.read_text()))
    cfg["secret_api"] = secret
    config_file.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def main():
    root = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(
        description="一次性取出 secret_api，并判定查排期是否需要登录态")
    p.add_argument("--pkg", help=".wxapkg 文件或装着一堆 .wxapkg 的目录；"
                                "不给就起上传页等手机传")
    p.add_argument("--adb", action="store_true",
                   help="用 adb 直接从连着的安卓手机拉包（安卓 11+ 也能用）")
    p.add_argument("--adb-bin", help="adb 可执行文件路径（默认用自带的官方 platform-tools）")
    p.add_argument("--appid", default="", help="仅解密微信桌面端加密包时需要")
    p.add_argument("--port", type=int, default=UPLOAD_PORT, help="上传页端口")
    p.add_argument("--stadium", default="1128", help="用来探测的场馆 id")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="候选上限")
    p.add_argument("--pause", type=float, default=DEFAULT_PAUSE,
                   help="候选之间的间隔秒数，别调太小")
    p.add_argument("--dry-run", action="store_true", help="只列候选，不打接口")
    p.add_argument("--dump-sign-context", action="store_true",
                   help="打印签名相关源码供人工判读")
    args = p.parse_args()

    if args.pkg:
        packages = reverse_wxapkg.collect_packages(args.pkg)
        if not packages:
            raise SystemExit(f"{args.pkg} 下没找到 .wxapkg")
    elif args.adb:
        print("通过 adb 从手机拉取…")
        packages = pull_via_adb(root / "captures_pkg" / "adb", resolve_adb(args.adb_bin))
    else:
        packages = receive_packages(root / "captures_pkg", port=args.port)

    print(f"待处理 {len(packages)} 个包")
    js_texts = gather_js(packages, root / "reverse_unpacked", args.appid)
    print(f"合并后 JS 文件 {len(js_texts)} 个")

    if args.dump_sign_context:
        dump_sign_context(js_texts)
        return

    candidates, dropped = extract_candidates(js_texts, args.limit)
    print(f"\n候选密钥 {len(candidates)} 个" +
          (f"（另有 {dropped} 个超出 --limit 未探测）" if dropped else ""))
    if not candidates:
        raise SystemExit("没抽到候选。试试 --dump-sign-context 人工看看。")

    if args.dry_run:
        for c in candidates[:20]:
            print(f"  {c}")
        print(f"  …共 {len(candidates)} 个（--dry-run 不打接口）")
        return

    date_ts = int((datetime.now() + timedelta(days=1))
                  .replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    print(f"逐个代入并打查排期接口（场馆 {args.stadium}，"
          f"间隔 {args.pause}s，最多 {len(candidates)} 发）…")

    result = probe_candidates(candidates, make_probe(args.stadium, date_ts),
                              args.pause)

    if not result.secret:
        print(f"\n❌ 试了 {result.tried} 个候选都是「签名错误」。")
        print("   密钥可能是拼接或混淆的。跑 --dump-sign-context 人工看一眼。")
        raise SystemExit(1)

    write_secret(root / "config.json", result.secret)
    print(f"\n✅ 找到 secret_api（第 {result.tried} 个候选），已写入 config.json")

    if result.verdict == HIT_NO_SESSION:
        print("✅ 空 appsessionid 就能查排期 —— 不需要登录，也不需要抓包。")
        print("   直接 bash start.sh 开始用。")
    else:
        data = make_probe(args.stadium, date_ts)(result.secret)
        print(f"⚠️ 密钥对了，但查排期需要登录态：{data.get('returnMsg')}")
        print("   需要再补一步拿 appsessionid（登录接口就在刚解包的源码里）。")


if __name__ == "__main__":
    main()
