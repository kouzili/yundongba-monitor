#!/usr/bin/env python3
"""
韵动吧网球场监控 - Web 管理后台（本地签名版，无需抓包）
==========================================================
签名算法已逆向，所有 API 调用本地签名。抓包仅用于刷新会话。
"""
import collections, json, os, re, socket, subprocess, threading, time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request

import sign

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
FLOWS_FILE = Path.home() / "captures/yundongba.flows"
MITMDUMP_BIN = "/opt/homebrew/bin/mitmdump"
ANALYZE_SCRIPT = Path.home() / "captures/analyze2.py"

DEFAULT_CONFIG = {
    "appsessionid": "",
    "userid": 0,
    "stadiums": {},               # {id: name}
    "stadium_priority": [],       # 有序列表，优先级从高到低
    "target_start": 18,
    "target_end": 21,
    "start_date": "",
    "end_date": "",
    "poll_interval": 30,
    "auto_book": False,           # 默认只通知，不下单
    "serverchan_key": "",
    "cityid": 75,                 # 上海
    "secret_api": "",             # /api/ 接口签名密钥（逆向所得，存于 config.json）
    "secret_ydb": "",             # /YDB/ 接口签名密钥
}

LOG_BUFFER = collections.deque(maxlen=500)
_log_lock = threading.Lock()

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock:
        LOG_BUFFER.append(line)
    print(line)

def tomorrow():
    """下一个自然日（默认开始/结束日期）"""
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
    else:
        cfg = dict(DEFAULT_CONFIG)
    # 日期默认值：下一个自然日
    if not cfg.get("start_date"):
        cfg["start_date"] = tomorrow()
    if not cfg.get("end_date"):
        cfg["end_date"] = tomorrow()
    return cfg

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# ---- 抓包（仅刷新会话）----
_mitm_proc = None
_mitm_lock = threading.Lock()

def mitm_is_alive():
    with _mitm_lock:
        return _mitm_proc is not None and _mitm_proc.poll() is None

