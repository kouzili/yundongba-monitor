#!/usr/bin/env python3
"""
韵动吧网球场 — 空场监控 + 自动抢场脚本
=========================================
用法:
  python3 monitor.py                    # 开始轮询监控
  python3 monitor.py --once             # 只查一次
  python3 monitor.py --interval 15      # 每 15 秒轮询

配置:
  修改下方 CONFIG 区域的参数
  通知方式任选其一:
    - Server酱: export SERVERCHAN_KEY="你的key"
    - 企业微信机器人: export WECOM_WEBHOOK="https://qyapi.weixin.qq.com/..."

签名更新:
  签名从韵动吧 App 抓包获取。Session 过期后需重新抓包:
  1. 手机连 Mac 代理, 在 App 内逐个搜日期
  2. 运行这个脚本的同目录下的 extract_signs.py 提取新签名
  3. 把输出粘贴到下方 SCHEDULE_SIGNS / BOOK_SIGNS
"""

import json
import os
import sys
import time
from datetime import datetime

import requests


# ============================================================
#  C O N F I G
# ============================================================
STADIUM_ID = "1128"
STADIUM_NAME = "唛恩网球中心（东馆）"

# 监控的日期 (Unix 时间戳, 当天 00:00:00 CST)
#   Aug 5 = 1785859200
#   Aug 6 = 1785945600
#   Aug 7 = 1786032000
#   Aug 8 = 1786118400
#   Aug 9 = 1786204800
MONITOR_DATES = [1786118400, 1786204800]

# 目标时间段  (小时, 左闭右开)
TARGET_START = 18
TARGET_END = 21

# 轮询间隔(秒)
POLL_INTERVAL = 30


def date_str(ts):
    return datetime.fromtimestamp(ts).strftime("%m/%d")


# ============================================================
#  签 名 库 (session 过期后重新抓包并替换此处)
# ============================================================
SCHEDULE_SIGNS = {
    1786118400: {"nonce": "2608051785915534", "sign": "e8b687dab028ff7db4a3"},
    1786204800: {"nonce": "2608051785915529", "sign": "217c0ebc0acb1c84fcf9"},
}

# 自动抢场签名: {(date_ts, fieldid): {"nonce": "...", "sign": "..."}}
# 从 App 下单时抓包获取，签名绑定具体场地+日期+时段
BOOK_SIGNS = {
    # Aug 8, 室内东馆08, 16:00-18:00 (fieldid=2999)
    (1786118400, "2999"): {"nonce": "2608051785915540", "sign": "0aa8818c359066460730"},
}

# ============================================================
#  API 基础
# ============================================================
API_BASE = "https://wxapi.sports8.com.cn"
APP_KEY, METHOD = "17992635", "iOS.v20"

def _load_cfg():
    p = os.path.join(os.path.dirname(__file__) or ".", "config.json")
    if os.path.exists(p):
        return json.load(open(p))
    return {}

def _get_headers():
    c = _load_cfg()
    return {
        "content-type": "application/json", "accept": "application/json",
        "appversion": "5.0.140", "accept-language": "zh-CN,zh-Hans;q=0.9",
        "user-agent": "app/1 CFNetwork/3826.400.120 Darwin/24.3.0",
        "appsessionid": c.get("appsessionid", ""),
    }

def _get_uid(): return _load_cfg().get("user_id", 0)


# ============================================================
#  核 心
# ============================================================
def call(endpoint, extra, sign_info):
    body = {
        "wCommon": None,
        "biz": endpoint.rsplit("/", 1)[-1],
        "nonce": sign_info["nonce"],
        "method": METHOD,
        "ydbsp_app_key": APP_KEY,
        "sign": sign_info["sign"],
        **extra,
    }
    try:
        r = requests.post(f"{API_BASE}/{endpoint}", json=body, headers=_get_headers(), timeout=15)
        if r.status_code == 200:
            d = r.json()
            if d.get("returnCode") == "0":
                return d["returnData"]
            return {"error": d.get("returnMsg", f"code={d.get('returnCode')}")}
        return {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"error": str(e)}


def poll(date_ts):
    """查询排期, 返回 (目标时段空场列表, 所有可订时段列表, 错误信息或None)"""
    si = SCHEDULE_SIGNS.get(date_ts)
    if not si:
        return [], [], f"日期 {date_str(date_ts)} 缺签名"
    data = call("api/ydb/stadium/apiGetStadiumShedule",
                {"stadiumid": STADIUM_ID, "date": date_ts, "userid": _get_uid()}, si)
    if "error" in data:
        return [], [], data["error"]

    target_slots = []
    all_slots = []
    for f in data.get("fieldList", []):
        for s in f.get("shedule", []):
            if s.get("status") != "0":
                continue
            tp = s.get("timePoint")
            if tp is None:
                continue
            slot = {
                "field": f["name"], "fieldid": s["fieldid"],
                "time": f"{tp:02d}:00", "timePoint": tp, "price": s["realPrice"],
            }
            all_slots.append(slot)
            if TARGET_START <= tp < TARGET_END:
                target_slots.append(slot)
    return target_slots, all_slots, None


