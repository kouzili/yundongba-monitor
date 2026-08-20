# 韵动吧场地助手 —— Hermes 可调用的 skill

日期：2026-08-18
状态：待评审
前一阶段：`2026-08-14-linux-monitor-only-design.md`（已实现，122 测试全绿）

## 形态变化

上一阶段做的是「常驻监控 + Web 后台」。实际需求是「Hermes 按自然语言请求调用的工具」：

- 「这几个场馆预定情况如何」→ 一次性查询，要结构化结果
- 「盯住周六早上 8-10，放出来帮我抢」→ 盯场 + 下单
- 「某时段畅打放没放出，报名几个人了」→ 畅打查询

所以主体从 Flask 后台换成**命令行工具集**，每个子命令输出 JSON。Web 后台保留但降级为可选的观察面板。

## 已探明的接口事实（全部实测）

### 两把密钥、两套返回信封

| 前缀 | 密钥 | 信封 |
|------|------|------|
| `/api/` | `secret_api` | `returnCode` / `returnMsg` / `returnData` |
| `YDBCLUB/`、`YDB/` | `secret_ydb` | `result_code` / `result_msg` / `result_data` |

两把密钥都已探到并写入 `config.json`，都是常量、不过期。签名算法相同。
`sign.normalize_response()` 负责归一化 —— 只认一套会把 YDBCLUB 的「签名错误」
误判成「密钥对但需要登录」，结论整个反过来。

### 免登录可用（`appsessionid=""`、`userid=0`）

- `api/ydb/stadium/apiGetStadiumShedule` `{stadiumid, date, userid}` —— 查排期
- `api/ydb/seach/apiSearchKeyword` `{keyword, cityid, pageSize}` —— 关键词搜场馆
- `api/ydb/stadium/report/apiAreaCity` —— 区县列表（不传参默认返回北京）
- `YDBCLUB/service/personalCenter/activityList` —— 畅打活动列表
  `{page, size, longitude, latitude, begin, end, typecode:"201103", status:0, cityid, flag:"1", stadiumid?}`

### 需要登录

- `api/ydb/account/userLogin` `{mobile, password}` —— 手机号+密码，不需要微信 code
- `api/ydb/stadium/apiSetOrderField` `{userid, stadiumid, date, orderList}` —— 锁单
- `api/ydb/order/apiCancelOrder` `{orderid, userid}` —— 取消
- `api/ydb/order/apiGetNewOrderList_refund` `{orderStatus, page, pageSize, targettype, userid}` —— 订单列表

### 尚未接入但已定位

- `api/ydb/stadium/v2/apiGetSearchStadiumByV2`
  `{cityid, countyid, date, lat, lng, pageIndex, pageSize, sort, timePeriod, userid}`
  —— 按日期+时段找有空场的场馆。比关键词搜索更适合「周六早上 8-10 哪里有场」。
- `api/ydb/stadium/apiGetTravelParticipantsList` `{travelid}` —— 报名者列表
- `YDBCLUB/service/personalCenter/activityDetail` `{activityid, latitude, longitude, typecode}`

### 平台边界

- 场馆只放约两周的场。+14 天有数据，+30 天 `returnCode` 为 0 但 `fieldList` 为空 ——
  是场馆放场策略，不是接口限制。
- `cityid=75` 是上海。试过 1/2/131/289 搜「网球」都返回 0，平台大概率只做上海。
- `status` 字段：`0` 可订，`4` 不通过平台放场（长租/教学/停用，别当成"订满"）。

## 命令行接口

```
ydb search  <关键词>                     场馆名搜索 → [{stadiumid, name}]
ydb find    --date D --from H --to H     按时段找有空场的场馆（v2 接口）
ydb slots   <场馆…> [--date] [--from --to]  空场明细
ydb freely  [--stadium] [--date] [--from --to]  畅打活动 + 报名人数/上限
ydb watch   <条件> [--book]              盯场，可选自动锁单
ydb login                                拿/刷新 session（只有 watch --book 需要）
ydb orders                               订单列表（确认锁单结果）
```

所有子命令支持 `--json`。场馆参数接受 id 或名字（名字先走 search 解析，多个匹配则报错列出候选，不猜）。

## 速率控制（新增，核心）

现状缺口：`poll_round` 循环发请求**中间没有任何延迟**。5 场馆 × 14 天 = 70 个请求
背靠背打出去。虽然严格串行（非并发），但从服务端看是突发脉冲。

改为**全局配速器**，配置项 `max_requests_per_minute`（默认 10）：

- 所有出站请求经过同一个配速器，按 `60 / rpm` 秒间隔放行，带 ±20% 抖动
- 一次性查询命令也走它 —— 否则 Hermes 连续调几次就绕过了限制
- 跨进程共享：配速状态写在 `.rate_state.json`，避免 watch 常驻 + Hermes 临时调用叠加超速

