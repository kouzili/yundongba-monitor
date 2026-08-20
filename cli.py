#!/usr/bin/env python3
"""韵动吧场地助手 —— 命令行入口，给 Hermes 调用。

每个子命令输出统一信封：
    {"ok": bool, "summary": "一句话结论", "data": {...结构化数据...}}
简单问题直接转述 summary，复杂问题用 data 自己算。

用法:
  ydb search 徐泾
  ydb slots 1189 --date 2026-08-24 --from 18 --to 21
  ydb slots 穿岳 唛恩 --days 7 --from 19 --to 21
  ydb freely --date 2026-08-19 --from 19 --to 21
  ydb orders
  ydb book 1189 2026-08-24 6 --hours 1        # 真实锁单，需 --yes
  ydb cancel 2090646
"""
import argparse
import json
import sys
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

import freely
import ratelimit
import sign

CONFIG_FILE = Path(__file__).parent / "config.json"
RATE_STATE = Path(__file__).parent / ".rate_state.json"


class StadiumNotFound(Exception):
    pass


class AmbiguousStadium(Exception):
    pass


# ---- 输出信封 ----

def envelope(ok: bool, summary: str, data: dict) -> dict:
    return {"ok": ok, "summary": summary, "data": data}


# ---- 场馆解析 ----

def resolve_stadium(token: str, search) -> str:
    """把「穿岳」这种人话变成 stadiumid。

    歧义时抛异常并列出候选 —— 猜错会让用户盯着错的场馆，宁可让调用方再问一次。
    """
    token = str(token).strip()
    if token.isdigit():
        return token

    matches = search(token)
    if not matches:
        raise StadiumNotFound(f"没有找到名字含「{token}」的场馆")
    if len(matches) == 1:
        return matches[0]["stadiumid"]

    exact = [m for m in matches if m["name"] == token]
    if len(exact) == 1:
        return exact[0]["stadiumid"]

    listed = "；".join(f"{m['name']}({m['stadiumid']})" for m in matches)
    raise AmbiguousStadium(f"「{token}」匹配到 {len(matches)} 个场馆，请指明：{listed}")


# ---- 摘要 ----

def summarize_slots(slots: list, window: str) -> str:
    if not slots:
        return f"{window} 没有空场。"
    stadiums = list(OrderedDict.fromkeys(s["stadium"] for s in slots))
    dates = sorted({s["date"] for s in slots})
    span = dates[0] if len(dates) == 1 else f"{dates[0]} 至 {dates[-1]}（{'、'.join(dates)}）"
    return (f"{span} 的 {window} 共有 {len(slots)} 个空场，"
            f"分布在 {len(stadiums)} 个场馆：{'、'.join(stadiums)}。")


def summarize_freely(activities: list) -> str:
    if not activities:
        return "没有符合条件的畅打活动。"
    open_ones = [a for a in activities if not a.get("full")]
    if not open_ones:
        return f"{len(activities)} 场畅打活动全部报满。"
    spots = sum(a["spots_left"] for a in open_ones
                if a.get("spots_left") is not None)
    return (f"共 {len(activities)} 场畅打活动，{len(open_ones)} 场还有名额"
            f"（合计约 {spots} 个空位）。")


# ---- 运行时装配 ----

def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def setup(config: dict) -> None:
    sign.reload_secrets()
    sign.set_rate_limiter(ratelimit.RateLimiter(
        per_minute=config.get("max_requests_per_minute",
                              ratelimit.DEFAULT_PER_MINUTE),
        state_file=RATE_STATE))


def _search(config):
    return lambda keyword: sign.search_stadium(
        keyword, config.get("cityid", 75), config.get("appsessionid", ""))


def _dates(args) -> list:
    if args.date:
        return [args.date]
    start = datetime.now() + timedelta(days=1)
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(args.days)]


# ---- 子命令 ----

def cmd_search(args, config):
    results = _search(config)(args.keyword)
    summary = (f"「{args.keyword}」匹配到 {len(results)} 个场馆。"
               if results else f"没有找到名字含「{args.keyword}」的场馆。")
    return envelope(bool(results), summary, {"stadiums": results})


