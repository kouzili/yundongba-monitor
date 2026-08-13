#!/usr/bin/env python3
"""
韵动吧 API 客户端（签名算法逆向自微信小程序 __APP__.wxapkg，已实测验证）
========================================================================
核心: signBymd5 = MD5(排序 "key=value&...&key=" + secret).toUpperCase()
method = "WxAppBooking"，nonce = "260720" + 毫秒时间戳

签名密钥（secret_api / secret_ydb）从 config.json 读取，不硬编码。
"""
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

import requests

API_BASE = "https://wxapi.sports8.com.cn"
METHOD = "WxAppBooking"
NONCE_PREFIX = "260720"

_CONFIG_FILE = Path(__file__).parent / "config.json"


def reload_secrets():
    """从 config.json 重新加载签名密钥（避免硬编码泄露）"""
    global SECRET_API, SECRET_YDB
    cfg = {}
    if _CONFIG_FILE.exists():
        try:
            cfg = json.load(open(_CONFIG_FILE))
        except Exception:
            cfg = {}
    SECRET_API = cfg.get("secret_api", "")
    SECRET_YDB = cfg.get("secret_ydb", "")
    return SECRET_API, SECRET_YDB


SECRET_API, SECRET_YDB = reload_secrets()


def sign_bymd5(params: dict, secret: str) -> str:
    """标准签名：排序 key=value 拼接 + &key=secret → MD5 大写"""
    keys = [k for k, v in params.items() if v is not None and v != ""]
    keys.sort()
    u = "&".join(f"{k}={params[k]}" for k in keys)
    f = u + "&key=" + secret
    return hashlib.md5(f.encode("utf-8")).hexdigest().upper()


def make_nonce() -> str:
    return NONCE_PREFIX + str(int(time.time() * 1000))


def call_api(endpoint: str, params: dict, appsessionid: str) -> dict:
    """通用 API 调用，自动补 biz/method/nonce/sign，返回 returnData 或 {"error": msg}"""
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    biz = endpoint.rstrip("/").split("/")[-1]
    secret = SECRET_API if endpoint.startswith("/api/") else SECRET_YDB

    body = dict(params)
    body["biz"] = biz
    body["method"] = METHOD
    body["nonce"] = make_nonce()
    body["sign"] = sign_bymd5(body, secret)

    headers = {
        "content-type": "application/json",
        "user-agent": "app/1 CFNetwork/3826.400.120 Darwin/24.3.0",
        "appsessionid": appsessionid,
    }
    try:
        r = requests.post(f"{API_BASE}{endpoint}", json=body, headers=headers, timeout=20)
        data = r.json()
    except Exception as e:
        return {"error": str(e)}
    if data.get("returnCode") == "0":
        return data.get("returnData", {})
    return {"error": data.get("returnMsg", f"code={data.get('returnCode')}")}


def get_schedule(stadiumid: str, date_ts: int, userid: int, appsessionid: str) -> dict:
    """查排期，返回 {stadiumName, fieldList, ...} 或 {"error": msg}"""
    return call_api(
        "api/ydb/stadium/apiGetStadiumShedule",
        {"stadiumid": str(stadiumid), "date": date_ts, "userid": userid},
        appsessionid,
    )


def search_stadium(keyword: str, cityid: int, appsessionid: str) -> list:
    """按关键词搜索场地，返回 [{stadiumid, name}, ...] 或 []"""
    data = call_api(
        "api/ydb/seach/apiSearchKeyword",
        {"keyword": keyword, "cityid": cityid, "pageSize": 30},
        appsessionid,
    )
    if isinstance(data, dict) and "error" in data:
        return []
    result = []
    for item in data.get("resultList", []):
        sid = str(item.get("stadiumid", item.get("id", "")))
        name = item.get("stadiumName", item.get("name", ""))
        if sid and name:
            result.append({"stadiumid": sid, "name": name})
    return result


def book_field(stadiumid: str, date_ts: int, userid: int, appsessionid: str,
               fieldid, start_hour: int, hours: int = 1) -> dict:
    """下单预留（不付款），返回 {orderuid, orderid, realExpense, ...} 或 {"error": msg}"""
    order_list = json.dumps([{
        "fieldid": fieldid,
        "startTime": start_hour,
        "endTime": start_hour + hours,
    }])
    return call_api(
        "api/ydb/stadium/apiSetOrderField",
        {"userid": userid, "date": date_ts, "stadiumid": str(stadiumid),
         "orderList": order_list},
        appsessionid,
    )


def date_to_ts(date_str: str) -> int:
    """日期字符串 -> 当天 00:00 本地时区(CST) Unix 时间戳"""
    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())


def parse_schedule(data: dict, start_hour: int, end_hour: int) -> dict:
    """解析排期，返回 {stadiumName, target:[{field,fieldid,timePoint,price}], nearest:{...}, all_count}"""
    if isinstance(data, dict) and "error" in data:
        return data
    target, all_slots = [], []
    for f in data.get("fieldList", []):
        for s in f.get("shedule", []):
            if s.get("status") != "0":
                continue
            tp = s.get("timePoint")
            if tp is None:
                continue
            slot = {"field": f["name"], "fieldid": s["fieldid"],
                    "time": f"{tp:02d}:00", "timePoint": tp, "price": s["realPrice"]}
            all_slots.append(slot)
            if start_hour <= tp < end_hour:
                target.append(slot)
    nearest = None
    if not target and all_slots:
        best, best_d = None, float("inf")
        for s in all_slots:
            tp = s["timePoint"]
            d = start_hour - tp if tp < start_hour else (tp - (end_hour - 1) if tp >= end_hour else 0)
            if d > 0 and d < best_d:
                best_d, best = d, s
        nearest = best
    return {"stadiumName": data.get("stadiumName", "?"), "target": target,
            "nearest": nearest, "all_count": len(all_slots)}


if __name__ == "__main__":
    import sys
    sid = sys.argv[1] if len(sys.argv) > 1 else "1128"
    # 从 config.json 读取凭据，避免硬编码泄露
    import json
    from pathlib import Path
    cfg = {}
    cfg_path = Path(__file__).parent / "config.json"
    if cfg_path.exists():
        cfg = json.load(open(cfg_path))
    uid = int(sys.argv[2]) if len(sys.argv) > 2 else cfg.get("userid", 0)
    appid = sys.argv[3] if len(sys.argv) > 3 else cfg.get("appsessionid", "")
    today = int(time.time())
    for i in range(1, 4):
        ts = today + i * 86400
        data = get_schedule(sid, ts, uid, appid)
        if "error" in data:
            print(f"+{i}天: ❌ {data['error']}")
        else:
            print(f"+{i}天: ✅ {data.get('stadiumName', '?')}, 可订 {sum(1 for f in data.get('fieldList',[]) for s in f.get('shedule',[]) if s.get('status')=='0')} 个时段")
