"""监控引擎：单轮轮询 与 主循环。

网络、时钟、配置读取全部注入，所以这些行为可以真测而不是靠肉眼：
  · 查询失败的场地不进入「已轮询范围」（否则去重状态会被错误清掉）
  · 连续全失败要告警，且只告警一次
  · 每轮重新读配置
"""
import engine


def parsed(target=(), nearest=None, name="馆"):
    return {"stadiumName": name,
            "target": [{"field": f"场{tp}", "fieldid": f"f{tp}",
                        "time": f"{tp:02d}:00", "timePoint": tp, "price": 400}
                       for tp in target],
            "nearest": nearest,
            "all_count": len(target)}


# ---- 单轮轮询 ----

def test_slots_are_enriched_with_stadium_and_date():
    result = engine.poll_round(["1128"], {"1128": "唛恩东馆"}, ["2026-08-15"],
                               lambda sid, d: parsed(target=[18]))

    assert len(result.slots) == 1
    slot = result.slots[0]
    assert slot["stadiumid"] == "1128"
    assert slot["stadium"] == "唛恩东馆"
    assert slot["date"] == "2026-08-15"
    assert slot["timePoint"] == 18


def test_slots_follow_priority_order_not_dict_order():
    stadiums = {"1128": "先建的", "2000": "后建的"}
    result = engine.poll_round(["2000", "1128"], stadiums, ["2026-08-15"],
                               lambda sid, d: parsed(target=[18]))

    assert [s["stadiumid"] for s in result.slots] == ["2000", "1128"]


def test_every_polled_stadium_date_pair_is_recorded():
    result = engine.poll_round(["1128", "2000"], {}, ["2026-08-15", "2026-08-16"],
                               lambda sid, d: parsed())

    assert result.polled_scopes == {
        ("1128", "2026-08-15"), ("1128", "2026-08-16"),
        ("2000", "2026-08-15"), ("2000", "2026-08-16"),
    }


def test_failed_query_is_recorded_and_its_scope_is_not_marked_polled():
    def fetch(sid, d):
        return {"error": "签名错误"} if sid == "1128" else parsed(target=[18])

    result = engine.poll_round(["1128", "2000"], {"1128": "坏的", "2000": "好的"},
                               ["2026-08-15"], fetch)

    assert result.polled_scopes == {("2000", "2026-08-15")}
    assert result.failures == [("坏的", "2026-08-15", "签名错误")]
    assert [s["stadiumid"] for s in result.slots] == ["2000"]


def test_nearest_slot_is_reported_as_a_miss_not_as_a_free_slot():
    near = {"field": "场16", "fieldid": "f16", "time": "16:00",
            "timePoint": 16, "price": 400}
    result = engine.poll_round(["1128"], {"1128": "馆"}, ["2026-08-15"],
                               lambda sid, d: parsed(nearest=near))

    assert result.slots == []
    assert result.misses == [("馆", "2026-08-15", near)]


def test_unnamed_stadium_uses_the_name_from_the_response():
    # 配置里没写名字时，用接口返回的 stadiumName —— 它本来就在响应里，不额外花请求。
    # 否则通知里只有一串 id，人看不出是哪个馆。
    result = engine.poll_round(["9999"], {}, ["2026-08-15"],
                               lambda sid, d: parsed(target=[18], name="真名网球馆"))

    assert result.slots[0]["stadium"] == "真名网球馆"


def test_falls_back_to_the_id_when_the_response_has_no_name_either():
    result = engine.poll_round(["9999"], {}, ["2026-08-15"],
                               lambda sid, d: parsed(target=[18], name=None))

    assert "9999" in result.slots[0]["stadium"]


def test_configured_name_wins_over_the_response_name():
    result = engine.poll_round(["1128"], {"1128": "我起的名"}, ["2026-08-15"],
                               lambda sid, d: parsed(target=[18], name="官方名"))

    assert result.slots[0]["stadium"] == "我起的名"


# ---- 日期范围 ----

def test_date_range_is_inclusive_of_both_ends():
    assert engine.gen_dates("2026-08-15", "2026-08-17") == \
        ["2026-08-15", "2026-08-16", "2026-08-17"]


def test_single_day_range():
    assert engine.gen_dates("2026-08-15", "2026-08-15") == ["2026-08-15"]


def test_end_before_start_collapses_to_start_day():
    assert engine.gen_dates("2026-08-15", "2026-08-10") == ["2026-08-15"]


# ---- 主循环 ----