`watch` 额外做**分层日期轮询**：近 3 天每轮查，+4~7 天每 5 轮，+8~14 天每 30 轮。
远期日期本来就全空（实测 +6 天之后全是满格未预订状态），每轮都查纯属浪费预算。

**自适应退避**：连续失败 → 间隔翻倍，上限 30 分钟；成功后复位。
未知错误（既非签名类也非登录类）单独归类并立即告警 —— 限流大概率长这样，第一次遇到就要知道。

## 登录与抢场

### session 管理

`login` 用 `{mobile, password}` 换 `appsessionid` + `userid`，存入 `config.json`。
`watch --book` 每轮检查：遇到登录类错误就自动重登一次，失败则告警并停止 book（但继续通知）。

密码存 `config.json`（已 gitignore，且 `/api/status` 与 `GET /api/config` 不回传）。
**密码不得出现在对话、日志、命令行参数里** —— 命令行参数会进 shell history 和 ps 输出。
由用户自己写入 `config.json`。

### 锁单行为（已实测验证，2026-08-19）

在 DUECE 徐泾(1189) 2026-08-24 06:00 室内01 跑了一次完整生命周期，全部通过，
账号无残留：

| 步骤 | 结果 |
|------|------|
| 锁单 `apiSetOrderField` | `orderid=2090646`、`orderuid=T2608191016599883237-YA89`、`realExpense=120.00`；返回字段 `discount / expireTime / orderid / orderuid / payStatus / promotion / realExpense` |
| 排期变化 | 该 fieldid 的 `status` 由 `0` 变 `1` |
| 订单列表 | 出现在待处理列表，`countDown=657`（秒） |
| 取消 `apiCancelOrder` | 返回 `{"code":"0"}` |
| 取消后 | 待处理列表清空，时段恢复 `status=0` |

**确认成立**：锁单不付款（付款是独立的 `apiSetPay`），全程 `appsessionid=""`，
仅靠 `userid` 完成鉴权。

### 平台规则（实测得出，直接约束 --book 的设计）

- **同一时间只允许一笔未处理的场地订单**。已有挂单时锁单返回
  「您有未处理的场地订单，请先去处理！」
- **付款窗口约 11 分钟**（`countDown` 657 秒起算）
- **过期不等于自动清除**：实测一笔 `countDown=-43` 的订单仍然挂在待处理列表里
  并继续挡住新订单，必须显式取消或付款

因此 `watch --book` 每轮锁单前必须先调 `pending_field_orders()`；有挂单就不锁，
改为只通知，并在通知里点明「有挂单挡路」。

**风险（必须让用户知情）**：未付款订单会挂住并影响后续操作 —— 用户已经亲历过一次
「请求已处理，请勿重复提交」。自动锁单意味着可能定期造出这种挂单；若平台对反复不付款
有违约或限制机制，后果落在用户账号上。因此：

- `--book` 默认关闭
- 单次 watch 会话最多锁 1 单，锁成功即停止盯场并立即通知
- 锁单后写入本地 `pending_orders.json`，`watch` 启动时若发现有未处理的挂单则拒绝再锁

## 隐私

`activityList` 的 `enrollPeoples` 含其他用户的真实姓名与头像 URL。skill 只输出
**人数与上限**（`joinCount` / `jointoplimit`），不输出他人身份信息，除非用户明确要求。

## Skill 打包

```
.claude/skills/ydb/SKILL.md      触发描述 + 用法（Hermes 读这个决定何时调用）
```

`SKILL.md` 说明：能回答什么问题、每个子命令的输入输出、平台边界（两周窗口、
仅上海、status=4 的含义），以及「抢场需要显式确认」这条约束。

## 可移植性（已验证）

全新 `python:3.11-slim` 容器（Debian 13，与宿主内核/发行版都不同）中
`pip install -r requirements.txt` → 122 测试全绿 + 真实 API 查询正常。

搬迁只需：代码（240KB）+ `config.json`（两把密钥）。不需要 `captures_pkg`、
`tools/android`、docker，也不需要那台 Windows。密钥是常量。

## 实现顺序

1. 全局配速器 + 跨进程状态（所有后续能力都依赖它）
2. `search` / `slots` / `find` —— 复用现有 `sign.py`，加 CLI 与 JSON 输出
3. `freely` —— 接 `activityList`，解析时间/人数/上限
4. `watch` —— 现有引擎 + 配速 + 分层日期 + 退避
5. `login` —— 单独一步，先只验证能拿到 session
6. 锁单单次真实验证 → 通过后再接 `--book`
7. `SKILL.md`

1-4 不需要密码，可以立刻做完。5-6 需要用户提供密码并同意真实下单一次。

## 测试

沿用现有做法：纯逻辑 TDD（配速器、分层日期、退避、畅打解析、场馆名解析），
接口层用 fixture + 本地假服务器，禁止在测试里打真实接口。
干净容器跑一遍全量作为可移植性回归。
