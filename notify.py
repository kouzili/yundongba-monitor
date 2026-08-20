#!/usr/bin/env python3
"""飞书自定义机器人推送。

机器人若开启了「签名校验」，需在 body 里附带 timestamp + sign：
  sign = base64( HMAC-SHA256( key = f"{timestamp}\\n{secret}", msg = b"" ) )
未开启时 feishu_secret 留空即可。
"""
import base64
import hashlib
import hmac
import json
import time
from collections import OrderedDict

import requests

WEBHOOK_TIMEOUT = 10


def feishu_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_payload(text: str, secret: str = "", timestamp: str = None) -> dict:
    payload = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = timestamp or str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = feishu_sign(ts, secret)
    return payload


def send_text(webhook: str, text: str, secret: str = "") -> tuple:
    """返回 (成功?, 说明)。任何异常都收敛成 (False, 原因)，不抛出。"""
    if not webhook:
        return False, "未配置 feishu webhook"
    try:
        r = requests.post(webhook, json=build_payload(text, secret),
                          timeout=WEBHOOK_TIMEOUT)
        data = r.json()
    except Exception as e:
        return False, str(e)
    if data.get("code") == 0 or data.get("StatusCode") == 0:
        return True, "ok"
    return False, json.dumps(data, ensure_ascii=False)


def format_slots(slots: list) -> str:
    """按场地分组成推送正文。组间与组内均保持传入顺序（调用方已按优先级排好）。"""
    if not slots:
        return ""
    groups = OrderedDict()
    for s in slots:
        groups.setdefault(s["stadium"], []).append(s)
    blocks = []
    for stadium, items in groups.items():
        lines = [f"  {i['date']} {i['time']} · {i['field']} · ¥{i['price']}"
                 for i in items]
        blocks.append(stadium + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)
