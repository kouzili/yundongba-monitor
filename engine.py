#!/usr/bin/env python3
"""监控引擎 —— 单轮轮询与主循环。

外部依赖（网络、时钟、配置读取、推送）全部由调用方注入，所以这里的行为可以被
测试直接驱动。app.py 只负责把真实实现接上去。
"""
from collections import namedtuple
from datetime import datetime, timedelta

import dedup
import notify

RoundResult = namedtuple("RoundResult", "slots polled_scopes failures misses")

DEFAULT_FAIL_ALERT_ROUNDS = 3


def gen_dates(start_date: str, end_date: str) -> list:
    """生成日期字符串列表（含首尾）。结束早于开始时退化为只查开始那天。"""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if end < start:
        end = start
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range((end - start).days + 1)]


NEAR_DAYS, MID_DAYS = 3, 7
MID_EVERY, FAR_EVERY = 5, 30
BACKOFF_CAP = 1800

ERR_SIGNATURE = "signature"
ERR_SESSION = "session"
ERR_NETWORK = "network"
ERR_UNKNOWN = "unknown"


def dates_due(dates: list, round_num: int, near: int = NEAR_DAYS,
              mid: int = MID_DAYS, mid_every: int = MID_EVERY,
              far_every: int = FAR_EVERY) -> list:
    """这一轮该查哪些日期。dates 从近到远排序。

    近期每轮查；中期每 mid_every 轮；远期每 far_every 轮。远期日期本来就全空
    （实测 +6 天之后全是满格未预订状态），每轮都查纯属浪费速率预算。
    第 1 轮全查，先建立完整基线。
    """
    due = []
    for index, date in enumerate(dates):
        if index < near:
            due.append(date)
        elif index < mid:
            if round_num % mid_every == 1:
                due.append(date)
        elif round_num % far_every == 1:
            due.append(date)
    return due


def backoff_interval(base: float, consecutive_failures: int,
                     cap: float = BACKOFF_CAP) -> float:
    """失败越多间隔越长，上限 cap。持续报错时别再按原节奏猛敲。"""
    if consecutive_failures <= 0:
        return base
    return min(base * 2 ** consecutive_failures, cap)


def classify_error(message: str) -> str:
    """给错误分类。

    限流大概率既不是签名错误也不是登录错误 —— 那一类必须能被单独认出来，
    否则第一次撞上限流会被当成普通失败静默重试。
    """
    text = str(message or "")
    lowered = text.lower()
    if "签名" in text or "sign" in lowered or "授权" in text:
        return ERR_SIGNATURE
    if "登录" in text or "会话" in text or "session" in lowered:
        return ERR_SESSION
    if any(k in lowered for k in ("timed out", "timeout", "connection",
                                 "httpsconnectionpool", "resolve", "refused")):
        return ERR_NETWORK
    return ERR_UNKNOWN


def poll_round(priority: list, stadiums: dict, dates: list, fetch) -> RoundResult:
    """把每个「场地 × 日期」查一遍。

    fetch(stadiumid, date) 返回 sign.parse_schedule 的结果，或 {"error": msg}。

    slots 按优先级顺序排列（不是配置字典的顺序），推送时的分组顺序依赖于此。
    polled_scopes 只包含**成功**查到的范围 —— 去重状态机靠它避免把查询失败
    误当成「空场消失」。
    """
    slots, polled, failures, misses = [], set(), [], []
    for stadiumid in priority:
        sid = str(stadiumid)
        for date in dates:
            result = fetch(sid, date)
            # 名字优先用配置里的；没配就用接口返回的 stadiumName（本来就在响应里，
            # 不额外花请求）；都没有才退化成 id
            name = (stadiums.get(sid) or result.get("stadiumName")
                    or f"场地{sid}")
            if "error" in result:
                failures.append((stadiums.get(sid) or f"场地{sid}", date,
                                 result["error"]))
                continue
            polled.add((sid, date))
            for slot in result.get("target", []):
                slots.append({**slot, "stadium": name, "stadiumid": sid, "date": date})
            if not result.get("target") and result.get("nearest"):
                misses.append((name, date, result["nearest"]))
    return RoundResult(slots, polled, failures, misses)


