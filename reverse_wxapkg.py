#!/usr/bin/env python3
"""
微信小程序 wxapkg 解密 + 解包工具（通用）
==========================================
用于逆向微信小程序代码包（V1MMWX 加密格式），提取 JS 源码以分析签名算法。

解密原理（V1MMWX 格式，已实测验证）:
  - 文件头: 前 6 字节魔数 b"V1MMWX"
  - 密钥:   PBKDF2-HMAC-SHA1(appid, salt=b"saltiest", iterations=1000, dkLen=32)
  - IV:     b"the iv: 16 bytes"
  - 前 1024 字节用 AES-256-CBC 解密，取前 1023 字节明文
  - 其余字节 XOR 解密，key = ord(appid[-2])

用法:
  python3 reverse_wxapkg.py                       # 自动搜索微信缓存里的小程序包并解密
  python3 reverse_wxapkg.py --list                # 只列出找到的包，不解密
  python3 reverse_wxapkg.py --appid wx123456789   # 指定 appid 解密
  python3 reverse_wxapkg.py --pkg /path/__APP__.wxapkg  # 指定包路径
  python3 reverse_wxapkg.py --search sign         # 解包后搜索关键词（如 sign/md5/appkey）
  python3 reverse_wxapkg.py --out /tmp/out        # 指定输出目录

依赖: pycryptodome  (pip install pycryptodome)
"""

import argparse
import glob
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


# 微信 Mac 客户端小程序包缓存目录
WECHAT_CONTAINER = os.path.expanduser(
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
    "app_data/radium/users/*/applet/packages/*/*/__APP__.wxapkg"
)

MAGIC = b"V1MMWX"
SALT = b"saltiest"
IV = b"the iv: 16 bytes"
PBKDF2_ITER = 1000
AES_BLOCK_LEN = 1024   # 头部 AES 加密区字节数
AES_PLAIN_LEN = 1023   # 解密后取前 1023 字节明文


def find_wxapkg(appid=None):
    """搜索微信缓存目录里的小程序包，返回 [(appid, path), ...]"""
    results = []
    for path in glob.glob(WECHAT_CONTAINER):
        # 从路径解析 appid: .../packages/<appid>/<version>/__APP__.wxapkg
        parts = path.split(os.sep)
        try:
            idx = parts.index("packages")
            pkg_appid = parts[idx + 1]
        except (ValueError, IndexError):
            pkg_appid = "?"
        if appid and pkg_appid != appid:
            continue
        results.append((pkg_appid, path))
    return results


def decrypt_wxapkg(appid, data):
    """解密 V1MMWX 格式的 wxapkg，返回明文 bytes"""
    if not data.startswith(MAGIC):
        # 可能已经是明文包，直接返回
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

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    result = []
    for name, offset, size in files:
        clean = name.lstrip("/")   # 去掉前导斜杠，防止写出目录
        if not clean:
            continue
        path = outdir / clean
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
    p = argparse.ArgumentParser(description="微信小程序 wxapkg 解密 + 解包工具")
    p.add_argument("--appid", help="指定小程序 appid")
    p.add_argument("--pkg", help="指定 __APP__.wxapkg 路径（默认自动搜索）")
    p.add_argument("--out", help="输出目录（默认 ./reverse_unpacked/<appid>）")
    p.add_argument("--list", action="store_true", help="只列出找到的包，不解密")
    p.add_argument("--search", help="解包后搜索关键词")
    args = p.parse_args()

    # 确定要处理的包
    targets = []
    if args.pkg:
        appid = args.appid or "unknown"
        targets = [(appid, args.pkg)]
    else:
        targets = find_wxapkg(args.appid)

    if not targets:
        print("未找到小程序包。请先打开微信小程序让其缓存代码包，或用 --pkg 指定路径。")
        return

    if args.list:
        print("找到的小程序包：")
        for appid, path in targets:
            size = os.path.getsize(path)
            print(f"  [{appid}] {path}  ({size} 字节)")
        return

    # 逐个解密 + 解包
    for appid, path in targets:
        print(f"\n=== 处理 [{appid}] ===")
        print(f"  包路径: {path}")
        with open(path, "rb") as f:
            raw = f.read()
        print(f"  原始大小: {len(raw)} 字节, 魔数: {raw[:6]!r}")

        decrypted = decrypt_wxapkg(appid, raw)
        print(f"  解密后: {len(decrypted)} 字节, 魔数: {decrypted[:4].hex()}")

        outdir = args.out or os.path.join("reverse_unpacked", appid)
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
