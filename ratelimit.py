#!/usr/bin/env python3
"""全局请求配速器。

按「每分钟请求数」配置，调用方不用自己算间隔 —— 加场馆、加日期都不会偷偷把
实际速率顶上去。

状态落盘并跨进程共享：常驻的 watch 和 Hermes 临时调起的一次性查询走同一个
预算，否则两边各自配速，实际速率翻倍。
"""
import fcntl
import json
import random
import time
from pathlib import Path

DEFAULT_PER_MINUTE = 10
JITTER_RANGE = (0.8, 1.2)


class RateLimiter:
    def __init__(self, per_minute: int = DEFAULT_PER_MINUTE, state_file=None,
                 now=time.time, sleep=time.sleep, jitter=None):
        if per_minute <= 0:
            raise ValueError(f"per_minute 必须 > 0，收到 {per_minute}")
        self.interval = 60.0 / per_minute
        self.state_file = Path(state_file) if state_file else None
        self._now = now
        self._sleep = sleep
        self._jitter = jitter or (lambda: random.uniform(*JITTER_RANGE))
        self.count = 0

    # ---- 状态读写 ----

    def _read_last(self) -> float:
        if not self.state_file or not self.state_file.exists():
            return 0.0
        try:
            return float(json.loads(self.state_file.read_text())["last"])
        except Exception:
            # 状态文件损坏不该让整个查询挂掉，最差就是这一次不等待
            return 0.0

    def _write_last(self, stamp: float) -> None:
        if not self.state_file:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass        # 不支持 flock 的文件系统上退化为无锁，能接受
            json.dump({"last": stamp}, f)

    # ---- 对外 ----

    def acquire(self) -> float:
        """阻塞到可以发下一个请求，返回实际等待的秒数。"""
        wait = self._read_last() + self.interval * self._jitter() - self._now()
        if wait > 0:
            self._sleep(wait)
        else:
            wait = 0.0
        self._write_last(self._now())
        self.count += 1
        return wait