def cmd_slots(args, config):
    search = _search(config)
    stadiums, unresolved = {}, []
    for token in args.stadium:
        try:
            stadiums[resolve_stadium(token, search)] = token
        except (StadiumNotFound, AmbiguousStadium) as e:
            unresolved.append(str(e))
    if not stadiums:
        return envelope(False, "；".join(unresolved) or "没有可查询的场馆",
                        {"errors": unresolved})

    userid = config.get("userid", 0)
    session = config.get("appsessionid", "")
    slots, failures = [], []
    for stadiumid in stadiums:
        for date in _dates(args):
            raw = sign.get_schedule(stadiumid, sign.date_to_ts(date),
                                    userid, session)
            parsed = sign.parse_schedule(raw, args.start, args.end)
            if "error" in parsed:
                failures.append({"stadiumid": stadiumid, "date": date,
                                 "error": parsed["error"]})
                continue
            for s in parsed["target"]:
                slots.append({**s, "stadium": parsed["stadiumName"],
                              "stadiumid": stadiumid, "date": date})

    window = f"{args.start:02d}:00-{args.end:02d}:00"
    summary = summarize_slots(slots, window)
    if unresolved:
        summary += " " + "；".join(unresolved)
    return envelope(True, summary,
                    {"slots": slots, "failures": failures, "window": window})


def compute_begin(date_str, start_hour, now=None) -> int:
    """畅打列表的起始时间戳。

    接口按时间排序、从 begin 往后返回，所以查「今晚 19 点」时把 begin 设成
    今晚 19 点，比传「现在」再翻好几页高效得多，也不会因为页数不够而漏掉。
    """
    now = now or datetime.now()
    if date_str:
        day = datetime.strptime(date_str, "%Y-%m-%d")
        return int(day.replace(hour=start_hour or 0).timestamp())
    if start_hour is not None:
        target = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        return int(max(target, now).timestamp())
    return int(now.timestamp())


def cmd_freely(args, config):
    stadiumid = None
    if args.stadium:
        stadiumid = resolve_stadium(args.stadium, _search(config))

    begin = compute_begin(args.date, args.start)
    collected, quantity = [], None
    for page in range(1, args.pages + 1):
        result = freely.list_activities(
            cityid=config.get("cityid", 75), stadiumid=stadiumid,
            begin=begin, page=page, size=20,
            appsessionid=config.get("appsessionid", ""))
        if "error" in result:
            return envelope(False, f"查询畅打失败：{result['error']}", {})
        quantity = result["quantity"]
        collected.extend(result["activities"])
        if len(result["activities"]) < 20:
            break

    items = freely.filter_by_date(collected, args.date)
    items = freely.filter_by_hours(items, args.start, args.end)
    return envelope(True, summarize_freely(items),
                    {"activities": items, "total_in_city": quantity,
                     "scanned": len(collected)})


def describe_find(found: list, total, date: str, window: str) -> str:
    """find 的摘要。

    接口的 allStadiumSize 有时返回 0 而列表非空 —— 不能报「筛出 6 个（全市共 0 个）」
    这种自相矛盾的话，调用方会被带偏。总数不可信就干脆不提。
    """
    if not found:
        return f"{date} {window} 没有筛出候选场馆。"

    head = f"{date} {window} 筛出 {len(found)} 个候选场馆"
    if isinstance(total, int) and total > len(found):
        head += f"（全市共 {total} 个）"
    bits = [head]

    nearest = min((s for s in found if s.get("distance_km") is not None),
                  key=lambda s: s["distance_km"], default=None)
    cheapest = min((s for s in found if s.get("min_price") is not None),
                   key=lambda s: s["min_price"], default=None)
    if nearest:
        bits.append(f"最近 {nearest['name']} {nearest['distance_km']}km")
    if cheapest:
        bits.append(f"最低价 {cheapest['name']} ¥{cheapest['min_price']}")
    bits.append("这是候选清单，具体哪个钟点空着要再用 slots 确认")
    return "；".join(bits) + "。"