def mitm_start():
    global _mitm_proc
    with _mitm_lock:
        if _mitm_proc and _mitm_proc.poll() is None:
            return True, "已在运行"
        import subprocess as sp
        sp.run(["pkill", "-f", "mitmdump"], capture_output=True)
        time.sleep(1)
        try:
            FLOWS_FILE.parent.mkdir(parents=True, exist_ok=True)
            FLOWS_FILE.unlink(missing_ok=True)
            _mitm_proc = subprocess.Popen(
                [MITMDUMP_BIN, "-q", "-p", "8080", "-w", str(FLOWS_FILE)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            if _mitm_proc.poll() is None:
                log("抓包已启动 (8080)")
                return True, "已启动"
            return False, "启动失败"
        except Exception as e:
            return False, str(e)

def mitm_stop():
    global _mitm_proc
    with _mitm_lock:
        if _mitm_proc:
            try:
                _mitm_proc.terminate(); _mitm_proc.wait(timeout=5)
            except Exception:
                _mitm_proc.kill()
            _mitm_proc = None
        log("抓包已停止")
    return True

def extract_session():
    """从抓包提取 appsessionid 和 userid（不提取 sign）"""
    if not FLOWS_FILE.exists() or FLOWS_FILE.stat().st_size == 0:
        return None, "抓包文件为空"
    try:
        r = subprocess.run([MITMDUMP_BIN, "-r", str(FLOWS_FILE), "-p", "9125",
                            "-s", str(ANALYZE_SCRIPT)],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        return None, str(e)
    text = r.stdout + r.stderr
    appsessionid, userid = "", 0
    m = re.search(r'"appsessionid"\s*:\s*"([^"]+)"', text)
    if m:
        appsessionid = m.group(1)
    m = re.search(r'"userid"\s*:\s*(\d+)', text)
    if m:
        userid = int(m.group(1))
    if not appsessionid:
        return None, "未提取到 appsessionid"
    return {"appsessionid": appsessionid, "userid": userid}, None

# ---- 监控引擎 ----
_monitor_running = False
_monitor_thread = None
_monitor_lock = threading.Lock()

def gen_dates(start_date, end_date):
    """生成日期字符串列表（含首尾）"""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if end < start:
        end = start
    days = (end - start).days + 1
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

def send_notify(title, content, sckey):
    if not sckey:
        return
    try:
        requests.post(f"https://sctapi.ftqq.com/{sckey}.send",
                      data={"title": title, "desp": content}, timeout=10)
    except Exception:
        pass

def monitor_loop():
    global _monitor_running
    cfg = load_config()
    appid = cfg.get("appsessionid", "")
    uid = cfg.get("userid", 0)
    priority = cfg.get("stadium_priority", [])
    stadiums = cfg.get("stadiums", {})
    sh, eh = cfg["target_start"], cfg["target_end"]
    dates = gen_dates(cfg["start_date"], cfg["end_date"])
    interval = cfg["poll_interval"]
    auto_book = cfg.get("auto_book", False)
    sckey = cfg.get("serverchan_key", "")
    round_num = 0

    log(f"监控启动: {len(priority)} 场地, {len(dates)} 天, {sh:02d}:00-{eh:02d}:00, 自动下单={'开' if auto_book else '关'}")

    while _monitor_running:
        round_num += 1
        now = datetime.now().strftime("%H:%M:%S")
        log(f"--- 第{round_num}轮 {now} ---")

        for d in dates:
            date_ts = sign.date_to_ts(d)
            hit = None  # 优先级最高的空场场地
            for sid in priority:
                name = stadiums.get(str(sid), f"场地{sid}")
                data = sign.get_schedule(sid, date_ts, uid, appid)
                if isinstance(data, dict) and "error" in data:
                    if "签名" in data["error"] or "登录" in data["error"] or "session" in data["error"].lower():
                        log(f"  {name} {d} ❌ 会话失效: {data['error']}")
                    else:
                        log(f"  {name} {d} ❌ {data['error']}")
                    continue
                parsed = sign.parse_schedule(data, sh, eh)
                if parsed.get("target"):
                    hit = (sid, name, parsed)
                    break  # 找到优先级最高的空场，停止
                elif parsed.get("nearest"):
                    n = parsed["nearest"]
                    log(f"  {name} {d} ⚪ 全满 | 最近 {n['field']} {n['time']} ¥{n['price']}")
                else:
                    log(f"  {name} {d} ⚪ 全天无空场")

            if hit:
                sid, name, parsed = hit
                slots = parsed["target"]
                log(f"  🎾 {name} {d} 空场 {len(slots)} 个:")
                lines = []
                for s in slots:
                    info = f"{s['field']} {s['time']} ¥{s['price']}"
                    lines.append(info)
                    log(f"     🟢 {info}")
                if auto_book:
                    # 下单第一个空场
                    s0 = slots[0]
                    r = sign.book_field(sid, date_ts, uid, appid, s0["fieldid"], s0["timePoint"], 1)
                    if isinstance(r, dict) and "error" in r:
                        log(f"     ❌ 下单失败: {r['error']}")
                        send_notify(f"🎾 {name} {d} 有空场!", "\n".join(lines) + f"\n\n下单失败: {r['error']}", sckey)
                    else:
                        log(f"     🤖 已下单 [{r.get('orderuid')}] ¥{r.get('realExpense')}")
                        send_notify(f"🎾 已抢到 {name} {d}!", "\n".join(lines) + f"\n\n订单: {r.get('orderuid')}", sckey)
                else:
                    send_notify(f"🎾 {name} {d} 有空场!", "\n".join(lines), sckey)

        time.sleep(interval)

def monitor_start():
    global _monitor_running, _monitor_thread
    with _monitor_lock:
        if _monitor_running:
            return False, "监控已在运行"
        _monitor_running = True
        _monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        _monitor_thread.start()
        return True, "监控已启动"

def monitor_stop():
    global _monitor_running
    with _monitor_lock:
        _monitor_running = False
        log("监控已停止")
    return True, "监控已停止"

# ---- Flask ----
import logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)
app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    return jsonify({
        "mitm_alive": mitm_is_alive(),
        "monitor_running": _monitor_running,
        "local_ip": get_local_ip(),
        "proxy_port": 8080,
        "config": load_config(),
    })

@app.route("/api/capture/start", methods=["POST"])
def api_capture_start():
    ok, msg = mitm_start()
    return jsonify({"ok": ok, "msg": msg, "ip": get_local_ip(), "port": 8080})

@app.route("/api/capture/stop", methods=["POST"])
def api_capture_stop():
    mitm_stop()
    return jsonify({"ok": True})

@app.route("/api/capture/extract", methods=["POST"])
def api_capture_extract():
    data, err = extract_session()
    if err:
        return jsonify({"ok": False, "msg": err})
    cfg = load_config()
    cfg["appsessionid"] = data["appsessionid"]
    cfg["userid"] = data["userid"]
    save_config(cfg)
    log(f"会话已刷新: appsessionid={data['appsessionid'][:8]}... userid={data['userid']}")
    return jsonify({"ok": True, "appsessionid": data["appsessionid"], "userid": data["userid"]})

@app.route("/api/search", methods=["POST"])
def api_search():
    body = request.get_json()
    kw = body.get("keyword", "")
    cfg = load_config()
    appid = cfg.get("appsessionid", "")
    if not kw or not appid:
        return jsonify({"ok": False, "results": []})
    results = sign.search_stadium(kw, cfg.get("cityid", 75), appid)
    return jsonify({"ok": True, "results": results})

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())

@app.route("/api/config", methods=["POST"])
def api_update_config():
    cfg = load_config()
    body = request.get_json()
    for key in ["target_start", "target_end", "start_date", "end_date",
                "poll_interval", "auto_book", "serverchan_key", "appsessionid",
                "userid", "cityid", "stadiums", "stadium_priority",
                "secret_api", "secret_ydb"]:
        if key in body:
            cfg[key] = body[key]
    save_config(cfg)
    # 密钥更新后重新加载到 sign 模块
    if "secret_api" in body or "secret_ydb" in body:
        sign.reload_secrets()
    # 保存配置时打印摘要到日志（仅"保存配置"按钮触发的字段）
    if any(k in body for k in ["target_start", "target_end", "start_date",
                               "end_date", "poll_interval", "auto_book"]):
        sdb = cfg.get("stadiums", {})
        pri = cfg.get("stadium_priority", [])
        names = [sdb.get(str(s), s) for s in pri]
        dates = f"{cfg['start_date']} ~ {cfg['end_date']}"
        log(f"配置已保存: 日期 {dates} | 时段 {cfg['target_start']:02d}:00-{cfg['target_end']:02d}:00 | "
            f"间隔 {cfg['poll_interval']}s | 自动下单 {'开' if cfg['auto_book'] else '关'} | "
            f"场地: {', '.join(names) if names else '无'}")
    return jsonify({"ok": True})

@app.route("/api/monitor/start", methods=["POST"])
def api_monitor_start():
    ok, msg = monitor_start()
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    monitor_stop()
    return jsonify({"ok": True})

@app.route("/api/logs")
def api_logs():
    with _log_lock:
        return jsonify({"logs": list(LOG_BUFFER)})

@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    with _log_lock:
        LOG_BUFFER.clear()
    return jsonify({"ok": True})

@app.route("/api/test", methods=["POST"])
def api_test():
    cfg = load_config()
    appid = cfg.get("appsessionid", "")
    uid = cfg.get("userid", 0)
    priority = cfg.get("stadium_priority", [])
    stadiums = cfg.get("stadiums", {})
    sh, eh = cfg["target_start"], cfg["target_end"]
    dates = gen_dates(cfg["start_date"], cfg["end_date"])
    results = []
    for d in dates:
        date_ts = sign.date_to_ts(d)
        for sid in priority:
            name = stadiums.get(str(sid), f"场地{sid}")
            data = sign.get_schedule(sid, date_ts, uid, appid)
            if isinstance(data, dict) and "error" in data:
                results.append({"stadium": name, "date": d, "error": data["error"]})
                continue
            parsed = sign.parse_schedule(data, sh, eh)
            results.append({"stadium": name, "date": d,
                            "slots": parsed.get("target", []),
                            "nearest": parsed.get("nearest")})
    return jsonify({"results": results})

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5100)
    args = p.parse_args()
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
    (BASE_DIR / "templates").mkdir(exist_ok=True)
    log(f"管理后台: http://localhost:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False)

if __name__ == "__main__":
    main()
