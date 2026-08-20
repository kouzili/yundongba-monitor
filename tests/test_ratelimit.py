"""全局请求配速器。

关键要求：
  · 上限按「每分钟请求数」配置，调用方不用自己算间隔
  · 状态落盘 —— 常驻的 watch 和 Hermes 临时调起的一次性查询必须共享同一个预算，
    否则两边各自配速，实际速率翻倍
  · 带抖动 —— 完全均匀的间隔比有抖动的更像机器
"""
import ratelimit


class Clock:
    """可控时钟：sleep 直接推进时间，不真的等。"""

    def __init__(self, start=1000.0):
        self.t = start
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def limiter(tmp_path, clock, per_minute=10, jitter=1.0):
    return ratelimit.RateLimiter(
        per_minute=per_minute,
        state_file=tmp_path / "rate.json",
        now=clock.now, sleep=clock.sleep, jitter=lambda: jitter)


# ---- 间隔换算 ----

def test_ten_per_minute_means_six_second_spacing(tmp_path):
    clock = Clock()
    rl = limiter(tmp_path, clock, per_minute=10)

    rl.acquire()
    rl.acquire()

    assert clock.slept == [6.0]


def test_four_per_minute_means_fifteen_second_spacing(tmp_path):
    clock = Clock()
    rl = limiter(tmp_path, clock, per_minute=4)

    rl.acquire()
    rl.acquire()

    assert clock.slept == [15.0]


# ---- 不该等的时候不等 ----

def test_first_request_never_waits(tmp_path):
    clock = Clock()

    limiter(tmp_path, clock).acquire()

    assert clock.slept == []


def test_no_wait_when_enough_time_already_passed(tmp_path):
    clock = Clock()
    rl = limiter(tmp_path, clock, per_minute=10)
    rl.acquire()
    clock.t += 100          # 外部耗时远超间隔

    rl.acquire()

    assert clock.slept == []


def test_only_the_remaining_time_is_waited(tmp_path):
    clock = Clock()
    rl = limiter(tmp_path, clock, per_minute=10)
    rl.acquire()
    clock.t += 4            # 已经过去 4 秒，只需再等 2 秒

    rl.acquire()

    assert clock.slept == [2.0]


# ---- 跨进程共享 ----

def test_a_separate_instance_honours_the_shared_state(tmp_path):
    clock = Clock()
    limiter(tmp_path, clock).acquire()

    # 模拟另一个进程：新实例，同一个状态文件，同一个时钟
    limiter(tmp_path, clock).acquire()

    assert clock.slept == [6.0]


def test_state_file_is_created_on_first_use(tmp_path):
    clock = Clock()

    limiter(tmp_path, clock).acquire()

    assert (tmp_path / "rate.json").exists()


def test_corrupt_state_file_does_not_crash(tmp_path):
    (tmp_path / "rate.json").write_text("not json at all")
    clock = Clock()

    limiter(tmp_path, clock).acquire()      # 不应抛异常

    assert clock.slept == []


# ---- 抖动 ----

def test_jitter_scales_the_interval(tmp_path):
    import pytest

    clock = Clock()
    rl = limiter(tmp_path, clock, per_minute=10, jitter=1.2)

    rl.acquire()
    rl.acquire()

    assert clock.slept == [pytest.approx(7.2)]


def test_default_jitter_stays_within_twenty_percent(tmp_path):
    rl = ratelimit.RateLimiter(per_minute=10, state_file=tmp_path / "r.json")

    factors = [rl._jitter() for _ in range(200)]

    assert all(0.8 <= f <= 1.2 for f in factors)
    assert len(set(factors)) > 1          # 真的在抖，不是常数


# ---- 预算可见性 ----

def test_counts_requests_so_usage_is_observable(tmp_path):
    clock = Clock()
    rl = limiter(tmp_path, clock)

    for _ in range(3):
        rl.acquire()

    assert rl.count == 3