def find_nearest(all_slots, target_start, target_end):
    """在可订时段中, 找离目标窗口最近的一个"""
    if not all_slots:
        return None
    best, best_dist = None, float("inf")
    for s in all_slots:
        tp = s["timePoint"]
        if tp < target_start:
            dist = target_start - tp          # 目标之前, 距离=时间差
        elif tp >= target_end:
            dist = tp - (target_end - 1)       # 目标之后, 距离=时间差
        else:
            continue                            # 已在窗口中, 不参与
        if dist < best_dist:
            best_dist, best = dist, s
    return best


def book(date_ts, fieldid, field_name, time_str):
    si = BOOK_SIGNS.get((date_ts, str(fieldid)))
    if not si:
        return None, "无预置签名"
    tp = int(time_str.split(":")[0])
    order_list = json.dumps([{
        "cheapFlag": 0, "name": field_name, "timePoint": tp,
        "discountType": 1, "expense": 480, "status": "0",
        "fieldid": fieldid, "realPrice": 400,
        "start": time_str, "end": f"{tp+2:02d}:00",
        "startTime": tp, "endTime": tp + 2,
    }])
    data = call("api/ydb/stadium/apiSetOrderField",
                {"userid": _get_uid(), "stadiumid": STADIUM_ID, "date": date_ts, "orderList": order_list}, si)
    if "error" in data:
        return None, data["error"]
    return data.get("orderuid"), f"¥{data.get('realExpense','?')}"


# ============================================================
#  通 知
# ============================================================
def notify(title, content):
    ok = False
    key = os.environ.get("SERVERCHAN_KEY", _load_cfg().get("serverchan_key", ""))
    if key:
        try:
            requests.post(f"https://sctapi.ftqq.com/{key}.send",
                          data={"title": title, "desp": content}, timeout=10)
            ok = True
        except Exception:
            pass
    url = os.environ.get("WECOM_WEBHOOK", "")
    if url:
        try:
            requests.post(url, json={
                "msgtype": "text", "text": {"content": f"{title}\n{content}"}
            }, timeout=10)
            ok = True
        except Exception:
            pass
    if ok:
        print("  📱 微信通知已发送")
    else:
        print("  ⚠️  未配置通知方式 (SERVERCHAN_KEY / WECOM_WEBHOOK)")


# ============================================================
#  主 循 环
# ============================================================
def run_once(round_num=0):
    now = datetime.now().strftime("%H:%M:%S")
    header = f"[第{round_num}轮 {now}]" if round_num else f"[{now}]"
    print(f"\n{header}")
    found_any = False

    for ts in MONITOR_DATES:
        target, all_slots, err = poll(ts)
        if err:
            print(f"  {date_str(ts)} ❌ {err}")
            continue

        ds = date_str(ts)
        if target:
            found_any = True
            print(f"  {ds} 🎾 目标时段 {TARGET_START:02d}:00–{TARGET_END:02d}:00 空场 {len(target)} 个:")
            lines = []
            for s in target:
                info = f"{s['field']} · {s['time']} · ¥{s['price']}"
                lines.append(info)
                oid, msg = book(ts, s["fieldid"], s["field"], s["time"])
                tag = "🤖已抢" if oid else "✋需手动"
                print(f"    🟢 {info} → {tag}")
            if lines:
                notify(f"🎾 {STADIUM_NAME} {ds} 有空场!", "\n".join(lines))
        else:
            # 无目标空场 → 找最近的
            nearest = find_nearest(all_slots, TARGET_START, TARGET_END)
            if nearest:
                nt = nearest["timePoint"]
                if nt < TARGET_START:
                    hint = f"距目标 {TARGET_START - nt}h"
                else:
                    hint = f"距目标 {nt - (TARGET_END - 1)}h"
                print(f"  {ds} ⚪ 目标时段全满  |  最近空场 → {nearest['field']} {nearest['time']} ¥{nearest['price']} ({hint})")
            else:
                print(f"  {ds} ⚪ 全天无空场")

    return found_any


def main():
    global POLL_INTERVAL

    if "--interval" in sys.argv:
        idx = sys.argv.index("--interval")
        if idx + 1 < len(sys.argv):
            POLL_INTERVAL = int(sys.argv[idx + 1])

    print("=" * 55)
    print(f" 韵动吧 · {STADIUM_NAME}")
    print(f" 时段 {TARGET_START:02d}:00–{TARGET_END:02d}:00  排查 {', '.join(date_str(d) for d in MONITOR_DATES)}")
    print(f" 轮询间隔 {POLL_INTERVAL}s")
    print("=" * 55)

    if "--once" in sys.argv:
        run_once()
        return

    round_num = 0
    while True:
        round_num += 1
        run_once(round_num)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