def cmd_find(args, config):
    date = args.date or (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    result = sign.find_stadiums(
        cityid=config.get("cityid", 75), date_ts=sign.date_to_ts(date),
        start_hour=args.start, end_hour=args.end,
        userid=config.get("userid", 0), sort=args.sort,
        page=1, size=args.size, appsessionid=config.get("appsessionid", ""))
    if "error" in result:
        return envelope(False, f"搜索失败：{result['error']}", {})

    found = result["stadiums"]
    window = (f"{args.start:02d}:00-{args.end:02d}:00"
              if args.start is not None and args.end is not None else "全天")
    return envelope(True, describe_find(found, result["total"], date, window),
                    {"stadiums": found, "date": date, "window": window,
                     "total_in_city": result["total"]})


def cmd_watch(args, config):
    """盯场。命中即通知，可选自动锁单。"""
    import engine
    import notify

    search = _search(config)
    resolved, unresolved = {}, []
    for token in args.stadium:
        try:
            resolved[resolve_stadium(token, search)] = token
        except (StadiumNotFound, AmbiguousStadium) as e:
            unresolved.append(str(e))
    if not resolved:
        return envelope(False, "；".join(unresolved) or "没有可盯的场馆",
                        {"errors": unresolved})

    dates = _dates(args)
    watch_config = {
        "stadium_priority": list(resolved),
        # 只有用户输的是名字才当名字用；输 id 就留空，让引擎用接口返回的真实馆名，
        # 否则通知里只剩一串数字
        "stadiums": {sid: token for sid, token in resolved.items()
                     if not str(token).isdigit()},
        "target_start": args.start, "target_end": args.end,
        "start_date": dates[0], "end_date": dates[-1],
        "poll_interval": args.interval,
        "fail_alert_rounds": config.get("fail_alert_rounds", 3),
    }

    booked, rounds = [], {"n": 0}

    def fetch(stadiumid, date):
        raw = sign.get_schedule(stadiumid, sign.date_to_ts(date),
                                config.get("userid", 0),
                                config.get("appsessionid", ""))
        return sign.parse_schedule(raw, args.start, args.end)

    def on_new_slots(slots):
        if not args.book or booked:
            return None
        note, order = try_book_slot(slots[0], config)
        if order:
            booked.append(order)
        return note

    def send(text):
        print(text, flush=True)
        return notify.send_text(config.get("feishu_webhook", ""), text,
                                config.get("feishu_secret", ""))

    def should_stop():
        if booked:
            return True                     # 锁到一单就收工，不给账号堆挂单
        return bool(args.rounds) and rounds["n"] >= args.rounds

    def log(message):
        if message.startswith("---"):
            rounds["n"] += 1
        print(message, flush=True)

    engine.run_monitor(lambda: dict(watch_config), fetch, send, log,
                       should_stop, time.sleep, on_new_slots=on_new_slots)

    return envelope(True,
                    ("盯场结束，已锁单。" if booked else "盯场结束，未锁单。")
                    + (" " + "；".join(unresolved) if unresolved else ""),
                    {"booked": booked, "rounds": rounds["n"]})


def try_book_slot(slot: dict, config: dict) -> tuple:
    """盯到空场后锁单。返回 (给通知附加的说明, 订单或 None)。

    三道闸：`--book` 默认关（在调用方）；单次会话最多 1 单（在调用方）；
    有挂单一律不锁 —— 平台不允许，硬锁只会白白失败。
    """
    userid = config.get("userid", 0)
    if not userid:
        return "⚠️ 缺 userid（先跑 `ydb login`），只做了通知", None

    pending = sign.pending_field_orders(userid, config.get("appsessionid", ""))
    if pending:
        ids = "、".join(str(o.get("orderid")) for o in pending)
        return (f"⚠️ 有待处理订单（{ids}）挡路，平台不允许再锁单，只做了通知", None)

    order = sign.book_field(slot["stadiumid"], sign.date_to_ts(slot["date"]),
                            userid, slot["fieldid"], slot["timePoint"], 1,
                            config.get("appsessionid", ""))
    if "error" in order:
        return f"⚠️ 锁单失败：{order['error']}", None
    return (f"🤖 已锁单 {slot['stadium']} {slot['date']} {slot['time']} "
            f"{slot['field']} ¥{order.get('realExpense')}\n"
            f"订单 {order.get('orderid')} —— 约 11 分钟内去 App 付款，"
            f"否则作废且仍会挡住新单", order)


# 凭据录入的字段表：(键, 提示, 是否敏感)
CREDENTIAL_FIELDS = (
    ("secret_api", "secret_api（查排期/搜场馆必需）", True),
    ("secret_ydb", "secret_ydb（查畅打才需要）", True),
    ("mobile", "mobile 手机号（锁单才需要）", False),
    ("password", "password 密码（锁单才需要）", True),
    ("feishu_webhook", "feishu_webhook（盯场推送才需要）", True),
)


def collect_credentials(current: dict, ask, ask_secret) -> dict:
    """交互式收集凭据，返回要写入的字段。空输入 = 不改。

    敏感字段走不回显的通道，且提示里绝不回显已存的值 —— 密码和密钥不该出现在
    终端、对话或 shell history 里。
    """
    updates = {}
    for key, prompt, sensitive in CREDENTIAL_FIELDS:
        state = "已设置" if current.get(key) else "未设置"
        if sensitive:
            answer = ask_secret(f"{prompt} [{state}，留空不改]: ")
        else:
            answer = ask(f"{prompt} [{state}，留空不改]: ",
                         current.get(key) or "")
        if answer:
            updates[key] = answer
    return updates


def cmd_credentials(args, config):
    import getpass

    updates = collect_credentials(
        config,
        ask=lambda prompt, default: input(prompt).strip(),
        ask_secret=lambda prompt: getpass.getpass(prompt).strip())
    if not updates:
        return envelope(True, "没有改动。", {"updated": []})

    config.update(updates)
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    sign.reload_secrets()
    return envelope(True, f"已更新 {len(updates)} 个字段并写入 config.json。",
                    {"updated": sorted(updates)})


def cmd_orders(args, config):
    orders = sign.pending_field_orders(config.get("userid", 0),
                                       config.get("appsessionid", ""))
    if not orders:
        summary = "没有待处理的场地订单。"
    else:
        summary = (f"有 {len(orders)} 笔待处理场地订单挡路，"
                   f"在处理掉之前无法锁新单。")
    return envelope(True, summary, {"orders": orders})


def cmd_login(args, config):
    if not config.get("mobile") or not config.get("password"):
        return envelope(False, "config.json 里没有 mobile / password", {})
    result = sign.login(config["mobile"], config["password"])
    if "error" in result:
        return envelope(False, f"登录失败：{result['error']}", {})
    config["userid"] = result["userid"]
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    return envelope(True, f"登录成功，userid={result['userid']} 已保存。",
                    {"userid": result["userid"]})


def cmd_book(args, config):
    if not args.yes:
        return envelope(False, "锁单会在你账号上产生真实订单，需要加 --yes 确认。", {})
    userid = config.get("userid", 0)
    if not userid:
        return envelope(False, "还没有 userid，先跑 `ydb login`。", {})

    pending = sign.pending_field_orders(userid, config.get("appsessionid", ""))
    if pending:
        ids = "、".join(str(o.get("orderid")) for o in pending)
        return envelope(False,
                        f"有待处理订单（{ids}）挡路，平台不允许再锁单。"
                        f"先付款或 `ydb cancel <orderid>`。",
                        {"pending": pending})

    stadiumid = resolve_stadium(args.stadium, _search(config))
    date_ts = sign.date_to_ts(args.date)
    raw = sign.get_schedule(stadiumid, date_ts, userid,
                            config.get("appsessionid", ""))
    if "error" in raw:
        return envelope(False, f"查排期失败：{raw['error']}", {})

    candidates = [(f["name"], s) for f in raw.get("fieldList", [])
                  for s in f.get("shedule", [])
                  if s.get("timePoint") == args.hour and s.get("status") == "0"]
    if not candidates:
        return envelope(False,
                        f"{args.date} {args.hour:02d}:00 没有可订场地。", {})

    field_name, slot = candidates[0]
    order = sign.book_field(stadiumid, date_ts, userid, slot["fieldid"],
                            args.hour, args.hours,
                            config.get("appsessionid", ""))
    if "error" in order:
        return envelope(False, f"锁单失败：{order['error']}", {})
    return envelope(True,
                    f"已锁 {raw.get('stadiumName')} {args.date} "
                    f"{args.hour:02d}:00-{args.hour + args.hours:02d}:00 "
                    f"{field_name}，¥{order.get('realExpense')}。"
                    f"约 11 分钟内去 App 付款，否则订单作废但仍会挡住新单。",
                    {"order": order, "field": field_name})


def cmd_cancel(args, config):
    result = sign.cancel_order(args.orderid, config.get("userid", 0),
                              config.get("appsessionid", ""))
    if "error" in result:
        return envelope(False, f"取消失败：{result['error']}", {})
    return envelope(True, f"订单 {args.orderid} 已取消。", {"result": result})


# ---- 入口 ----

def build_parser():
    p = argparse.ArgumentParser(prog="ydb", description="韵动吧场地助手")
    p.add_argument("--json", action="store_true", help="只输出 JSON")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="按名字搜场馆")
    s.add_argument("keyword")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("slots", help="查空场")
    s.add_argument("stadium", nargs="+", help="场馆 id 或名字")
    s.add_argument("--date", help="指定日期 YYYY-MM-DD")
    s.add_argument("--days", type=int, default=3, help="不指定日期时查未来几天")
    s.add_argument("--from", dest="start", type=int, default=0, help="起始整点")
    s.add_argument("--to", dest="end", type=int, default=24, help="结束整点（不含）")
    s.set_defaults(func=cmd_slots)

    s = sub.add_parser("freely", help="查畅打活动")
    s.add_argument("--stadium", help="限定场馆")
    s.add_argument("--date", help="限定日期 YYYY-MM-DD")
    s.add_argument("--from", dest="start", type=int, help="起始整点")
    s.add_argument("--to", dest="end", type=int, help="结束整点")
    s.add_argument("--pages", type=int, default=3, help="扫描页数")
    s.set_defaults(func=cmd_freely)

    s = sub.add_parser("find", help="按日期+时段筛候选场馆")
    s.add_argument("--date", help="日期 YYYY-MM-DD，默认明天")
    s.add_argument("--from", dest="start", type=int, help="起始整点")
    s.add_argument("--to", dest="end", type=int, help="结束整点")
    s.add_argument("--sort", default="distance",
                   choices=("distance", "minPrice", "collect"), help="排序方式")
    s.add_argument("--size", type=int, default=20, help="返回条数")
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("watch", help="盯场（长跑），可选自动锁单")
    s.add_argument("stadium", nargs="+", help="场馆 id 或名字")
    s.add_argument("--date", help="指定日期")
    s.add_argument("--days", type=int, default=7, help="不指定日期时盯未来几天")
    s.add_argument("--from", dest="start", type=int, default=18)
    s.add_argument("--to", dest="end", type=int, default=22)
    s.add_argument("--interval", type=int, default=60, help="轮询间隔秒")
    s.add_argument("--rounds", type=int, default=0, help="跑几轮后退出，0=不限")
    s.add_argument("--book", action="store_true",
                   help="盯到空场自动锁单（真实订单，单次会话最多 1 单）")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("orders", help="待处理订单")
    s.set_defaults(func=cmd_orders)

    s = sub.add_parser("credentials", help="交互式录入凭据（不回显、不进 history）")
    s.set_defaults(func=cmd_credentials)

    s = sub.add_parser("login", help="用手机号密码登录，保存 userid")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("book", help="锁单（真实订单，需 --yes）")
    s.add_argument("stadium")
    s.add_argument("date")
    s.add_argument("hour", type=int)
    s.add_argument("--hours", type=int, default=1)
    s.add_argument("--yes", action="store_true", help="确认要真实下单")
    s.set_defaults(func=cmd_book)

    s = sub.add_parser("cancel", help="取消订单")
    s.add_argument("orderid")
    s.set_defaults(func=cmd_cancel)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    config = load_config()
    setup(config)
    try:
        result = args.func(args, config)
    except (StadiumNotFound, AmbiguousStadium) as e:
        result = envelope(False, str(e), {})

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["summary"])
        if result["data"]:
            print(json.dumps(result["data"], ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
