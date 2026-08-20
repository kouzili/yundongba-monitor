#!/usr/bin/env bash
# 在一台新机器上装好韵动吧场地助手，并把 skill 注册给 Claude Code / Hermes。
#
#   bash install.sh                    # 装到 ~/.claude/skills/ydb（Claude Code 的约定）
#   bash install.sh --project          # 装到当前项目的 .claude/skills/ydb
#   bash install.sh --skill-dir DIR    # 装到指定目录（别的 agent 框架用这个）
#   bash install.sh --print-skill      # 只把渲染好的 SKILL.md 打到标准输出，不安装
#
# SKILL.md 只是给 agent 看的说明书，放哪儿取决于你的 agent 怎么发现工具。
# 功能本身与此无关 —— ./ydb 是普通命令行程序，能跑 shell 就能用。
#
# 不会碰 config.json —— 密钥必须自己带过来，绝不进 git。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$HOME/.claude/skills/ydb"
case "${1:-}" in
    --project)    SKILL_DIR="$HERE/.claude/skills/ydb" ;;
    --skill-dir)  SKILL_DIR="${2:?--skill-dir 需要一个目录}" ;;
    --print-skill)
        sed "s|__YDB_HOME__|$HERE|g" "$HERE/skill/SKILL.md"
        exit 0 ;;
esac

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
echo "装好了。功能入口：$HERE/ydb"
echo "  这是个普通命令行程序，输出 {ok, summary, data} JSON —— 能跑 shell 就能用，"
echo "  不依赖任何 skill 机制。"
echo
echo "说明书已写到：$SKILL_DIR/SKILL.md"
echo "  这个位置是 Claude Code 的约定。如果你的 agent 从别处发现工具，用"
echo "  --skill-dir DIR 指定，或用 --print-skill 把内容取出来自己安置。"