class Harness:
    """把循环的外部依赖全部替换成可观察的假实现。"""

    def __init__(self, fetch, rounds=1, config=None):
        self.fetch = fetch
        self.sent = []
        self.logs = []
        self.config_reads = 0
        self._rounds = rounds
        self._config = config or {
            "stadium_priority": ["1128"], "stadiums": {"1128": "馆"},
            "target_start": 18, "target_end": 21,
            "start_date": "2026-08-15", "end_date": "2026-08-15",
            "poll_interval": 30, "fail_alert_rounds": 3,
        }

    def load_config(self):
        self.config_reads += 1
        return dict(self._config)

    def should_stop(self):
        # 按「已开始的轮数」判断，而不是按被调用次数 —— 分片睡眠每秒都会问一次
        return self.config_reads >= self._rounds

    def send(self, text):
        self.sent.append(text)
        return True, "ok"

    def log(self, msg):
        self.logs.append(msg)

    def run(self):
        engine.run_monitor(self.load_config, self.fetch, self.send,
                           self.log, self.should_stop, sleep=lambda s: None)


def test_new_free_slot_triggers_one_notification():
    h = Harness(lambda sid, d: parsed(target=[18]), rounds=1)

    h.run()

    assert len(h.sent) == 1
    assert "18:00" in h.sent[0]


def test_same_slot_across_rounds_notifies_only_once():
    h = Harness(lambda sid, d: parsed(target=[18]), rounds=5)

    h.run()

    assert len(h.sent) == 1


def test_no_free_slot_sends_nothing():
    h = Harness(lambda sid, d: parsed(), rounds=3)

    h.run()

    assert h.sent == []


def test_config_is_reread_every_round():
    h = Harness(lambda sid, d: parsed(), rounds=4)

    h.run()

    assert h.config_reads == 4


def test_alert_fires_after_the_configured_number_of_failing_rounds():
    h = Harness(lambda sid, d: {"error": "签名错误"}, rounds=3)

    h.run()

    assert len(h.sent) == 1
    assert "签名错误" in h.sent[0]


def test_alert_does_not_fire_before_the_threshold():
    h = Harness(lambda sid, d: {"error": "签名错误"}, rounds=2)

    h.run()

    assert h.sent == []


def test_alert_is_not_repeated_every_round_while_still_broken():
    h = Harness(lambda sid, d: {"error": "签名错误"}, rounds=10)

    h.run()

    assert len(h.sent) == 1


def test_alert_can_fire_again_after_recovering():
    state = {"round": 0}

    def fetch(sid, d):
        state["round"] += 1
        # 3 轮失败 -> 告警; 第 4 轮成功 -> 复位; 再 3 轮失败 -> 再告警
        if state["round"] == 4:
            return parsed()
        return {"error": "签名错误"}

    h = Harness(fetch, rounds=7)
    h.run()

    assert len(h.sent) == 2


def test_partial_failure_does_not_count_as_a_failing_round():
    def fetch(sid, d):
        return {"error": "boom"} if sid == "1128" else parsed()

    h = Harness(fetch, rounds=10, config={
        "stadium_priority": ["1128", "2000"],
        "stadiums": {"1128": "坏的", "2000": "好的"},
        "target_start": 18, "target_end": 21,
        "start_date": "2026-08-15", "end_date": "2026-08-15",
        "poll_interval": 30, "fail_alert_rounds": 3,
    })
    h.run()

    assert h.sent == []


def test_loop_exits_without_polling_when_told_to_stop_immediately():
    h = Harness(lambda sid, d: parsed(target=[18]), rounds=0)

    h.run()

    assert h.sent == []
    assert h.config_reads == 0


# ---- 分层日期轮询 ----
# 近期日期每轮都查；远期本来就全空（实测 +6 天之后全是满格未预订状态），
# 每轮都查纯属浪费速率预算。

def test_first_round_scans_every_date():
    dates = [f"2026-08-{d:02d}" for d in range(19, 33 - 1)]

    assert engine.dates_due(dates, 1) == dates


def test_second_round_only_scans_the_near_days():
    dates = [f"2026-08-{d:02d}" for d in range(19, 33 - 1)]

    assert engine.dates_due(dates, 2) == dates[:3]


def test_mid_range_comes_back_every_fifth_round():
    dates = [f"2026-08-{d:02d}" for d in range(19, 33 - 1)]

    assert engine.dates_due(dates, 6) == dates[:7]
    assert engine.dates_due(dates, 5) == dates[:3]


def test_far_range_comes_back_every_thirtieth_round():
    dates = [f"2026-08-{d:02d}" for d in range(19, 33 - 1)]

    assert engine.dates_due(dates, 31) == dates


def test_short_range_is_always_fully_scanned():
    dates = ["2026-08-19", "2026-08-20"]

    assert engine.dates_due(dates, 7) == dates


def test_no_dates_yields_nothing():
    assert engine.dates_due([], 3) == []


# ---- 自适应退避 ----

def test_no_failures_means_the_base_interval():
    assert engine.backoff_interval(30, 0) == 30


def test_each_consecutive_failure_doubles_the_interval():
    assert engine.backoff_interval(30, 1) == 60
    assert engine.backoff_interval(30, 2) == 120
    assert engine.backoff_interval(30, 3) == 240


