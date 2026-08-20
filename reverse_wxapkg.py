#!/usr/bin/env python3
"""
微信小程序 wxapkg 解密 + 解包工具（通用）
==========================================
用于解包微信小程序代码包，提取 JS 源码以分析签名算法。

包有两种形态:
  - 安卓 (/sdcard/Android/data/com.tencent.mm/.../appbrand/pkg/*.wxapkg)
    未加密，0xBE 开头，直接解包即可，不需要 appid
  - 微信桌面端缓存
    V1MMWX 加密，需要 --appid 才能解密:
      密钥 PBKDF2-HMAC-SHA1(appid, salt=b"saltiest", iter=1000, dkLen=32)
      IV   b"the iv: 16 bytes"
      前 1024 字节 AES-256-CBC 解密取前 1023 字节，其余 XOR ord(appid[-2])

包路径必须显式给出（--pkg），不再自动搜索微信缓存目录 —— 那个路径只在
macOS 上成立。

用法:
  python3 reverse_wxapkg.py --pkg /path/to/pkg.wxapkg
  python3 reverse_wxapkg.py --pkg /path/to/dir/          # 递归找 *.wxapkg
  python3 reverse_wxapkg.py --pkg X --list               # 只列出，不解包
  python3 reverse_wxapkg.py --pkg X --appid wx123456789  # 加密包需要 appid
  python3 reverse_wxapkg.py --pkg X --search sign        # 解包后搜关键词
  python3 reverse_wxapkg.py --pkg X --out /tmp/out       # 指定输出目录

依赖: pycryptodome  (pip install pycryptodome)
"""

import argparse
import os
import struct
import sys
from hashlib import pbkdf2_hmac
from pathlib import Path

try:
    from Crypto.Cipher import AES
except ImportError:
    print("缺少依赖 pycryptodome，请先安装：pip install pycryptodome")
    sys.exit(1)


MAGIC = b"V1MMWX"
SALT = b"saltiest"
IV = b"the iv: 16 bytes"
PBKDF2_ITER = 1000
AES_BLOCK_LEN = 1024   # 头部 AES 加密区字节数
AES_PLAIN_LEN = 1023   # 解密后取前 1023 字节明文


def collect_packages(path) -> list:
    """path 是文件就返回它自己；是目录就递归找 *.wxapkg。"""
    p = Path(path)
    if p.is_file():
        return [p]
    if p.is_dir():
        return sorted(p.rglob("*.wxapkg"))
    return []


def is_encrypted(data: bytes) -> bool:
    """带 V1MMWX 魔数的是微信桌面端加密包，需要 appid 才能解；安卓上的包无此头。"""
    return data.startswith(MAGIC)


def decrypt_wxapkg(appid, data):
    """解密 V1MMWX 格式的 wxapkg，返回明文 bytes"""
    if not is_encrypted(data):
        # 安卓上的包本来就是明文，直接返回
        return data

    # 1. 派生 AES 密钥
    key = pbkdf2_hmac("sha1", appid.encode("utf-8"), SALT, PBKDF2_ITER, 32)

    # 2. 分离加密区
    aes_ct = data[6:6 + AES_BLOCK_LEN]          # 前 1024 字节 AES 加密
    xor_ct = data[6 + AES_BLOCK_LEN:]           # 其余 XOR 加密

    # 3. AES-256-CBC 解密（取前 1023 字节明文）
    cipher = AES.new(key, AES.MODE_CBC, IV)
    aes_pt = cipher.decrypt(aes_ct)[:AES_PLAIN_LEN]

    # 4. XOR 解密
    xor_key = ord(appid[-2])
    xor_pt = bytes(b ^ xor_key for b in xor_ct)

    decrypted = aes_pt + xor_pt
    return decrypted


