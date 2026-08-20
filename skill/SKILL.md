---
name: ydb
description: 查询上海「韵动吧」平台的网球等场馆预定情况、空场时段、畅打活动报名人数，以及锁单/取消订单。当用户问某个球馆有没有空场、某个时段能不能订、畅打放出来没有、报名几个人了、或者要求盯住某个时段并帮忙抢场时使用。也用于用户报出场馆名字（如「穿岳」「DUECE 徐泾」「古北」）询问预定情况的场景。
---

# 韵动吧场地助手

上海韵动吧订场平台的只读查询 + 锁单能力。签名本地生成，查询完全免登录。

## 运行

```bash
__YDB_HOME__/ydb <子命令> [--json]
```

这个路径由 `install.sh` 写死成本机的实际位置，从任何工作目录调用都成立。

加 `--json` 只输出 JSON。默认输出是「一句话摘要 + JSON 数据」。

所有子命令返回统一信封：

```json
{"ok": true, "summary": "一句话结论", "data": {...}}
```

简单问题直接转述 `summary`；需要挑时段、比价、排序时用 `data` 自己算。

## 子命令

### 查场馆 id

```bash
__YDB_HOME__/ydb search 徐泾
```
→ `data.stadiums`: `[{stadiumid, name}]`

场馆参数在别的子命令里可以直接写名字，会自动解析。**匹配到多个时会报错并列出候选，不会替用户猜** —— 这时要回去问用户是哪一个。

### 查空场

```bash
__YDB_HOME__/ydb slots 1189 --date 2026-08-24 --from 18 --to 21
__YDB_HOME__/ydb slots 穿岳 唛恩 --days 7 --from 19 --to 21
```

- 可以一次传多个场馆（id 或名字混着写）
- `--days N`：不指定日期时查未来 N 天（默认 3）
- `--from/--to`：整点，左闭右开。不给就是全天
- → `data.slots`: `[{stadium, stadiumid, date, time, field, fieldid, price}]`
- → `data.failures`: 查询失败的场馆/日期

### 按时段筛候选场馆

```bash
__YDB_HOME__/ydb find --date 2026-08-22 --from 8 --to 10 --sort distance
```

用于「周六早上 8-10 哪里有场」这类不指定场馆的问题。`--sort` 可选
`distance` / `minPrice` / `collect`。

- → `data.stadiums`: `[{stadiumid, name, district, distance_km, min_price, indoor, outdoor, tags, max_days_ahead, address, phone}]`
- **只给候选清单，不含具体空场时段** —— 拿到 id 后要再用 `slots` 确认哪个钟点空着
- `max_days_ahead` 是该场馆提前放场的天数（各馆不同，实测有 5 天的）。查超过这个天数必然是空的

### 查畅打

```bash
__YDB_HOME__/ydb freely --from 19 --to 22
__YDB_HOME__/ydb freely --stadium 1189 --date 2026-08-24
```

- → `data.activities`: `[{name, stadium, date, start, end, joined, limit, spots_left, full, price, distance_km, enroll_open, enroll_close}]`
- `joined/limit` 就是「已报名几人 / 上限几人」
- 出于隐私，**不返回其他报名者的姓名和头像**

### 盯场

```bash
__YDB_HOME__/ydb watch 1189 --days 7 --from 8 --to 10 --interval 60
__YDB_HOME__/ydb watch 穿岳 唛恩 --date 2026-08-22 --from 8 --to 10 --book
```

**长跑命令**，会一直占着终端直到 `--rounds` 用完、锁到单、或被中断。用于
「盯住周六早上 8-10，放出来告诉我」。

- 只推**新出现**的空场；同一个空场不会重复推。空场消失后再出现会重新推
- 近 3 天每轮都查，中期每 5 轮、远期每 30 轮查一次，省速率预算
- 连续整轮失败会退避（间隔翻倍，上限 30 分钟）并推告警，恢复后复位
- `--book` 盯到空场自动锁单。**单次会话最多锁 1 单，锁到即退出**
- 需要飞书推送就在 `config.json` 里配 `feishu_webhook`；没配只打终端

### 订单

```bash
__YDB_HOME__/ydb orders                        # 待处理订单
__YDB_HOME__/ydb book 1189 2026-08-24 6 --yes  # 锁单（真实订单！）
__YDB_HOME__/ydb cancel 2090646
__YDB_HOME__/ydb login                         # 首次或 userid 丢失时
```

## 锁单的硬性约束

**锁单会在用户账号上产生真实订单。没有 `--yes` 会被拒绝。除非用户明确要求下单，否则不要调用 `book`。**

平台规则（都是实测得出的）：

- **同一时间只允许一笔未处理的场地订单**。有挂单时锁单必定失败 —— 所以 `book` 会先自动检查，有挂单就直接返回错误并列出 orderid
- **付款窗口约 11 分钟**，锁单后要提醒用户去 App 付款
- **过期不等于自动清除**：超时的订单仍然挂在列表里继续挡住新订单，必须显式 `cancel` 或付款

锁单成功后，把 orderid 和「11 分钟内付款」一起告诉用户。

## 平台边界（回答问题时要考虑）

- **只有上海**（`cityid=75`）。其他城市查不到场馆
- **场馆只放约两周的场**。查 +30 天会成功返回但 `fieldList` 为空 —— 那是还没放场，不是没空位
- **场地 `status=4` 表示不通过平台放场**（长租/教学/停用），不要当成「已订满」。看到某片场连续多天全部不可订，多半是这个
- 排期返回的是整天所有时段，时段筛选在本地做

## 速率限制

所有请求经过全局配速器，默认 10 次/分钟，状态跨进程共享（`.rate_state.json`）。

这意味着**查多个场馆 × 多天会明显变慢**：3 个场馆 × 7 天 = 21 个请求 ≈ 2 分钟。回答用户时按需要的最小范围查，别无谓扩大 `--days`。

## 判断「值不值得盯」

用户问某场馆情况时，光报空场数不够。有用的信号：

- 未来几天**同一时段反复空着** → 不缺场，随时能订，不必盯
- 只有个别时段被订走（比如只有 19:00 满） → 那个时段才值得盯
- 远期日期全部满格且完全一致 → 那是「还没人订」的原始状态，不代表抢手
