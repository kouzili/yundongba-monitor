"""飞书机器人推送 —— payload 形状、签名校验、成功/失败判定。

不 mock requests：起一个真实的本地 HTTP 服务当假 webhook，断言服务端
实际收到的字节，以及 send_text 对各种响应体的判定。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import notify


# ---- 假 webhook ----

class _FakeWebhook:
    """收一个 POST，记下 body，按预设响应。"""

    def __init__(self, response_body: dict, status: int = 200):
        self.received = None
        self._response_body = response_body
        self._status = status
        handler = self._make_handler()
        self._server = HTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/hook/xxx"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                outer.received = json.loads(self.rfile.read(length))
                body = json.dumps(outer._response_body).encode()
                self.send_response(outer._status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass  # 别把 HTTP 日志混进 pytest 输出

        return Handler

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


# ---- payload 形状 ----

def test_payload_without_secret_has_no_sign_fields():
    payload = notify.build_payload("有空场了", secret="", timestamp="1700000000")

    assert payload == {"msg_type": "text", "content": {"text": "有空场了"}}


def test_payload_with_secret_carries_timestamp_and_sign():
    payload = notify.build_payload("有空场了", secret="testsecret", timestamp="1700000000")

    assert payload["timestamp"] == "1700000000"
    assert payload["sign"] == "AOc8oJ7//5OlQlfWC3nRL0R+IkuzcD1FKcAyibRK9Q8="
    assert payload["content"] == {"text": "有空场了"}


def test_sign_matches_feishu_known_vector():
    # 飞书算法: HMAC-SHA256(key=f"{timestamp}\n{secret}", msg=b"") 再 base64
    assert notify.feishu_sign("1700000000", "testsecret") == \
        "AOc8oJ7//5OlQlfWC3nRL0R+IkuzcD1FKcAyibRK9Q8="


# ---- 发送与判定 ----

def test_send_text_posts_payload_and_reports_success():
    with _FakeWebhook({"code": 0, "msg": "success"}) as hook:
        ok, msg = notify.send_text(hook.url, "唛恩网球中心 18:00 有空场")

        assert ok is True
        assert hook.received == {
            "msg_type": "text",
            "content": {"text": "唛恩网球中心 18:00 有空场"},
        }


def test_send_text_accepts_legacy_statuscode_zero():
    # 飞书部分响应用 StatusCode 而非 code
    with _FakeWebhook({"StatusCode": 0, "StatusMessage": "success"}) as hook:
        ok, _ = notify.send_text(hook.url, "x")

        assert ok is True


def test_send_text_reports_failure_body_on_nonzero_code():
    with _FakeWebhook({"code": 19021, "msg": "sign match fail"}) as hook:
        ok, msg = notify.send_text(hook.url, "x", secret="wrong")

        assert ok is False
        assert "19021" in msg
        assert "sign match fail" in msg


def test_send_text_reports_failure_on_unreachable_webhook():
    # 端口 1 上不会有服务在听
    ok, msg = notify.send_text("http://127.0.0.1:1/hook/xxx", "x")

    assert ok is False
    assert msg


def test_send_text_refuses_empty_webhook_without_network_call():
    ok, msg = notify.send_text("", "x")

    assert ok is False
    assert "webhook" in msg.lower()


# ---- 文本组装 ----

def test_format_slots_groups_by_stadium_in_given_order():
    slots = [
        {"stadium": "古北网球", "field": "1号场", "date": "2026-08-15",
         "time": "19:00", "price": 400},
        {"stadium": "唛恩东馆", "field": "室内08", "date": "2026-08-15",
         "time": "18:00", "price": 380},
        {"stadium": "古北网球", "field": "2号场", "date": "2026-08-16",
         "time": "20:00", "price": 420},
    ]

    text = notify.format_slots(slots)

    # 场地分组，组内保持传入顺序；先出现的场地排在前
    assert text.index("古北网球") < text.index("唛恩东馆")
    assert text.index("1号场") < text.index("2号场")
    assert "2026-08-15" in text
    assert "19:00" in text
    assert "¥400" in text


def test_format_slots_on_empty_input_returns_empty_string():
    assert notify.format_slots([]) == ""
