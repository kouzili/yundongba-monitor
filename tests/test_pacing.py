"""所有出站请求都必须经过配速器。

不测「配速器有没有被调用」这种 mock 断言，而是打一个真实的本地 HTTP 服务，
用可控时钟看请求之间实际被拉开了多少。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import ratelimit
import sign


class Clock:
    def __init__(self, start=1000.0):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeApi:
    """永远返回成功的本地 API。"""

    def __init__(self):
        self.hits = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.hits += 1
                length = int(self.headers.get("content-length", 0))
                self.rfile.read(length)
                body = json.dumps({"returnCode": "0",
                                   "returnData": {"ok": True}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


def test_consecutive_calls_are_spaced_by_the_configured_rate(tmp_path, monkeypatch):
    clock = Clock()
    with FakeApi() as api:
        monkeypatch.setattr(sign, "API_BASE", api.base)
        sign.set_rate_limiter(ratelimit.RateLimiter(
            per_minute=10, state_file=tmp_path / "r.json",
            now=clock.now, sleep=clock.sleep, jitter=lambda: 1.0))

        for _ in range(3):
            sign.call_api("api/x/y", {"a": 1}, "")

        assert api.hits == 3
        # 第 1 次不等，之后每次等 6 秒
        assert clock.t == 1012.0


def test_pacing_applies_across_different_endpoints(tmp_path, monkeypatch):
    clock = Clock()
    with FakeApi() as api:
        monkeypatch.setattr(sign, "API_BASE", api.base)
        sign.set_rate_limiter(ratelimit.RateLimiter(
            per_minute=10, state_file=tmp_path / "r.json",
            now=clock.now, sleep=clock.sleep, jitter=lambda: 1.0))

        sign.call_api("api/a/b", {}, "")
        sign.call_api("YDBCLUB/c/d", {}, "")

        # 两个不同前缀、不同密钥，仍然共享同一个预算
        assert clock.t == 1006.0


def test_without_a_limiter_calls_are_not_paced(tmp_path, monkeypatch):
    """没配限速时不该凭空引入延迟 —— 测试和一次性脚本要能全速跑。"""
    clock = Clock()
    with FakeApi() as api:
        monkeypatch.setattr(sign, "API_BASE", api.base)
        sign.set_rate_limiter(None)

        sign.call_api("api/a/b", {}, "")
        sign.call_api("api/a/b", {}, "")

        assert clock.t == 1000.0
