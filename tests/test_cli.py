"""CLI 的纯逻辑：场馆名解析 与 摘要生成。

Hermes 传进来的是人话（「穿岳」「DUECE 徐泾」），要变成 stadiumid。
歧义时绝不猜 —— 猜错会让用户盯着错的场馆。
"""
import pytest

import cli


def fake_search(results):
    return lambda keyword: results


# ---- 场馆解析 ----

def test_numeric_token_is_used_as_an_id_directly():
    called = []

    def search(keyword):
        called.append(keyword)
        return []

    assert cli.resolve_stadium("1189", search) == "1189"
    assert called == []          # 纯数字不该浪费一次搜索请求


def test_single_match_resolves_to_its_id():
    search = fake_search([{"stadiumid": "1189", "name": "DUECE TENNIS寻网网球·徐泾店"}])

    assert cli.resolve_stadium("徐泾", search) == "1189"


def test_ambiguous_name_raises_with_all_candidates():
    search = fake_search([
        {"stadiumid": "1189", "name": "DUECE TENNIS寻网网球·徐泾店"},
        {"stadiumid": "614", "name": "酷享网球•徐泾北城店"},
    ])

    with pytest.raises(cli.AmbiguousStadium) as exc:
        cli.resolve_stadium("徐泾", search)

    assert "1189" in str(exc.value)
    assert "614" in str(exc.value)


def test_exact_name_match_wins_over_ambiguity():
    search = fake_search([
        {"stadiumid": "1189", "name": "DUECE TENNIS寻网网球·徐泾店"},
        {"stadiumid": "614", "name": "酷享网球•徐泾北城店"},
    ])

    assert cli.resolve_stadium("酷享网球•徐泾北城店", search) == "614"


def test_no_match_raises():
    with pytest.raises(cli.StadiumNotFound):
        cli.resolve_stadium("这个场馆不存在", fake_search([]))


# ---- 空场摘要 ----

def slot(stadium="馆A", date="2026-08-24", time="19:00", field="1号", price="200.00"):
    return {"stadium": stadium, "date": date, "time": time,
            "field": field, "price": price}


def test_summary_of_no_slots_says_so_plainly():
    assert "没有" in cli.summarize_slots([], "18:00-21:00")


def test_summary_counts_slots_and_stadiums():
    slots = [slot(stadium="馆A"), slot(stadium="馆A", time="20:00"),
             slot(stadium="馆B")]

    text = cli.summarize_slots(slots, "18:00-21:00")

    assert "3" in text
    assert "馆A" in text and "馆B" in text


def test_summary_mentions_the_queried_window():
    text = cli.summarize_slots([slot()], "18:00-21:00")

    assert "18:00-21:00" in text


def test_summary_lists_dates_when_several_days_are_covered():
    slots = [slot(date="2026-08-24"), slot(date="2026-08-25")]

    text = cli.summarize_slots(slots, "18:00-21:00")

    assert "2026-08-24" in text and "2026-08-25" in text


# ---- 畅打摘要 ----

def activity(stadium="馆A", joined=2, limit=4, full=False, start="19:00"):
    return {"stadium": stadium, "joined": joined, "limit": limit,
            "spots_left": None if limit is None else max(0, limit - joined),
            "full": full, "start": start, "end": "21:00",
            "date": "2026-08-19", "price": "99.00"}


def test_freely_summary_of_nothing():
    assert "没有" in cli.summarize_freely([])


def test_freely_summary_counts_open_spots():
    items = [activity(joined=2, limit=4), activity(joined=4, limit=4, full=True)]

    text = cli.summarize_freely(items)

    assert "2" in text          # 2 场活动
    assert "还有名额" in text or "空位" in text


def test_freely_summary_flags_when_everything_is_full():
    items = [activity(joined=4, limit=4, full=True)]

    text = cli.summarize_freely(items)

    assert "满" in text


# ---- 输出信封 ----

def test_envelope_carries_ok_summary_and_data():
    out = cli.envelope(True, "一切正常", {"slots": []})

    assert out["ok"] is True
    assert out["summary"] == "一切正常"
    assert out["data"] == {"slots": []}


def test_error_envelope_marks_not_ok():
    out = cli.envelope(False, "查不到", {})

    assert out["ok"] is False


# ---- 畅打的 begin 计算 ----
# 接口按时间排序、从 begin 往后返回。begin 传"现在"就得翻很多页才够到晚上，
# 直接把 begin 设成目标窗口起点才对。

def test_begin_uses_the_given_date_and_hour():
    from datetime import datetime

    now = datetime(2026, 8, 19, 10, 0)

    begin = cli.compute_begin("2026-08-24", 19, now)

    assert datetime.fromtimestamp(begin) == datetime(2026, 8, 24, 19, 0)


def test_begin_with_date_only_starts_at_midnight():
    from datetime import datetime

    begin = cli.compute_begin("2026-08-24", None, datetime(2026, 8, 19, 10, 0))

    assert datetime.fromtimestamp(begin) == datetime(2026, 8, 24, 0, 0)


def test_begin_with_hour_only_uses_today():
    from datetime import datetime

    begin = cli.compute_begin(None, 19, datetime(2026, 8, 19, 10, 0))

    assert datetime.fromtimestamp(begin) == datetime(2026, 8, 19, 19, 0)


def test_begin_with_nothing_is_now():
    from datetime import datetime

    now = datetime(2026, 8, 19, 10, 30)

    assert cli.compute_begin(None, None, now) == int(now.timestamp())


