"""wxapkg 解包 —— 格式解析与路径穿越防护。

输入是从网上下载来的小程序包，属于不可信输入：包内文件名由包决定，
不能让它写到输出目录之外。
"""
import struct

import pytest

import reverse_wxapkg


def build_package(entries):
    """entries: [(包内文件名, 内容 bytes)] -> 合法的 wxapkg 字节流"""
    index = b""
    bodies = b""
    # 先算索引长度，才能确定各文件的绝对 offset
    index_len = 18 + sum(4 + len(n.encode()) + 4 + 4 for n, _ in entries)
    for name, content in entries:
        encoded = name.encode()
        index += struct.pack(">I", len(encoded)) + encoded
        index += struct.pack(">I", index_len + len(bodies))
        index += struct.pack(">I", len(content))
        bodies += content
    header = b"\xbe" + b"\x00" * 13 + struct.pack(">I", len(entries))
    return header + index + bodies


def test_unpacks_entries_with_their_contents(tmp_path):
    package = build_package([("app.js", b"var a=1"), ("page/x.js", b"var b=2")])

    written = reverse_wxapkg.unpack_wxapkg(package, tmp_path)

    assert (tmp_path / "app.js").read_bytes() == b"var a=1"
    assert (tmp_path / "page/x.js").read_bytes() == b"var b=2"
    assert sorted(n for n, _ in written) == ["app.js", "page/x.js"]


def test_leading_slash_in_entry_name_is_stripped(tmp_path):
    package = build_package([("/app.js", b"x")])

    reverse_wxapkg.unpack_wxapkg(package, tmp_path)

    assert (tmp_path / "app.js").read_bytes() == b"x"


def test_parent_directory_traversal_cannot_escape_the_output_dir(tmp_path):
    outdir = tmp_path / "out"
    package = build_package([("../escaped.js", b"pwned")])

    reverse_wxapkg.unpack_wxapkg(package, outdir)

    assert not (tmp_path / "escaped.js").exists()
    assert list(outdir.rglob("*.js")) != []      # 落在 outdir 里，没被静默丢弃


def test_absolute_path_entry_cannot_escape_the_output_dir(tmp_path):
    outdir = tmp_path / "out"
    package = build_package([("/etc/passwd", b"pwned")])

    reverse_wxapkg.unpack_wxapkg(package, outdir)

    assert (outdir / "etc/passwd").read_bytes() == b"pwned"


def test_deep_traversal_is_also_contained(tmp_path):
    outdir = tmp_path / "a" / "b" / "out"
    package = build_package([("../../../../../../tmp/escaped.js", b"pwned")])

    reverse_wxapkg.unpack_wxapkg(package, outdir)

    assert list(outdir.rglob("escaped.js")) != []


def test_rejects_data_that_is_not_a_wxapkg():
    with pytest.raises(ValueError):
        reverse_wxapkg.unpack_wxapkg(b"not a package at all", "/tmp/whatever")


# ---- 解密分支 ----

def test_plain_android_package_passes_through_undecrypted():
    # 安卓上的包未加密，没有 V1MMWX 魔数，应原样返回
    plain = b"\xbe\x00\x01\x02payload"

    assert reverse_wxapkg.decrypt_wxapkg("wx1234567890", plain) == plain


def test_encrypted_package_requires_an_appid():
    encrypted = b"V1MMWX" + b"\x00" * 2048

    assert reverse_wxapkg.is_encrypted(encrypted) is True
    assert reverse_wxapkg.is_encrypted(b"\xbe\x00\x00") is False
