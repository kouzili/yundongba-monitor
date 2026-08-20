"""空场通知去重状态机。

key = (stadiumid, date, fieldid, timePoint)
只有本轮成功查询到的范围才更新状态 —— 否则单次网络失败会清空状态，
下一轮成功时把同一批空场重推一遍。
"""
import dedup


def slot(stadiumid="1128", date="2026-08-15", fieldid="2999", tp=18, **kw):
    d = {"stadiumid": stadiumid, "date": date, "fieldid": fieldid,
         "timePoint": tp, "field": f"场{fieldid}", "time": f"{tp:02d}:00",
         "price": 400, "stadium": f"馆{stadiumid}"}
    d.update(kw)
    return d


def scope(stadiumid="1128", date="2026-08-15"):
    return (stadiumid, date)


def test_first_appearance_is_reported():
    t = dedup.SlotTracker()

    new = t.new_slots([slot()], polled_scopes={scope()})

    assert len(new) == 1
    assert new[0]["timePoint"] == 18


def test_same_slot_next_round_is_not_reported_again():
    t = dedup.SlotTracker()
    t.new_slots([slot()], polled_scopes={scope()})

    new = t.new_slots([slot()], polled_scopes={scope()})

    assert new == []


def test_slot_reappearing_after_disappearing_is_reported_again():
    t = dedup.SlotTracker()
    t.new_slots([slot()], polled_scopes={scope()})
    t.new_slots([], polled_scopes={scope()})          # 被别人订走了

    new = t.new_slots([slot()], polled_scopes={scope()})  # 又被退了

    assert len(new) == 1


def test_only_newly_appeared_slots_are_reported():
    t = dedup.SlotTracker()
    t.new_slots([slot(tp=18)], polled_scopes={scope()})

    new = t.new_slots([slot(tp=18), slot(tp=19)], polled_scopes={scope()})

    assert [s["timePoint"] for s in new] == [19]


def test_reported_slots_keep_input_order():
    t = dedup.SlotTracker()

    new = t.new_slots([slot(tp=20), slot(tp=18), slot(tp=19)],
                      polled_scopes={scope()})

    assert [s["timePoint"] for s in new] == [20, 18, 19]


def test_failed_scope_keeps_its_state_so_nothing_is_respammed():
    t = dedup.SlotTracker()
    t.new_slots([slot()], polled_scopes={scope()})

    # 这一轮该场地查询失败 —— 不在 polled_scopes 里，空场自然也查不到
    assert t.new_slots([], polled_scopes=set()) == []
    # 下一轮恢复，同一个空场不该重推
    assert t.new_slots([slot()], polled_scopes={scope()}) == []


def test_failure_in_one_scope_does_not_affect_another():
    t = dedup.SlotTracker()
    t.new_slots([slot(stadiumid="1128"), slot(stadiumid="2000")],
                polled_scopes={scope("1128"), scope("2000")})

    # 1128 查失败，2000 查成功且空场消失
    t.new_slots([], polled_scopes={scope("2000")})
    new = t.new_slots([slot(stadiumid="1128"), slot(stadiumid="2000")],
                      polled_scopes={scope("1128"), scope("2000")})

    # 1128 的状态被保住了，2000 的消失过所以要重推
    assert [s["stadiumid"] for s in new] == ["2000"]


def test_same_field_at_different_dates_are_distinct_slots():
    t = dedup.SlotTracker()
    t.new_slots([slot(date="2026-08-15")], polled_scopes={scope(date="2026-08-15")})

    new = t.new_slots([slot(date="2026-08-16")],
                      polled_scopes={scope(date="2026-08-16")})

    assert len(new) == 1


def test_retain_scopes_drops_state_outside_current_config():
    t = dedup.SlotTracker()
    t.new_slots([slot(stadiumid="1128"), slot(stadiumid="2000")],
                polled_scopes={scope("1128"), scope("2000")})

    # 用户从配置里移除了 2000
    t.retain_scopes({scope("1128")})

    # 1128 仍被记住，2000 重新加回来时视作新空场
    new = t.new_slots([slot(stadiumid="1128"), slot(stadiumid="2000")],
                      polled_scopes={scope("1128"), scope("2000")})
    assert [s["stadiumid"] for s in new] == ["2000"]
