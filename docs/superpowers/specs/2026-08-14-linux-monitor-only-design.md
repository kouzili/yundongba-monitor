# Linux 纯监控版改造设计

日期：2026-08-14
状态：待评审

## 背景

项目当前形态是「监控 + 自动下单」，且多处绑定 macOS：

- `app.py:19` `MITMDUMP_BIN = "/opt/homebrew/bin/mitmdump"`
- `app.py:20` `ANALYZE_SCRIPT = ~/captures/analyze2.py` —— 该文件不在仓库中，「提取会话」开箱即坏
- `reverse_wxapkg.py:41` 微信 Mac 客户端缓存路径

目标运行环境已确认为局域网内一台常开 Linux 裸机（`192.168.100.254/24`，Python 3.11，有 docker，无 mitmproxy / adb / 微信）。手机为安卓。

已实测确认的事实：

1. `wxapi.sports8.com.cn` 从该机器直连可达，无 IP / UA 门槛
2. 服务端**先校验签名**：坏签名返回 `{"returnCode":"-1","returnMsg":"签名错误"}`；带假 `appsessionid` 与完全不带 `appsessionid` 返回完全相同的错误，均未提及登录态
3. 缺签名字段返回 `参数[sign]不能为空`
4. `www.sports8.com.cn` 仅为 App 下载落地页（无登录表单、无 API 引用），**不存在网页版订场入口**

## 目标

在该 Linux 机器上无人值守监控多场地空余时段，命中后飞书机器人推送。

## 非目标

- 自动下单（本次物理删除相关代码）
- mitmproxy 抓包子系统（本次删除）
- 安卓容器 / redroid（仅作为风险兜底，不实现）
- 网页版接入（已确认不存在）

## 关键收缩

放弃下单后，需求链条产生三处连锁简化：

1. **只需 `secret_api`**。`sign.py:60` 按路径前缀选密钥，而查排期 `api/ydb/stadium/apiGetStadiumShedule` 与搜场地 `api/ydb/seach/apiSearchKeyword` 均为 `/api/` 前缀。`secret_ydb` 可删。
2. **可能完全不需要 session**。场馆排期属公开信息。若服务端对该接口只校验签名，则 `appsessionid` 留空、`userid=0` 即可查询，「几天过期需重新抓包」这一唯一周期性人工步骤消失。此假设在阶段 0 一次性验证（见下）。
3. **下单路径全删**：`book_field`、`auto_book`、前端开关、`monitor.py`（含其硬编码的 `BOOK_SIGNS`）。

## 结构

```
sign.py               签名 + 查排期 + 搜场地      改：删 book_field / secret_ydb
notify.py             飞书机器人推送              新增
app.py                Flask 后台 + 监控线程       改：删 mitm_* / extract_session / auto_book
reverse_wxapkg.py     wxapkg 解密 + 解包          改：删 macOS 自动搜索，改为 --pkg 必填
tools/get_secret.py   一次性取密钥                新增
templates/index.html  管理界面                    改：删自动下单开关，密钥输入改掩码语义
monitor.py            删
```

## 阶段 0：一次性取 `secret_api`（唯一需要动手机的步骤）

`tools/get_secret.py` 单次运行完成取包、解包、认密钥，并同时给出「session 是否必需」的结论。

### 取包

安卓上小程序包未加密（`0xBE` 开头），`decrypt_wxapkg` 的非 `V1MMWX` 分支会原样透传，无需解密。

手机文件管理器进入 `Android/data/com.tencent.mm/MicroMsg/*/appbrand/pkg/`，找到韵动吧的 `.wxapkg`。

传输方式：`get_secret.py --upload` 起一个临时上传页，手机浏览器打开 `http://192.168.100.254:5101` 选文件上传，收到一个文件后自行退出。此处绑 `0.0.0.0` 是有意的（手机必须可达）且生命周期仅一次上传。已有文件时用 `--pkg PATH` 跳过该步。

### 解包与候选抽取

解包到 `reverse_unpacked/`（已在 `.gitignore`）。从所有 `.js` 文件中抽取字符串字面量作为密钥候选：

- 正则 `["']([A-Za-z0-9_\-]{8,64})["']`
- **优先级排序**：出现在 `&key=` / `key=` / `signBymd5` 前后 200 字符内的候选排在最前，其余按出现顺序在后
- 去重，上限 300 个；被截断时必须 `log` 出丢弃数量，不做静默截断

### 在线预言机认密钥

对每个候选，用 `sign_bymd5` 构造一次查排期请求（默认 `stadiumid=1128`、日期取次日 00:00 时间戳、`userid=0`、`appsessionid=""`），按返回分类：

| 返回 | 判定 |
|------|------|
| `returnCode == "0"` | **命中，且空 session 可用** → 阶段 1 结论：无状态 |
| `returnMsg` 含「签名」 | 候选错误，继续 |
| 其它错误（如未登录 / 会话失效） | **命中，但需要 session** → 阶段 1 结论：需登录，打印 `returnMsg` |

命中即停。候选间隔 0.5s，降低风控概率。`--dry-run` 只列候选不发请求。

命中后把 `secret_api` 写入 `config.json`，并打印结论。

### 兜底

若 300 个候选全灭，说明密钥可能是拼接或混淆产物。此时 `get_secret.py --dump-sign-context` 打印 `signBymd5` 前后源码供人工判读。

## 阶段 1：分支决策

