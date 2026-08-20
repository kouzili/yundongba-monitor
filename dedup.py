#!/usr/bin/env python3
"""空场通知去重。

现象：监控每 30s 一轮，同一个空场只要还在，每轮都会命中。不去重就是刷屏。

规则：记住上一轮「已知空场」集合，只推本轮新出现的。空场消失后从集合中移出，
之后再出现会重新推送 —— 场被人退了值得再提醒一次。

状态只在内存里，进程重启后已知空场会重推一次。
"""


def slot_key(slot: dict) -> tuple:
    return (str(slot["stadiumid"]), slot["date"],
            str(slot["fieldid"]), slot["timePoint"])


def slot_scope(slot: dict) -> tuple:
    return (str(slot["stadiumid"]), slot["date"])


class SlotTracker:
    def __init__(self):
        self._known = set()

    def new_slots(self, slots: list, polled_scopes: set) -> list:
        """返回本轮新出现的空场，保持传入顺序，并更新状态。

        polled_scopes 是本轮成功查询到的 (stadiumid, date) 集合。状态只在这些
        范围内替换 —— 查询失败的场地保留上轮状态，否则一次网络抖动就会让下一轮
        把同一批空场全部重推。
        """
        current = {slot_key(s) for s in slots}
        new = [s for s in slots if slot_key(s) not in self._known]
        survivors = {k for k in self._known if (k[0], k[1]) not in polled_scopes}
        self._known = survivors | current
        return new

    def retain_scopes(self, scopes: set) -> None:
        """丢弃不在给定范围内的状态，用于配置里移除了场地或日期之后。"""
        self._known = {k for k in self._known if (k[0], k[1]) in scopes}