def test_begin_never_goes_backwards_past_now_for_hour_only():
    from datetime import datetime

    # 已经 20 点了还问 19 点的场，不该回到过去
    now = datetime(2026, 8, 19, 20, 30)

    begin = cli.compute_begin(None, 19, now)

    assert begin == int(now.timestamp())


# ---- find 的摘要 ----
# 接口的 allStadiumSize 有时返回 0 而列表非空。摘要里不能出现
# 「筛出 6 个（全市共 0 个）」这种自相矛盾 —— 会把调用方带偏。

def found(n):
    return [{"name": f"馆{i}", "distance_km": 1.0 + i, "min_price": 100 + i}
            for i in range(n)]


def test_find_summary_omits_a_zero_total():
    text = cli.describe_find(found(6), 0, "2026-08-22", "08:00-10:00")

    assert "6" in text
    assert "全市共 0" not in text


def test_find_summary_omits_a_total_smaller_than_what_was_returned():
    text = cli.describe_find(found(6), 3, "2026-08-22", "08:00-10:00")

    assert "全市共 3" not in text


def test_find_summary_reports_a_credible_total():
    text = cli.describe_find(found(6), 120, "2026-08-22", "08:00-10:00")

    assert "120" in text


def test_find_summary_names_the_nearest_and_cheapest():
    text = cli.describe_find(found(3), None, "2026-08-22", "08:00-10:00")

    assert "馆0" in text          # 最近也是最便宜的那个


def test_find_summary_of_nothing():
    text = cli.describe_find([], 0, "2026-08-22", "08:00-10:00")

    assert "没有" in text


def test_find_summary_tolerates_missing_distance_and_price():
    text = cli.describe_find([{"name": "馆X", "distance_km": None,
                               "min_price": None}], None, "d", "w")

    assert "馆X" in text or "1" in text


# ---- 自动锁单的闸门 ----
# 这些必须靠单测 —— 用真跑没法验证「该拒绝时确实拒绝了」。

def a_slot():
    return {"stadiumid": "1189", "date": "2026-08-24", "timePoint": 6,
            "time": "06:00", "field": "室内01", "fieldid": 3267,
            "stadium": "DUECE 徐泾"}


def test_booking_is_refused_without_a_userid(monkeypatch):
    booked = []
    monkeypatch.setattr(cli.sign, "book_field",
                        lambda *a, **k: booked.append(a) or {})

    note, order = cli.try_book_slot(a_slot(), {"userid": 0})

    assert order is None
    assert booked == []                  # 压根没调下单接口
    assert "userid" in note


def test_booking_is_refused_when_an_order_is_already_pending(monkeypatch):
    booked = []
    monkeypatch.setattr(cli.sign, "pending_field_orders",
                        lambda *a, **k: [{"orderid": 2089735}])
    monkeypatch.setattr(cli.sign, "book_field",
                        lambda *a, **k: booked.append(a) or {})

    note, order = cli.try_book_slot(a_slot(), {"userid": 999999})

    assert order is None
    assert booked == []
    assert "2089735" in note


def test_booking_proceeds_when_nothing_is_pending(monkeypatch):
    monkeypatch.setattr(cli.sign, "pending_field_orders", lambda *a, **k: [])
    monkeypatch.setattr(cli.sign, "book_field",
                        lambda *a, **k: {"orderid": 999, "realExpense": "120.00"})

    note, order = cli.try_book_slot(a_slot(), {"userid": 999999})

    assert order == {"orderid": 999, "realExpense": "120.00"}
    assert "999" in note
    assert "11 分钟" in note              # 必须提醒付款窗口


def test_a_failed_booking_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(cli.sign, "pending_field_orders", lambda *a, **k: [])
    monkeypatch.setattr(cli.sign, "book_field",
                        lambda *a, **k: {"error": "该时段已被预订"})

    note, order = cli.try_book_slot(a_slot(), {"userid": 999999})

    assert order is None
    assert "该时段已被预订" in note


# ---- 交互式录入凭据 ----
# 密码绝不能出现在对话、命令行参数或 shell history 里 —— 只能交互式输入。

def test_empty_answer_keeps_the_existing_value():
    current = {"secret_api": "OLD", "password": "OLDPW"}

    updates = cli.collect_credentials(current, ask=lambda p, d: "",
                                      ask_secret=lambda p: "")

    assert updates == {}


def test_non_empty_answer_overwrites():
    updates = cli.collect_credentials({}, ask=lambda p, d: "13800000000",
                                      ask_secret=lambda p: "NEWSECRET")

    assert updates["mobile"] == "13800000000"
    assert updates["password"] == "NEWSECRET"


def test_secret_fields_go_through_the_hidden_prompt():
    asked_plain, asked_hidden = [], []

    cli.collect_credentials({}, ask=lambda p, d: asked_plain.append(p) or "",
                            ask_secret=lambda p: asked_hidden.append(p) or "")

    # 密码和两把密钥必须走不回显的通道
    hidden = " ".join(asked_hidden)
    assert "password" in hidden or "密码" in hidden
    assert "secret_api" in hidden
    assert "secret_ydb" in hidden
    # 手机号不敏感，普通输入即可
    assert any("mobile" in p or "手机" in p for p in asked_plain)


def test_existing_values_are_never_echoed_in_the_prompt():
    prompts = []
    current = {"secret_api": "SUPERSECRET", "mobile": "13800000000"}

    cli.collect_credentials(current,
                            ask=lambda p, d: prompts.append((p, d)) or "",
                            ask_secret=lambda p: prompts.append((p, None)) or "")

    flat = str(prompts)
    assert "SUPERSECRET" not in flat
    # 手机号可以显示已存值方便确认，但密钥类一律不显示
    assert not any(d == "SUPERSECRET" for _, d in prompts)
