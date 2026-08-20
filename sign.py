#!/usr/bin/env python3
"""
韵动吧 API 客户端 —— 只读（查排期 / 搜场地），不含下单
========================================================================
签名算法逆向自微信小程序 __APP__.wxapkg，已实测验证：
  signBymd5 = MD5(排序 "key=value&...&key=" + secret).toUpperCase()
  method = "WxAppBooking"，nonce = "260720" + 毫秒时间戳

密钥从 config.json 读取，不硬编码。服务端按路径前缀用两把不同的密钥：
  /api/     → secret_api   查排期、搜场地、登录
  其它前缀  → secret_ydb   YDBCLUB/（畅打活动）、YDB/（支付相关）
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
            cfg = json.loads(_CONFIG_FILE.read_text())
        except Exception:
            cfg = {}
    SECRET_API = cfg.get("secret_api", "")
    SECRET_YDB = cfg.get("secret_ydb", "")
    return SECRET_API, SECRET_YDB


SECRET_API, SECRET_YDB = reload_secrets()


def secret_for(endpoint: str) -> str:
    """按路径前缀选密钥。大小写敏感 —— 真实路径的大小写是固定的。"""
    return SECRET_API if endpoint.startswith("/api/") else SECRET_YDB


def sign_bymd5(params: dict, secret: str) -> str:
    """标准签名：排序 key=value 拼接 + &key=secret → MD5 大写"""
    keys = [k for k, v in params.items() if v is not None and v != ""]
    keys.sort()
    u = "&".join(f"{k}={params[k]}" for k in keys)
    f = u + "&key=" + secret
    return hashlib.md5(f.encode("utf-8")).hexdigest().upper()


def make_nonce() -> str:
    return NONCE_PREFIX + str(int(time.time() * 1000))


def normalize_response(data: dict) -> dict:
    """把两套返回信封归一成「payload 或 {"error": msg}」。

    /api/    用 returnCode / returnMsg / returnData
    YDBCLUB/ 用 result_code / result_msg / result_data —— 字段名完全不同，
    照着一套解析另一套会把成功当失败、把失败当「需要登录」。
    """
    if "returnCode" in data:
        code, message, payload = (data.get("returnCode"), data.get("returnMsg"),
                                  data.get("returnData"))
    elif "result_code" in data:
        code, message, payload = (data.get("result_code"), data.get("result_msg"),
                                  data.get("result_data"))
    else:
        return {"error": f"无法识别的返回格式: {sorted(data)[:6]}"}

    if str(code) == "0":
        return payload if payload is not None else {}
    return {"error": message or f"code={code}"}


_RATE_LIMITER = None


def set_rate_limiter(limiter) -> None:
    """装上全局配速器。所有出站请求都经过它 —— 否则连续调用会绕过速率上限。

    传 None 表示不限速（测试和一次性脚本用）。
    """
    global _RATE_LIMITER
    _RATE_LIMITER = limiter


def call_api(endpoint: str, params: dict, appsessionid: str) -> dict:
    """通用 API 调用，自动补 biz/method/nonce/sign，返回 returnData 或 {"error": msg}"""
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    biz = endpoint.rstrip("/").split("/")[-1]

    if _RATE_LIMITER is not None:
        _RATE_LIMITER.acquire()

    body = dict(params)
    body["biz"] = biz
    body["method"] = METHOD
    body["nonce"] = make_nonce()
    body["sign"] = sign_bymd5(body, secret_for(endpoint))

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
    return normalize_response(data)


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


def hash_password(plaintext: str) -> str:
    """密码要先 MD5 再发。

    小程序里是 `MD5(this.password).toString()` —— CryptoJS 默认输出小写十六进制。
    发明文或发大写都会登录失败。
    """
    return hashlib.md5(plaintext.encode("utf-8")).hexdigest()


def login(mobile: str, password: str) -> dict:
    """手机号 + 密码登录，返回 {userid, ...} 或 {"error": msg}。

    注意这套接口没有 session token —— 小程序源码里 appsessionid 出现 0 次，
    登录成功后它只把 userid 存进本地存储。签名是唯一的门，userid 只是个参数，
    而且是永久的，所以登录只需要跑一次。
    """
    return call_api("api/ydb/account/userLogin",
                    {"mobile": mobile, "password": hash_password(password)}, "")


def build_order_list(fieldid, start_hour: int, hours: int = 1) -> str:
    """构造 orderList。客户端只发这三个字段，其余服务端自己取。

    连续时段在客户端会被合并成一条（endTime = 起点 + 小时数）。
    """
    if hours < 1:
        raise ValueError(f"hours 必须 >= 1，收到 {hours}")
    return json.dumps([{"fieldid": fieldid,
                        "startTime": start_hour,
                        "endTime": start_hour + hours}])


def book_field(stadiumid: str, date_ts: int, userid: int, fieldid,
               start_hour: int, hours: int = 1, appsessionid: str = "") -> dict:
    """锁单（不付款）。返回订单信息或 {"error": msg}。

    付款是独立接口（apiSetPay），所以这一步只占场不扣钱。
    """
    return call_api("api/ydb/stadium/apiSetOrderField",
                    {"userid": userid, "date": date_ts, "stadiumid": str(stadiumid),
                     "orderList": build_order_list(fieldid, start_hour, hours)},
                    appsessionid)


def time_period(start_hour, end_hour) -> str:
    """v2 搜索的时段参数格式：客户端发的是 "起点,终点"（如 "19,21"）。"""
    if start_hour is None or end_hour is None:
        return "0,24"
    return f"{start_hour},{end_hour}"


def parse_stadium_result(raw: dict) -> dict:
    """整理 v2 搜索的一条场馆记录。

    这个接口给的是「符合时段/距离/价格条件的候选场馆」，**不含具体空场时段** ——
    要知道哪个钟点空着，还得再用 get_schedule 查一次。
    """
    metres = raw.get("distance")
    try:
        km = round(float(metres) / 1000, 1)
    except (TypeError, ValueError):
        km = None
    return {
        "stadiumid": str(raw.get("stadiumid") or ""),
        "name": raw.get("stadiumName") or raw.get("name"),
        "district": raw.get("countyName"),
        "distance_km": km,
        "min_price": raw.get("minPrice"),
        "indoor": raw.get("indoor") == "1",
        "outdoor": raw.get("outdoor") == "1",
        "tags": raw.get("newTags") or [],
        # 该场馆提前放场的天数 —— 查更远的日期必然返回空
        "max_days_ahead": raw.get("maxday"),
        "address": raw.get("stadiumAddress") or raw.get("address"),
        "phone": raw.get("stadiumTel") or raw.get("telephone"),
    }


def find_stadiums(cityid: int, date_ts: int, start_hour, end_hour,
                  userid: int = 0, lat: float = 31.23, lng: float = 121.47,
                  sort: str = "distance", page: int = 1, size: int = 20,
                  appsessionid: str = "") -> dict:
    """按日期 + 时段筛候选场馆。sort: distance / minPrice / collect。"""
    data = call_api("api/ydb/stadium/v2/apiGetSearchStadiumByV2", {
        "pageIndex": page, "pageSize": size, "userid": userid,
        "cityid": cityid, "date": date_ts,
        "timePeriod": time_period(start_hour, end_hour),
        "countyid": "", "lat": lat, "lng": lng, "sort": sort,
    }, appsessionid)
    if "error" in data:
        return data
    return {"total": data.get("allStadiumSize") or data.get("stadiumSize"),
            "stadiums": [parse_stadium_result(s)
                         for s in data.get("stadiumList", [])]}


def pending_field_orders(userid: int, appsessionid: str = "") -> list:
    """待处理的场地订单。

    平台规则：同一时间只允许一笔未处理的场地订单，而且订单过期后（countDown 变负）
    仍然挂着挡路、不会自动消失。所以锁单前必须先查这个，否则必定被拒。

    场地订单的 targettype 是 "0"（"7" 是别的业务），orderStatus 必填。
    """
    data = call_api("api/ydb/order/apiGetNewOrderList_refund",
                    {"userid": userid, "orderStatus": "0", "targettype": "0",
                     "page": 1, "pageSize": 20}, appsessionid)
    if "error" in data:
        return []
    return data.get("orderList") or []


def cancel_order(orderid, userid: int, appsessionid: str = "") -> dict:
    """取消未付款订单。"""
    return call_api("api/ydb/order/apiCancelOrder",
                    {"orderid": orderid, "userid": userid}, appsessionid)


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
    # 冒烟测试: python3 sign.py [stadiumid] [userid] [appsessionid]
    import sys
    sid = sys.argv[1] if len(sys.argv) > 1 else "1128"
    cfg = json.loads(_CONFIG_FILE.read_text()) if _CONFIG_FILE.exists() else {}
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
