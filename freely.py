#!/usr/bin/env python3
"""畅打（开放打球）活动。

接口是 YDBCLUB/service/personalCenter/activityList，走 secret_ydb 那把密钥，
返回信封是 result_code / result_msg / result_data。免登录。

隐私：原始返回里的 enrollPeoples 含其他用户的真实姓名与头像 URL，一律不往外传，
只输出人数与上限。
"""
from datetime import datetime

import sign

ENDPOINT = "YDBCLUB/service/personalCenter/activityList"
TYPECODE = "201103"          # 畅打
SHANGHAI = (121.47, 31.23)   # 默认坐标，仅用于接口要求的 lng/lat


def _hhmm(stamp):
    return datetime.fromtimestamp(stamp).strftime("%H:%M") if stamp else None


def _int_or_none(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def parse_activity(raw: dict) -> dict:
    """把一条原始活动整理成对外的形状。不含任何他人身份信息。"""
    start, end = raw.get("startexecutetime"), raw.get("endexecutetime")
    joined = _int_or_none(raw.get("joinCount")) or 0
    limit = _int_or_none(raw.get("jointoplimit"))
    spots_left = max(0, limit - joined) if limit is not None else None

    return {
        "activityid": str(raw.get("activityid") or raw.get("id") or ""),
        "name": raw.get("name"),
        "stadium": raw.get("stadiumName") or raw.get("stadiumname"),
        "stadiumid": str(raw.get("stadiumid") or ""),
        "date": datetime.fromtimestamp(start).strftime("%Y-%m-%d") if start else None,
        "start": _hhmm(start),
        "end": _hhmm(end),
        "start_ts": start,
        "end_ts": end,
        "joined": joined,
        "limit": limit,
        "spots_left": spots_left,
        "full": spots_left == 0 if spots_left is not None else False,
        "price": raw.get("expense"),
        "distance_km": raw.get("distance"),
        "address": raw.get("address"),
        "enroll_open": _hhmm(raw.get("joinstartdate")),
        "enroll_close": _hhmm(raw.get("joinenddate")),
    }


def filter_by_hours(activities: list, start_hour, end_hour) -> list:
    """保留与 [start_hour, end_hour) 有重叠的活动。任一端为 None 则不过滤。

    用重叠而不是「开始时间落在窗口内」—— 18:00 想打球的人，一个 17-19 的场
    同样有意义。
    """
    if start_hour is None or end_hour is None:
        return list(activities)
    kept = []
    for a in activities:
        if not a.get("start_ts") or not a.get("end_ts"):
            continue
        begins = datetime.fromtimestamp(a["start_ts"])
        finishes = datetime.fromtimestamp(a["end_ts"])
        begin_hour = begins.hour + begins.minute / 60
        finish_hour = finishes.hour + finishes.minute / 60
        if finishes.date() > begins.date():
            finish_hour = 24            # 跨天的按开始那天算，收尾记作 24 点
        if finish_hour > start_hour and begin_hour < end_hour:
            kept.append(a)
    return kept


def filter_by_date(activities: list, date_str) -> list:
    if not date_str:
        return list(activities)
    return [a for a in activities if a.get("date") == date_str]


def list_activities(cityid: int = 75, stadiumid=None, begin=None, end=None,
                    page: int = 1, size: int = 20, appsessionid: str = "") -> dict:
    """拉一页畅打活动。返回 {"quantity": n, "activities": [...]} 或 {"error": msg}。"""
    params = {
        "page": page, "size": size,
        "longitude": SHANGHAI[0], "latitude": SHANGHAI[1],
        "typecode": TYPECODE, "status": 0, "flag": "1",
        "cityid": cityid,
    }
    if begin is not None:
        params["begin"] = begin
    if end is not None:
        params["end"] = end
    if stadiumid:
        params["stadiumid"] = str(stadiumid)

    data = sign.call_api(ENDPOINT, params, appsessionid)
    if "error" in data:
        return data
    return {"quantity": data.get("quantity"),
            "activities": [parse_activity(a) for a in data.get("activityList", [])]}
