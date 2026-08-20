#!/usr/bin/env python3
"""韵动吧场地监控 - Web 管理后台（纯监控，不下单）

签名本地生成，直接调 API 查排期。不含抓包与下单。
"""
import collections
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import engine
import notify
import sign

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "secret_api": "",             # 签名密钥，用 tools/get_secret.py 取
    "appsessionid": "",           # 若查排期不需要登录态，留空即可
    "userid": 0,
    "stadiums": {},               # {id: name}
    "stadium_priority": [],       # 有序列表，只决定日志与推送里的排序
    "target_start": 18,
    "target_end": 21,
    "start_date": "",
    "end_date": "",
    "poll_interval": 30,
    "cityid": 75,                 # 上海
    "feishu_webhook": "",
    "feishu_secret": "",          # 机器人开启签名校验时才需要
    "fail_alert_rounds": 3,       # 连续多少轮全失败后告警
    "bind_host": "127.0.0.1",
}

# 这些字段不回传给前端，只能写入
SECRET_FIELDS = ("secret_api", "appsessionid", "feishu_webhook", "feishu_secret")

WRITABLE_FIELDS = (
    "target_start", "target_end", "start_date", "end_date", "poll_interval",
    "userid", "cityid", "stadiums", "stadium_priority", "fail_alert_rounds",
) + SECRET_FIELDS

LOG_BUFFER = collections.deque(maxlen=500)
_log_lock = threading.Lock()


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    with _log_lock:
        LOG_BUFFER.append(line)
    print(line)


def tomorrow():
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    if not cfg.get("start_date"):
        cfg["start_date"] = tomorrow()
    if not cfg.get("end_date"):
        cfg["end_date"] = tomorrow()
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def public_config(cfg):
    """给前端的配置：密钥类字段只回传「是否已设置」，不回传值。"""
    safe = {k: v for k, v in cfg.items() if k not in SECRET_FIELDS}
    for field in SECRET_FIELDS:
        safe[f"{field}_set"] = bool(cfg.get(field))
    return safe


# ---- 监控引擎接线 ----
_monitor_running = False
_monitor_thread = None
_monitor_lock = threading.Lock()


def fetch_schedule(stadiumid, date):
    """查一个场地某天的排期并解析。返回 parse_schedule 结果或 {"error": msg}。"""
    cfg = load_config()
    data = sign.get_schedule(stadiumid, sign.date_to_ts(date),
                             cfg.get("userid", 0), cfg.get("appsessionid", ""))
    return sign.parse_schedule(data, cfg["target_start"], cfg["target_end"])


def send_notification(text):
    cfg = load_config()
    return notify.send_text(cfg.get("feishu_webhook", ""), text,
                            cfg.get("feishu_secret", ""))


def monitor_loop():
    cfg = load_config()
    log(f"监控启动: {len(cfg.get('stadium_priority', []))} 场地, "
        f"{cfg['start_date']} ~ {cfg['end_date']}, "
        f"{cfg['target_start']:02d}:00-{cfg['target_end']:02d}:00, "
        f"间隔 {cfg['poll_interval']}s")
    if not cfg.get("secret_api"):
        log("⚠️ 未配置 secret_api，所有查询都会返回「签名错误」。"
            "先跑 tools/get_secret.py")
    if not cfg.get("feishu_webhook"):
        log("⚠️ 未配置 feishu_webhook，只会记日志不会推送")
    engine.run_monitor(load_config, fetch_schedule, send_notification, log,
                       lambda: not _monitor_running, time.sleep)


def monitor_start():
    global _monitor_running, _monitor_thread
    with _monitor_lock:
        if _monitor_running:
            return False, "监控已在运行"
        sign.reload_secrets()
        _monitor_running = True
        _monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        _monitor_thread.start()
        return True, "监控已启动"


def monitor_stop():
    global _monitor_running
    with _monitor_lock:
        _monitor_running = False
        log("监控已停止")
    return True, "监控已停止"


# ---- Flask ----
logging.getLogger("werkzeug").setLevel(logging.ERROR)
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "monitor_running": _monitor_running,
        "config": public_config(load_config()),
    })


@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(public_config(load_config()))


@app.route("/api/config", methods=["POST"])
def api_update_config():
    cfg = load_config()
    body = request.get_json() or {}
    for key in WRITABLE_FIELDS:
        if key not in body:
            continue
        # 密钥类字段：留空表示「不修改」，避免前端把掩码写回去清掉密钥
        if key in SECRET_FIELDS and not body[key]:
            continue
        cfg[key] = body[key]
    save_config(cfg)
    if "secret_api" in body and body["secret_api"]:
        sign.reload_secrets()
        log("签名密钥已更新")
    if any(k in body for k in ("target_start", "target_end", "start_date",
                               "end_date", "poll_interval")):
        names = [cfg["stadiums"].get(str(s), s) for s in cfg["stadium_priority"]]
        log(f"配置已保存: {cfg['start_date']} ~ {cfg['end_date']} | "
            f"{cfg['target_start']:02d}:00-{cfg['target_end']:02d}:00 | "
            f"间隔 {cfg['poll_interval']}s | 场地: {', '.join(map(str, names)) or '无'}")
    return jsonify({"ok": True})


@app.route("/api/search", methods=["POST"])
def api_search():
    body = request.get_json() or {}
    keyword = body.get("keyword", "")
    if not keyword:
        return jsonify({"ok": False, "results": [], "msg": "关键词为空"})
    cfg = load_config()
    if not cfg.get("secret_api"):
        return jsonify({"ok": False, "results": [],
                        "msg": "未配置 secret_api，先跑 tools/get_secret.py"})
    sign.reload_secrets()
    results = sign.search_stadium(keyword, cfg.get("cityid", 75),
                                  cfg.get("appsessionid", ""))
    return jsonify({"ok": True, "results": results})


@app.route("/api/monitor/start", methods=["POST"])
def api_monitor_start():
    ok, msg = monitor_start()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    monitor_stop()
    return jsonify({"ok": True})


@app.route("/api/logs")
def api_logs():
    with _log_lock:
        return jsonify({"logs": list(LOG_BUFFER)})


@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    with _log_lock:
        LOG_BUFFER.clear()
    return jsonify({"ok": True})


@app.route("/api/test", methods=["POST"])
def api_test():
    """按当前配置查一轮，不去重不推送，用来确认链路通不通。"""
    cfg = load_config()
    sign.reload_secrets()
    priority = [str(s) for s in cfg.get("stadium_priority", [])]
    dates = engine.gen_dates(cfg["start_date"], cfg["end_date"])
    result = engine.poll_round(priority, cfg.get("stadiums", {}), dates,
                               fetch_schedule)
    return jsonify({
        "slots": result.slots,
        "failures": [{"stadium": n, "date": d, "error": e}
                     for n, d, e in result.failures],
        "misses": [{"stadium": n, "date": d, "nearest": near}
                   for n, d, near in result.misses],
    })


@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    ok, msg = send_notification("🎾 韵动吧监控 · 推送测试")
    return jsonify({"ok": ok, "msg": msg})


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5100)
    p.add_argument("--host", help="覆盖 config.json 里的 bind_host")
    args = p.parse_args()

    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        log(f"已创建 {CONFIG_FILE.name}")
    cfg = load_config()
    host = args.host or cfg.get("bind_host", "127.0.0.1")

    log(f"管理后台: http://{host}:{args.port}")
    if host == "127.0.0.1":
        log("只监听本机。要从局域网访问，改 config.json 的 bind_host 或加 --host")
    app.run(host=host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