def safe_member_path(name: str) -> str:
    """把包内文件名收敛成 outdir 内的相对路径。

    包是从网上下载的，文件名不可信：前导斜杠会写到根目录，".." 会逃出输出目录。
    两者都剔掉而不是整条丢弃 —— 内容还是要看的，只是必须留在 outdir 里。
    """
    parts = [p for p in Path(name.lstrip("/")).parts
             if p not in ("..", ".", "/")]
    return str(Path(*parts)) if parts else ""


def unpack_wxapkg(data, outdir):
    """解包 wxapkg，返回 [(文件名, 大小), ...]"""
    if len(data) < 18 or data[0] != 0xBE:
        raise ValueError("不是有效的 wxapkg（首字节应为 0xBE）")

    file_count = struct.unpack(">I", data[14:18])[0]
    pos = 18
    files = []
    for _ in range(file_count):
        name_len = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        name = data[pos:pos + name_len].decode("utf-8", errors="replace")
        pos += name_len
        offset = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        files.append((name, offset, size))

    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    result = []
    for name, offset, size in files:
        clean = safe_member_path(name)
        if not clean:
            continue
        path = outdir / clean
        # 双保险：清洗后仍落在 outdir 之外的直接跳过
        if not path.resolve().is_relative_to(outdir):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data[offset:offset + size])
        result.append((clean, size))
    return result


def search_unpacked(outdir, keyword):
    """在解包目录中搜索关键词，打印命中文件与上下文"""
    outdir = Path(outdir)
    print(f"\n=== 搜索关键词「{keyword}」 ===")
    hit = False
    for path in sorted(outdir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if keyword in text:
            hit = True
            idx = text.find(keyword)
            ctx = text[max(0, idx - 60):idx + 120]
            print(f"\n[{path.name}]")
            print(f"  ...{ctx}...")
    if not hit:
        print("  未找到匹配")


def main():
    p = argparse.ArgumentParser(description="微信小程序 wxapkg 解包工具")
    p.add_argument("--pkg", required=True, help="wxapkg 文件或包含它的目录")
    p.add_argument("--appid", help="小程序 appid，仅解密 V1MMWX 加密包时需要")
    p.add_argument("--out", help="输出目录（默认 ./reverse_unpacked/<包名>）")
    p.add_argument("--list", action="store_true", help="只列出找到的包，不解包")
    p.add_argument("--search", help="解包后搜索关键词")
    args = p.parse_args()

    targets = collect_packages(args.pkg)
    if not targets:
        print(f"{args.pkg} 下没找到 .wxapkg。安卓上的路径通常是："
              f"\n  /sdcard/Android/data/com.tencent.mm/MicroMsg/*/appbrand/pkg/")
        return

    if args.list:
        print("找到的小程序包：")
        for path in targets:
            print(f"  {path}  ({path.stat().st_size} 字节)")
        return

    for path in targets:
        print(f"\n=== 处理 {path.name} ===")
        raw = path.read_bytes()
        print(f"  原始大小: {len(raw)} 字节, 魔数: {raw[:6]!r}")

        if is_encrypted(raw) and not args.appid:
            print("  ❌ 这是微信桌面端的加密包，需要 --appid 才能解密。"
                  "安卓上取的包不需要 appid。")
            continue

        decrypted = decrypt_wxapkg(args.appid or "", raw)
        print(f"  解密后: {len(decrypted)} 字节, 魔数: {decrypted[:4].hex()}")

        outdir = args.out or os.path.join("reverse_unpacked", path.stem)
        files = unpack_wxapkg(decrypted, outdir)
        print(f"  解包 {len(files)} 个文件 -> {outdir}")

        # 高亮 JS 文件
        js_files = [f for f, _ in files if f.endswith(".js")]
        if js_files:
            big = sorted([(s, f) for f, s in files if f.endswith(".js")], reverse=True)[:3]
            print("  主要 JS 文件:")
            for size, name in big:
                print(f"    - {name} ({size} 字节)")

        if args.search:
            search_unpacked(outdir, args.search)


if __name__ == "__main__":
    main()
