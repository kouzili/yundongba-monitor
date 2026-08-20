"""畅打活动解析。

接口返回的 enrollPeoples 里有其他用户的真实姓名和头像 URL —— 一律不往外传，
只输出人数与上限。
"""
from datetime import datetime

import freely


def ts(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d %H:%M").timestamp())


def activity(**over):
    base = {
        "activityid": "100710",
        "name": "周二2️⃣小时畅打(19-21)-空调场",
        "stadiumName": "光圈网球·HALOTENNIS(普陀体育公园室内空调店)",
        "stadiumid": "833",
        "startexecutetime": ts("2026-08-18 19:00"),
        "endexecutetime": ts("2026-08-18 21:00"),
        "joinstartdate": ts("2026-08-18 17:00"),
        "joinenddate": ts("2026-08-18 21:00"),
        "joinCount": 2,
        "jointoplimit": "4",
        "expense": "99.00",
        "distance": "13.5",
        "address": "金通路158号（2号门）",
        "enrollPeoples": [{"userName": "张三", "headImg": "https://x/a.jpg"},
                          {"userName": "李四", "headImg": "https://x/b.jpg"}],
    }
    base.update(over)
    return base


# ---- 字段映射 ----

def test_core_fields_are_extracted():
    a = freely.parse_activity(activity())

    assert a["activityid"] == "100710"
    assert a["stadium"] == "光圈网球·HALOTENNIS(普陀体育公园室内空调店)"
    assert a["date"] == "2026-08-18"
    assert a["start"] == "19:00"
    assert a["end"] == "21:00"
    assert a["price"] == "99.00"


def test_participant_counts_are_reported():
    a = freely.parse_activity(activity(joinCount=2, jointoplimit="4"))

    assert a["joined"] == 2
    assert a["limit"] == 4
    assert a["spots_left"] == 2
    assert a["full"] is False


def test_a_full_activity_is_flagged():
    a = freely.parse_activity(activity(joinCount=4, jointoplimit="4"))

    assert a["spots_left"] == 0
    assert a["full"] is True


def test_overbooked_never_reports_negative_spots():
    a = freely.parse_activity(activity(joinCount=6, jointoplimit="4"))

    assert a["spots_left"] == 0
    assert a["full"] is True


def test_missing_limit_is_reported_as_unknown_not_zero():
    a = freely.parse_activity(activity(jointoplimit=""))

    assert a["limit"] is None
    assert a["spots_left"] is None
    assert a["full"] is False


# ---- 隐私 ----

def test_other_participants_identities_are_never_exposed():
    a = freely.parse_activity(activity())

    dumped = str(a)
    assert "张三" not in dumped
    assert "李四" not in dumped
    assert "headImg" not in dumped
    assert "enrollPeoples" not in a


# ---- 时段过滤 ----

def test_filter_keeps_activities_overlapping_the_window():
    items = [activity(activityid="a", startexecutetime=ts("2026-08-18 19:00"),
                      endexecutetime=ts("2026-08-18 21:00")),
             activity(activityid="b", startexecutetime=ts("2026-08-18 08:00"),
                      endexecutetime=ts("2026-08-18 10:00"))]

    kept = freely.filter_by_hours([freely.parse_activity(i) for i in items], 18, 22)

    assert [a["activityid"] for a in kept] == ["a"]


def test_activity_starting_before_but_running_into_the_window_is_kept():
    a = freely.parse_activity(activity(startexecutetime=ts("2026-08-18 17:00"),
                                       endexecutetime=ts("2026-08-18 19:00")))

    assert freely.filter_by_hours([a], 18, 22) == [a]


def test_activity_entirely_outside_the_window_is_dropped():
    a = freely.parse_activity(activity(startexecutetime=ts("2026-08-18 06:00"),
                                       endexecutetime=ts("2026-08-18 08:00")))

    assert freely.filter_by_hours([a], 18, 22) == []


def test_no_window_means_no_filtering():
    a = freely.parse_activity(activity())

    assert freely.filter_by_hours([a], None, None) == [a]


# ---- 日期过滤 ----

def test_filter_by_date_keeps_only_that_day():
    items = [freely.parse_activity(activity(activityid="a")),
             freely.parse_activity(activity(
                 activityid="b",
                 startexecutetime=ts("2026-08-19 19:00"),
                 endexecutetime=ts("2026-08-19 21:00")))]

    kept = freely.filter_by_date(items, "2026-08-19")

    assert [a["activityid"] for a in kept] == ["b"]


def test_filter_by_date_with_none_keeps_everything():
    items = [freely.parse_activity(activity())]

    assert freely.filter_by_date(items, None) == items
