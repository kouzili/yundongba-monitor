# 🎾 韵动吧 · 场地助手

查询「[韵动吧](https://www.sports8.com.cn)」（上海）的场馆排期、空场时段、畅打活动报名情况，
可选锁单与盯场通知。

签名本地生成，**查询全程免登录**。三个凭据（`secret_api`、`secret_ydb`、`userid`）都是常量，
配好一次永久有效 —— 不需要抓包，不需要 mitmproxy，不需要定期刷新 session。

## 两种用法

**命令行 / 给 agent 调用**（输出 `{ok, summary, data}` JSON）：

```bash
./ydb search 徐泾
./ydb slots 穿岳 1189 --date 2026-08-24 --from 18 --to 21
./ydb find --date 2026-08-22 --from 8 --to 10      # 按时段筛候选场馆
./ydb freely --from 19 --to 22                     # 畅打 + 报名人数
./ydb watch 1189 --from 8 --to 10 [--book]         # 盯场（长跑）
./ydb orders / login / book / cancel
```

技能描述模板在 `skill/SKILL.md`，`install.sh` 会把它渲染进 `~/.claude/skills/ydb/`。

**常驻盯场 + 飞书推送**（`app.py`，Web 后台）：见下文。

## 功能

- **多场地 × 多日期**：每轮查完全部组合，有空场的都上报
- **自定义时段**：如 18:00–21:00 晚场
- **飞书推送**：新空场即时推送，支持机器人签名校验
- **通知去重**：同一个空场只推一次；被人退了再出现会重新推
- **静默死亡告警**：连续多轮全部失败会推一条告警，不会悄悄不工作
- **配置热生效**：改配置不用重启监控
- **Web 管理后台**：可视化配置、一键启停、实时日志

## 环境要求

- Linux / macOS，Python 3.10+
- 一台安卓手机（只在第一次取签名密钥时需要）

## 快速开始

### 迁移到另一台机器

仓库里**不含任何密钥**（`config.json` 已 gitignore），268KB，可以安全推送到私有仓。

```bash
git clone <你的仓库> && cd yundongba-monitor
# 把旧机器的 config.json 单独拷过来（scp / 手动填，绝不要进 git）
bash install.sh          # 建 venv + 自检 + 注册 skill 到 ~/.claude/skills/ydb
```

`install.sh` 会检查缺哪些字段并说明各自用途；缺 `secret_api` 会直接失败，
因为那样任何查询都只会返回「签名错误」。

装完入口是 `./ydb`，从任何工作目录调用都成立。

### 从零开始（没有现成 config.json）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. 取签名密钥（一次性）

密钥是小程序里的一个字符串常量，解包就能拿到。**这是唯一需要动手机的步骤，做完之后永久有效。**

**先在手机微信里打开韵动吧小程序，逛一下订场页**，让微信把代码包缓存下来。这一步漏了后面必然找不到包。

安卓上的包未加密，直接拷就行，位置：

```
/sdcard/Android/data/com.tencent.mm/MicroMsg/<一串乱码>/appbrand/pkg/
```

里面是一堆 `_数字_数字.wxapkg`，你用过的所有小程序都堆在这儿。**不用挑哪个是韵动吧** —— 全都拿过来，工具会按 API 域名自己认（主包 + 分包都会被认出来）。

取包三条路，从省事到可靠：

```bash
# A. adb 直连（推荐，安卓 11+ 也能用：adb shell 不受 scoped storage 限制）
#    先 sudo apt install adb，手机开 USB 调试并在弹窗点「允许」
.venv/bin/python tools/get_secret.py --adb

# B. 手机自带文件管理器能进 Android/data 的话，起上传页，手机浏览器全选上传
.venv/bin/python tools/get_secret.py

# C. 已经有包或整个目录
.venv/bin/python tools/get_secret.py --pkg ~/wxpkg
```

> 第三方文件管理器在安卓 11+ 进不去 `Android/data`，但 `adb shell` 可以 —— 所以 A 比 B 稳。

工具会解包、筛出韵动吧的包、抽密钥候选、逐个代入真实接口验证，命中后把 `secret_api` 写进 `config.json`。

想先看看抽出来的候选而不打接口：加 `--dry-run`。

结束时会告诉你两件事之一：

- **「空 appsessionid 就能查排期」** → 不需要登录，直接进下一步，从此完全无人值守
- **「密钥对了，但需要登录态」** → 还需要补一个 `appsessionid`（登录接口就在刚解包出来的源码里）

### 3. 配飞书机器人

飞书群 → 设置 → 群机器人 → 添加「自定义机器人」→ 复制 Webhook 地址。

如果开了「签名校验」，把那个密钥也记下来。

### 4. 启动

```bash
bash start.sh
```

打开 http://localhost:5100 ，然后：

1. **凭证**卡片里粘贴飞书 Webhook（和签名密钥，如果有），点「保存凭证」→「测试推送」确认能收到
2. **场地**：搜关键词（如「古北」）→ 点「+ 添加」，可拖拽排序
3. **日期 & 时段**：选日期范围、目标时段、轮询间隔，点「保存配置」
4. 点「🔍 查询一次」确认能查到数据
5. 点「▶ 开始监控」，右侧日志实时显示

## 配置说明

`config.json`（已 gitignore）。密钥类字段也可以直接在后台页面里填。

| 字段 | 说明 |
|------|------|
| `secret_api` | 签名密钥，`tools/get_secret.py` 自动写入 |
| `feishu_webhook` | 飞书自定义机器人的 Webhook 地址 |
| `feishu_secret` | 机器人开了签名校验才需要，否则留空 |
| `appsessionid` / `userid` | 通常留空；只有在查排期需要登录态时才用 |
| `stadiums` / `stadium_priority` | 场地表与顺序，在后台页面里维护 |
| `target_start` / `target_end` | 目标时段，左闭右开（18/21 = 18:00–21:00） |
| `start_date` / `end_date` | 监控日期范围，含首尾 |
| `poll_interval` | 轮询间隔秒数，建议 ≥30 |
| `fail_alert_rounds` | 连续多少轮全失败后推告警，默认 3 |
| `bind_host` | 默认 `127.0.0.1` 只监听本机；要从局域网访问改成 `0.0.0.0` |

## 项目文件

| 文件 | 说明 |
|------|------|
| `cli.py` | 命令行入口，给 agent 调用 |
| `sign.py` | 签名 + API 客户端（查排期 / 搜场地 / 登录 / 锁单 / 取消） |
| `freely.py` | 畅打活动 |
| `ratelimit.py` | 全局配速器（跨进程共享） |
| `engine.py` | 盯场引擎：单轮轮询 + 主循环 |
| `dedup.py` | 通知去重状态机 |
| `notify.py` | 飞书推送 |
| `app.py` | Flask 后台 + REST API |
| `tools/get_secret.py` | 一次性取签名密钥 |
| `reverse_wxapkg.py` | wxapkg 解包 |
| `ydb` | 入口 wrapper（自动定位 venv） |
| `install.sh` | 新机器安装 + 注册 skill |
| `skill/SKILL.md` | 技能描述模板（含 `__YDB_HOME__` 占位符） |
| `tests/` | pytest 测试（213 个） |

## 平台边界

实测得出，用之前要知道：

- **只有上海**（`cityid=75`）
- **各场馆提前放场的天数不同**（`find` 返回的 `maxday`，实测有 5 天的）。查超过该天数会成功返回但 `fieldList` 为空 —— 是还没放场，不是没空位
- **`status=4` 表示不通过平台放场**（长租/教学/停用），不是「已订满」
- **同一时间只允许一笔未处理的场地订单**，付款窗口约 11 分钟，**过期不会自动清除**，
  必须显式取消或付款，否则一直挡住新订单

## 开发

```bash
.venv/bin/python -m pytest          # 全部测试（213 个）
.venv/bin/python sign.py            # 冒烟：查未来 3 天排期
```

改了 `skill/SKILL.md` 之后要重跑 `bash install.sh` 才会生效 ——
`~/.claude/skills/ydb/SKILL.md` 是渲染产物，不是源文件。

## 注意事项

- **密钥保密**：`config.json` 已在 `.gitignore`，切勿提交
- **后台默认只监听本机**：`/api/status` 不回传密钥，但后台没有鉴权，放开 `bind_host` 前想清楚
- **轮询频率**：建议 ≥30s，避免触发风控
- **`tools/get_secret.py` 会打真实接口**：最多 300 发，默认 0.5s 间隔。正确密钥一般在前几个候选就命中（离 `&key=` 越近的候选排越前）
- **合规提示**：仅供个人学习与技术研究，请勿用于商业用途或干扰他人正常订场

## License

MIT
