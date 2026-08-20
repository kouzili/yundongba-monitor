#!/usr/bin/env bash
# 在一台新机器上装好韵动吧场地助手，并把 skill 注册给 Claude Code / Hermes。
#
#   bash install.sh              # 装到 ~/.claude/skills/ydb（对所有项目可见）
#   bash install.sh --project    # 只装到当前项目的 .claude/skills/ydb
#
# 不会碰 config.json —— 密钥必须自己带过来，绝不进 git。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCOPE="user"
[ "${1:-}" = "--project" ] && SCOPE="project"

if [ "$SCOPE" = "user" ]; then
    SKILL_DIR="$HOME/.claude/skills/ydb"
else
    SKILL_DIR="$HERE/.claude/skills/ydb"
fi

echo "==> 1/4 虚拟环境"
if [ ! -x "$HERE/.venv/bin/python" ]; then
    python3 -m venv "$HERE/.venv"
    echo "    已创建 .venv"
fi
"$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"
echo "    依赖就绪：$("$HERE/.venv/bin/python" -V)"

echo "==> 2/4 自检"
( cd "$HERE" && "$HERE/.venv/bin/python" -m pytest -q 2>&1 | tail -1 | sed 's/^/    /' )

echo "==> 3/4 凭据"
CONFIG="$HERE/config.json"
if [ ! -f "$CONFIG" ]; then
    cp "$HERE/config.example.json" "$CONFIG"
    echo "    ⚠️  已从模板创建 config.json，但里面是空的。"
fi
"$HERE/.venv/bin/python" - <<'PY'
import json, pathlib, sys
cfg = json.loads((pathlib.Path(__file__).parent if False else pathlib.Path("config.json")).read_text())
need = {
    "secret_api": "查排期/搜场馆的签名密钥（必需）",
    "secret_ydb": "畅打接口的签名密钥（查畅打才需要）",
    "userid":     "锁单才需要，跑 `ydb login` 自动获得",
    "mobile":     "登录用（锁单才需要）",
    "password":   "登录用（锁单才需要）",
    "feishu_webhook": "盯场推送才需要",
}
missing = [k for k in need if not cfg.get(k)]
for k, why in need.items():
    mark = "缺" if k in missing else "✅"
    print(f"    {mark} {k:<16} {why}")
if "secret_api" in missing:
    print("\n    ❌ 没有 secret_api，任何查询都会返回「签名错误」。")
    print("       从旧机器把 config.json 拷过来，或跑 tools/get_secret.py 重新取。")
    sys.exit(1)
PY

echo "==> 4/4 注册 skill 到 $SKILL_DIR"
mkdir -p "$SKILL_DIR"
# 把占位符换成本机的真实路径 —— skill 从任何工作目录被调用都要能找到 ydb
sed "s|__YDB_HOME__|$HERE|g" "$HERE/skill/SKILL.md" > "$SKILL_DIR/SKILL.md"
echo "    已写入 $SKILL_DIR/SKILL.md"

echo
echo "==> 冒烟测试（1 个真实请求）"
if "$HERE/ydb" search 网球 >/dev/null 2>&1; then
    echo "    ✅ API 可达且签名有效"
else
    echo "    ⚠️  查询失败，跑 '$HERE/ydb' search 网球 看具体报错"
fi

echo
echo "装好了。入口：$HERE/ydb"
echo "Claude Code / Hermes 重启后就能看到 ydb 这个 skill。"