def _sleep_interval(seconds, should_stop, sleep):
    """分片睡眠，让「停止」不用等一整轮。"""
    for _ in range(max(1, int(seconds))):
        if should_stop():
            return
        sleep(1)


def _describe_failures(failures: list) -> str:
    return "\n".join(f"{name} {date}: {error}" for name, date, error in failures)


def run_monitor(load_config, fetch, send, log, should_stop, sleep,
                on_new_slots=None) -> None:
    """主循环。

    每轮重新读配置，所以改配置不需要重启监控。

    连续「整轮全部失败」达到阈值时推一条告警，且在恢复之前不再重复推 ——
    无人值守下悄悄不工作比报错更糟，但每 30s 报一次同样没人看。
    """
    tracker = dedup.SlotTracker()
    consecutive_failures = 0
    alerted = False
    round_num = 0

    while not should_stop():
        round_num += 1
        cfg = load_config()
        priority = [str(s) for s in cfg.get("stadium_priority", [])]
        stadiums = cfg.get("stadiums", {})
        dates = gen_dates(cfg["start_date"], cfg["end_date"])
        start_hour, end_hour = cfg["target_start"], cfg["target_end"]
        threshold = cfg.get("fail_alert_rounds", DEFAULT_FAIL_ALERT_ROUNDS)

        # 分层：近期每轮查，远期隔若干轮查一次，省速率预算
        due = dates_due(dates, round_num)
        log(f"--- 第{round_num}轮 {len(priority)} 场地 × {len(due)}/{len(dates)} 天 "
            f"{start_hour:02d}:00-{end_hour:02d}:00 ---")

        # 配置里被移除的场地/日期，其去重状态一并丢弃。用全部日期而不是本轮
        # 应查的日期 —— 否则没轮到的日期状态会被误清，下轮重推。
        tracker.retain_scopes({(s, d) for s in priority for d in dates})

        result = poll_round(priority, stadiums, due, fetch)

        for name, date, error in result.failures:
            kind = classify_error(error)
            mark = "⚠️ 未知错误（可能是限流）" if kind == ERR_UNKNOWN else "❌"
            log(f"  {name} {date} {mark} {error}")
        for name, date, near in result.misses:
            log(f"  {name} {date} ⚪ 全满 | 最近 {near['field']} "
                f"{near['time']} ¥{near['price']}")

        # 整轮无一成功才算失败轮；部分失败不算，否则单个坏场地会一直触发告警
        if result.failures and not result.polled_scopes:
            consecutive_failures += 1
            if consecutive_failures >= threshold and not alerted:
                kinds = {classify_error(e) for _, _, e in result.failures}
                hint = "（含未知错误，可能是限流）" if ERR_UNKNOWN in kinds else ""
                send(f"⚠️ 监控连续 {consecutive_failures} 轮全部失败{hint}\n\n"
                     + _describe_failures(result.failures))
                alerted = True
                log(f"  ⚠️ 连续 {consecutive_failures} 轮全失败，已告警")
        else:
            if alerted:
                log("  ✅ 查询已恢复")
            consecutive_failures = 0
            alerted = False

        new_slots = tracker.new_slots(result.slots, result.polled_scopes)
        for slot in new_slots:
            log(f"  🟢 {slot['stadium']} {slot['date']} {slot['time']} "
                f"{slot['field']} ¥{slot['price']}")
        if new_slots:
            text = f"🎾 发现 {len(new_slots)} 个空场\n\n{notify.format_slots(new_slots)}"
            if on_new_slots:
                # 钩子拿到的是结构化空场（自动锁单靠它）。钩子炸了不能拖垮循环 ——
                # 通知照发，异常记日志。
                try:
                    extra = on_new_slots(new_slots)
                    if extra:
                        text += f"\n\n{extra}"
                except Exception as e:
                    log(f"  ❌ 新空场钩子异常: {e}")
            ok, message = send(text)
            if not ok:
                log(f"  ❌ 推送失败: {message}")

        # 持续报错时拉长间隔，别按原节奏猛敲
        interval = backoff_interval(cfg.get("poll_interval", 30),
                                    consecutive_failures)
        if interval != cfg.get("poll_interval", 30):
            log(f"  退避中：本轮等待 {int(interval)}s")
        _sleep_interval(interval, should_stop, sleep)