def test_backoff_is_capped():
    assert engine.backoff_interval(30, 20, cap=1800) == 1800


def test_backoff_cap_is_respected_even_below_base():
    assert engine.backoff_interval(30, 5, cap=100) == 100


# ---- 错误分类 ----
# 限流大概率既不是签名错误也不是登录错误 —— 那类必须单独归类并告警，
# 否则第一次撞上限流会被当成普通失败静默重试。

def test_signature_errors_are_recognised():
    assert engine.classify_error("签名错误") == engine.ERR_SIGNATURE
    assert engine.classify_error("Api授权失败，请检查签名") == engine.ERR_SIGNATURE
    assert engine.classify_error("参数[sign]不能为空") == engine.ERR_SIGNATURE


def test_session_errors_are_recognised():
    assert engine.classify_error("用户未登录") == engine.ERR_SESSION
    assert engine.classify_error("会话失效，请重新登录") == engine.ERR_SESSION


def test_network_errors_are_recognised():
    assert engine.classify_error("HTTPSConnectionPool: Read timed out") \
        == engine.ERR_NETWORK
    assert engine.classify_error("Connection aborted") == engine.ERR_NETWORK


def test_anything_else_is_unknown_and_therefore_alarming():
    assert engine.classify_error("请求过于频繁") == engine.ERR_UNKNOWN
    assert engine.classify_error("429 Too Many Requests") == engine.ERR_UNKNOWN
    assert engine.classify_error("") == engine.ERR_UNKNOWN


# ---- 主循环要真的用上分层与退避 ----

def test_loop_only_fetches_the_due_dates_after_the_first_round():
    fetched = []

    def fetch(sid, date):
        fetched.append((sid, date))
        return parsed()

    h = Harness(fetch, rounds=2, config={
        "stadium_priority": ["1128"], "stadiums": {"1128": "馆"},
        "target_start": 18, "target_end": 21,
        "start_date": "2026-08-19", "end_date": "2026-09-01",   # 14 天
        "poll_interval": 30, "fail_alert_rounds": 3,
    })
    h.run()

    # 第 1 轮全扫 14 天，第 2 轮只扫近 3 天
    assert len(fetched) == 14 + 3


def test_loop_backs_off_after_repeated_total_failures():
    waits = []

    h = Harness(lambda sid, d: {"error": "签名错误"}, rounds=4)
    engine.run_monitor(h.load_config, h.fetch, h.send, h.log, h.should_stop,
                       sleep=lambda s: waits.append(s))

    # 分片睡眠每次睡 1 秒，所以看总秒数：退避后每轮等待应逐轮变长
    assert len(waits) > 30      # 基础 30s + 退避后更长


def test_unknown_errors_are_marked_in_the_log():
    h = Harness(lambda sid, d: {"error": "请求过于频繁"}, rounds=1)

    h.run()

    joined = "\n".join(h.logs)
    assert "请求过于频繁" in joined
    assert "未知" in joined or "⚠️" in joined


# ---- 新空场钩子（自动锁单靠它拿到结构化数据）----

def test_hook_receives_the_structured_new_slots():
    seen = []

    def on_new(slots):
        seen.append(slots)
        return None

    h = Harness(lambda sid, d: parsed(target=[18, 19]), rounds=1)
    engine.run_monitor(h.load_config, h.fetch, h.send, h.log, h.should_stop,
                       sleep=lambda s: None, on_new_slots=on_new)

    assert len(seen) == 1
    assert [s["timePoint"] for s in seen[0]] == [18, 19]
    assert seen[0][0]["stadiumid"] == "1128"      # 已带场馆与日期


def test_hook_text_is_appended_to_the_notification():
    h = Harness(lambda sid, d: parsed(target=[18]), rounds=1)
    engine.run_monitor(h.load_config, h.fetch, h.send, h.log, h.should_stop,
                       sleep=lambda s: None,
                       on_new_slots=lambda slots: "🤖 已锁单 12345")

    assert "🤖 已锁单 12345" in h.sent[0]


def test_hook_is_not_called_when_nothing_is_new():
    calls = []

    h = Harness(lambda sid, d: parsed(), rounds=3)
    engine.run_monitor(h.load_config, h.fetch, h.send, h.log, h.should_stop,
                       sleep=lambda s: None,
                       on_new_slots=lambda slots: calls.append(slots))

    assert calls == []


def test_hook_failure_does_not_kill_the_loop():
    def boom(slots):
        raise RuntimeError("下单接口炸了")

    h = Harness(lambda sid, d: parsed(target=[18]), rounds=1)
    engine.run_monitor(h.load_config, h.fetch, h.send, h.log, h.should_stop,
                       sleep=lambda s: None, on_new_slots=boom)

    # 通知照发，钩子的异常记进日志
    assert len(h.sent) == 1
    assert any("下单接口炸了" in m for m in h.logs)