- **空 session 可用** → `config.json` 的 `appsessionid` 留空，永久无状态，改造完成
- **需要 session** → `.wxapkg` 已在手，从解包源码读出登录接口，另起一轮实现本地登录（手机号 + 短信验证码）。本 spec 不覆盖该分支的实现。

`appsessionid` / `userid` 两个配置字段保留（默认 `""` / `0`）以支持该分支，值非空时照常作为请求头 / 参数发出。

## 监控引擎

### 上报范围

改变现有语义：`app.py:204` 命中即 `break` 是为「只能下一单」服务的，纯通知场景下退化为信息丢失。

改为**每轮查完所有场地 × 所有日期，全部有空场的都上报**，`stadium_priority` 只决定日志与推送中的排序，不再截断查询。每轮请求数 = 场地数 × 天数。

### 通知去重

现状每轮命中都推一次，30s 间隔会刷屏。改为状态机：

```
key = (stadiumid, date, fieldid, timePoint)
cur  = 本轮所有空场的 key 集合
new  = cur - prev
若 new 非空 → 推送 new（按优先级排序）
prev = cur
```

空场消失时 key 自动移出 `prev`，下次再出现会重新推送 —— 这是想要的行为（场被人退了值得再提醒一次）。

`prev` 仅存内存，进程重启后已知空场会重推一次，可接受。每轮配置重载后需把 `prev` 裁剪到当前监控范围内的 key，避免场地/日期被移除后残留。

### 配置热生效

每轮循环开头重新 `load_config()`。现状是启动时读一次（`app.py:170`），改配置必须重启监控。

### 停止响应

`time.sleep(interval)` 拆成 1 秒分片轮询停止标志，点「停止」不再最多等一整轮。

### 静默死亡防护

无人值守下「悄悄不工作」比报错更糟。记录连续全失败轮数，达到阈值（默认 3）推送一条告警，之后不重复推直到出现一次成功。

## 飞书推送（`notify.py`）

```
POST https://open.feishu.cn/open-apis/bot/v2/hook/<token>
{"msg_type": "text", "content": {"text": "..."}}
```

响应 `code == 0` 或 `StatusCode == 0` 视为成功，否则记录返回体到日志。

若机器人开启了签名校验，附加 `timestamp` 与 `sign`：

```python
string_to_sign = f"{timestamp}\n{secret}"
sign = base64(hmac_sha256(key=string_to_sign, msg=b""))
```

配置项 `feishu_webhook`（必填）、`feishu_secret`（可选，为空则不签名）。

推送内容按场地分组，每行 `场地名 · 球场 · 时段 · ¥价格`。

## 暴露面收紧

该机器长期开机且局域网上还挂着多个 docker 网桥。

- Flask 绑定改为 `127.0.0.1`，新增配置项 `bind_host`（默认 `127.0.0.1`）供需要时放开
- `/api/status` 与 `GET /api/config` **不返回** `secret_api`、`feishu_webhook`、`feishu_secret`、`appsessionid`，改为返回 `<field>_set: bool`
- `POST /api/config` 对这些字段：值非空则更新，空字符串视为「不修改」
- 前端对应输入框用 `type="password"`，placeholder 显示「已设置，留空则不改」

## 配置字段变更

| 字段 | 变更 |
|------|------|
| `secret_ydb` | 删除 |
| `auto_book` | 删除 |
| `serverchan_key` | 删除（改用飞书） |
| `feishu_webhook` | 新增，必填 |
| `feishu_secret` | 新增，可选（机器人开启签名校验时填） |
| `bind_host` | 新增，默认 `127.0.0.1` |
| `fail_alert_rounds` | 新增，默认 `3`，静默死亡告警阈值 |
| `secret_api` / `appsessionid` / `userid` / `stadiums` / `stadium_priority` / `target_start` / `target_end` / `start_date` / `end_date` / `poll_interval` / `cityid` | 保留 |

`DEFAULT_CONFIG`（`app.py:22`）与 `config.example.json` 同步更新。`config.json` 在本机尚不存在，`get_secret.py` 写入 `secret_api` 前若文件缺失需先按 `DEFAULT_CONFIG` 创建。

## 错误处理

- 签名类错误与会话类错误在日志中明确区分（现状 `app.py:196` 用关键词猜测，保留但措辞明确化）
- 网络异常记日志并继续下一轮，不中断监控线程
- 单个场地查询失败不影响同轮其它场地

## 测试

| 对象 | 方式 |
|------|------|
| `sign_bymd5` | 拿到 secret 后固化一组 `params → sign` 作为已知向量单测 |
| `parse_schedule` | 用真实响应存为 fixture，覆盖有空场 / 全满 / `nearest` 三种 |
| 去重状态机 | 纯单测：新增、重复、消失后重现三种转移 |
| 飞书推送 | 打本地假 webhook，断言 payload 形状与签名字段 |
| `get_secret` 候选抽取 | 用一小段构造的 JS 断言优先级排序与去重 |

## 风险

1. **session 可能仍是必需的** → 阶段 1 分支 B，`.wxapkg` 已在手，成本可控
2. **安卓 11+ `/Android/data` 访问受限** → 部分 ROM 需用系统自带文件管理器或 SAF 授权；若确实取不到包，兜底是本机 redroid 容器（不在本次范围）
3. **候选探测触发风控** → 0.5s 间隔 + 优先级排序（正确密钥预期在前几个命中）+ 300 上限
4. **密钥非明文常量** → `--dump-sign-context` 人工判读兜底

## 合规

沿用 README 现有立场：仅供个人学习与技术研究。本次改造移除自动下单，进一步降低对他人正常订场的干扰。
