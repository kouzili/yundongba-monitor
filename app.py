#!/usr/bin/env python3
"""韵动吧 网球场监控 - Web 管理后台"""
import collections, json, os, re, signal, socket, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path
import requests
from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
FLOWS_FILE = Path.home() / "captures/yundongba.flows"
MITMDUMP_BIN = "/opt/homebrew/bin/mitmdump"

API_BASE = "https://wxapi.sports8.com.cn"
APP_KEY, METHOD = "17992635", "iOS.v20"

def get_headers():
    """从配置读取 session, 避免硬编码泄露"""
    cfg = load_config()
    return {
        "content-type": "application/json", "accept": "application/json",
        "appversion": "5.0.140", "accept-language": "zh-CN,zh-Hans;q=0.9",
        "user-agent": "app/1 CFNetwork/3826.400.120 Darwin/24.3.0",
        "appsessionid": cfg.get("appsessionid", ""),
    }

def get_user_id():
    return load_config().get("user_id", 0)

STADIUM_NAMES = {
    "1006": "四方体育中心网球馆", "153": "熊猫网球·上海知音苑",
    "160": "达安花园网球场", "175": "绿地世纪城网球场",
    "778": "Our Tennis·古北巨鳄球场", "1128": "唛恩网球中心（东馆）",
    "510": "唛恩网球中心", "461": "达安乒乓桌球馆",
    "581": "熊猫网球·华高绿地公园", "628": "亦新网球室内空调场（古北店）",
    "747": "JOOLA&MLC匹克球体验中心", "760": "七乐网球·杨浦万达店",
    "858": "SPINTONIC网球发球机馆", "925": "虹康绿地网球场",
    "946": "光圈网球·HALOTENNIS", "997": "金智塔网球中心(古北店）",
    "1109": "道格达安网球俱乐部", "477": "上海泰尼士网球中心",
    "964": "CPK TENNIS",
}

DEFAULT_CONFIG = {
    "stadiums": {"1128": "唛恩网球中心（东馆）"},
    "active_stadiums": ["1128"],
    "dates": ["2026-08-08", "2026-08-09"],
    "target_start": 18, "target_end": 21,
    "poll_interval": 30,
    "schedule_signs": {}, "book_signs": {},
    "stadium_dates": {}, "available_dates": {}, "user_id": 0, "appsessionid": "",
    "serverchan_key": "",
}

LOG_BUFFER = collections.deque(maxlen=500)
_log_lock = threading.Lock()

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock: LOG_BUFFER.append(line)
    print(line)

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f: cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg: cfg[k] = v
        return cfg
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def date_to_ts(d):
    return int(datetime.strptime(d, "%Y-%m-%d").timestamp())

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
        return ip
    except: return "127.0.0.1"

_mitm_proc = None
_mitm_lock = threading.Lock()

def mitm_is_alive():
    with _mitm_lock: return _mitm_proc is not None and _mitm_proc.poll() is None

def mitm_start():
    global _mitm_proc
    with _mitm_lock:
        if _mitm_proc and _mitm_proc.poll() is None: return True, "已在运行"
        # 清理端口 8080 上的残留 mitmdump
        import subprocess as _sp
        _sp.run(["pkill", "-f", "mitmdump.*8080"], capture_output=True)
        time.sleep(1)
        try:
            FLOWS_FILE.parent.mkdir(parents=True, exist_ok=True)
            FLOWS_FILE.unlink(missing_ok=True)
            _mitm_proc = subprocess.Popen(
                [MITMDUMP_BIN, "-q", "-p", "8080", "-w", str(FLOWS_FILE)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            if _mitm_proc.poll() is None:
                log("mitmdump 已启动 (8080)"); return True, "已启动"
            return False, "启动失败"
        except Exception as e: return False, str(e)

def mitm_stop():
    global _mitm_proc
    with _mitm_lock:
        if _mitm_proc:
            try: _mitm_proc.terminate(); _mitm_proc.wait(timeout=5)
            except: _mitm_proc.kill()
            _mitm_proc = None
        log("mitmdump 已停止")
    return True

def extract_signs():
    if not FLOWS_FILE.exists() or FLOWS_FILE.stat().st_size == 0:
        return None, "抓包文件为空"
    try:
        r = subprocess.run([MITMDUMP_BIN, "-r", str(FLOWS_FILE), "-p", "9125", "-s", str(Path.home() / "captures/analyze2.py")],
            capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired: return None, "提取超时"
    except Exception as e: return None, str(e)

    text = r.stdout + r.stderr
    schedule_signs, book_signs, stadiums = {}, {}, {}

    # 1. 从响应 body 中提取 stadiumName -> stadiumid 映射
    name_pattern = r'"stadiumName"\s*:\s*"([^"]+)"[^}]*"stadiumid"\s*[:"\s]*(\d+)'
    from_capture = {}
    for m in re.finditer(name_pattern, text):
        name, sid = m.group(1).strip(), m.group(2)
        if name and sid not in from_capture:
            from_capture[sid] = name

    # 2. 从请求 body 提取签名（用 analyze2 格式）
    body_pattern = r'>> BODY:\s*\n(\{[^}]+\})\s*\n\s*\n<< RESPONSE'
    for m in re.finditer(body_pattern, text):
        try: body = json.loads(m.group(1))
        except: continue
        biz = body.get("biz",""); nonce = body.get("nonce","")
        sign = body.get("sign",""); date_ts = body.get("date")
        sid = body.get("stadiumid","")
        if not sign or not date_ts: continue
        if biz == "apiGetStadiumShedule" and sid:
            schedule_signs.setdefault(sid,{})[date_ts] = {"nonce":nonce,"sign":sign}
        if biz == "apiSetOrderField" and sid:
            try:
                ol = json.loads(body.get("orderList","[]"))
                if ol:
                    fid = str(ol[0].get("fieldid","?"))
                    book_signs.setdefault(sid,{}).setdefault(date_ts,{})[fid] = {"nonce":nonce,"sign":sign}
            except: pass

    # 3. 命名: 优先用抓包提取的中文名, 其次用已���映射, 最后用编号
    for sid in schedule_signs:
        stadiums[sid] = from_capture.get(sid) or STADIUM_NAMES.get(sid, f"场地{sid}")
    # 同时提取 appsessionid 和 user_id
    session_id = ""; user_id = 0
    sid_pattern = r'"appsessionid"\s*:\s*"([^"]+)"'
    sm = re.search(sid_pattern, text)
    if sm: session_id = sm.group(1)
    uid_pattern = r'"userid"\s*:\s*(\d+)'
    um = re.search(uid_pattern, text)
    if um: user_id = int(um.group(1))

    if not schedule_signs: return None, "未提取到任何签名"
    return {"stadiums":stadiums,"schedule_signs":schedule_signs,"book_signs":book_signs,"appsessionid":session_id,"user_id":user_id}, None

def call_api(endpoint, extra, sign_info):
    body = {"wCommon":None,"biz":endpoint.rsplit("/",1)[-1],"nonce":sign_info["nonce"],
            "method":METHOD,"ydbsp_app_key":APP_KEY,"sign":sign_info["sign"],**extra}
    try:
        r = requests.post(f"{API_BASE}/{endpoint}", json=body, headers=get_headers(), timeout=15)
        if r.status_code == 200:
            d = r.json()
            if d.get("returnCode")=="0": return d["returnData"]
            return {"error": d.get("returnMsg",f"code={d.get('returnCode')}")}
        return {"error": f"HTTP {r.status_code}"}
    except Exception as e: return {"error":str(e)}

def poll_stadium(sid, date_ts, start_h, end_h, signs):
    si = signs.get(str(sid),{}).get(date_ts) or signs.get(str(sid),{}).get(str(date_ts))
    if not si: return [],[],"缺签名"
    data = call_api("api/ydb/stadium/apiGetStadiumShedule",
                    {"stadiumid":str(sid),"date":date_ts,"userid":get_user_id()}, si)
    if "error" in data: return [],[],data["error"]
    target, all_slots = [],[]
    for f in data.get("fieldList",[]):
        for s in f.get("shedule",[]):
            if s.get("status")!="0": continue
            tp = s.get("timePoint")
            if tp is None: continue
            slot = {"field":f["name"],"fieldid":s["fieldid"],
                    "time":f"{tp:02d}:00","timePoint":tp,"price":s["realPrice"]}
            all_slots.append(slot)
            if start_h <= tp < end_h: target.append(slot)
    return target, all_slots, None

def find_nearest(all_slots, start, end):
    if not all_slots: return None
    best, best_d = None, float("inf")
    for s in all_slots:
        tp = s["timePoint"]
        d = start-tp if tp<start else (tp-(end-1) if tp>=end else 0)
        if d>0 and d<best_d: best_d, best = d, s
    return best

def try_book(sid, date_ts, slot, signs):
    sd = signs.get(str(sid),{}); bi = (sd.get(date_ts) or sd.get(str(date_ts)) or {}).get(str(slot["fieldid"]))
    if not bi: return None, "无签名"
    tp = int(slot["time"].split(":")[0])
    ol = json.dumps([{"cheapFlag":0,"name":slot["field"],"timePoint":tp,
        "discountType":1,"expense":480,"status":"0","fieldid":slot["fieldid"],
        "realPrice":400,"start":slot["time"],"end":f"{tp+2:02d}:00",
        "startTime":tp,"endTime":tp+2}])
    data = call_api("api/ydb/stadium/apiSetOrderField",
        {"userid":get_user_id(),"stadiumid":str(sid),"date":date_ts,"orderList":ol}, bi)
    if "error" in data: return None, data["error"]
    return data.get("orderuid"), f"¥{data.get('realExpense','?')}"

def send_notify(title, content, sckey):
    if not sckey: return
    try: requests.post(f"https://sctapi.ftqq.com/{sckey}.send",data={"title":title,"desp":content},timeout=10)
    except: pass

_monitor_running, _monitor_thread = False, None
_monitor_lock = threading.Lock()

def monitor_loop():
    global _monitor_running
    cfg = load_config()
    sids = cfg["active_stadiums"]; dates_map = cfg.get("stadium_dates", {})
    sh, eh = cfg["target_start"], cfg["target_end"]
    interval = cfg["poll_interval"]
    sign_db = cfg.get("schedule_signs",{}); book_db = cfg.get("book_signs",{})
    stadium_db = cfg["stadiums"]; sckey = cfg.get("serverchan_key","")
    round_num = 0
    log(f"监控启动: {len(sids)}场地, {sum(len(v) for v in dates_map.values() if v)}日期, {sh:02d}:00-{eh:02d}:00")
    while _monitor_running:
        round_num += 1
        now = datetime.now().strftime("%H:%M:%S")
        log(f"--- 第{round_num}轮 {now} ---")
        for sid in sids:
            name = stadium_db.get(str(sid),f"场地{sid}")
            stadium_dates = dates_map.get(str(sid), cfg.get("dates",[]))
            for d in stadium_dates:
                ts = date_to_ts(d)
                target, all_slots, err = poll_stadium(sid, ts, sh, eh, sign_db)
                if err: log(f"  {name} {d} ❌ {err}"); continue
                if target:
                    log(f"  {name} {d} 🎾 空场{len(target)}个:")
                    lines = []
                    for s in target:
                        info = f"{s['field']} {s['time']} ¥{s['price']}"
                        lines.append(info)
                        oid, bmsg = try_book(sid, ts, s, book_db)
                        tag = f"🤖已抢[{oid}]{bmsg}" if oid else "✋需手动"
                        log(f"     🟢 {info} → {tag}")
                    if lines: send_notify(f"🎾 {name} {d} 有空场!","\n".join(lines),sckey)
                else:
                    nearest = find_nearest(all_slots, sh, eh)
                    if nearest:
                        hint = f"距目标{sh-nearest['timePoint']}h" if nearest["timePoint"]<sh else f"距目标{nearest['timePoint']-(eh-1)}h"
                        log(f"  {name} {d} ⚪ 全满 | 最近→{nearest['field']} {nearest['time']} ¥{nearest['price']}({hint})")
                    else: log(f"  {name} {d} ⚪ 全天无空场")
        time.sleep(interval)

def monitor_start():
    global _monitor_running, _monitor_thread
    with _monitor_lock:
        if _monitor_running: return False,"监控已在运行"
        _monitor_running = True
        _monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        _monitor_thread.start(); return True,"监控已启动"

def monitor_stop():
    global _monitor_running
    with _monitor_lock: _monitor_running = False; log("监控已停止")
    return True,"监控已停止"

import logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)
app = Flask(__name__)

@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/status")
def api_status():
    cfg = load_config()
    return jsonify({"mitm_alive":mitm_is_alive(),"monitor_running":_monitor_running,
        "local_ip":get_local_ip(),"proxy_port":8080,"config":cfg})

@app.route("/api/capture/start", methods=["POST"])
def api_capture_start():
    ok,msg = mitm_start()
    return jsonify({"ok":ok,"msg":msg,"ip":get_local_ip(),"port":8080})

@app.route("/api/capture/stop", methods=["POST"])
def api_capture_stop(): mitm_stop(); return jsonify({"ok":True})

@app.route("/api/extract", methods=["POST"])
def api_extract():
    data,err = extract_signs()
    if err: return jsonify({"ok":False,"msg":err})
    cfg = load_config()
    for sid, name in data["stadiums"].items():
        cfg["stadiums"][sid] = name  # 覆盖为最新中文名
    for sid, dates in data["schedule_signs"].items():
        cfg["schedule_signs"].setdefault(sid,{}).update(dates)
    for sid, dates in data["book_signs"].items():
        cfg["book_signs"].setdefault(sid,{}).update(dates)
    # 从签名中提取日期，自动填入 available_dates 和 stadium_dates（默认全选）
    for sid, signs in cfg["schedule_signs"].items():
        date_list = sorted([datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d") for ts in signs])
        cfg["available_dates"][sid] = date_list
        if sid not in cfg["stadium_dates"]:
            cfg["stadium_dates"][sid] = list(date_list)  # 默认全选
    # 同步存入 appsessionid 和 user_id
    if data.get("appsessionid"): cfg["appsessionid"] = data["appsessionid"]
    if data.get("user_id"): cfg["user_id"] = data["user_id"]
    # 新场馆不自动激活（用户手动在页面勾选）
    save_config(cfg)
    log(f"签名提取完成: {len(data['stadiums'])} 个场地")
    return jsonify({"ok":True,"stadiums":data["stadiums"]})

@app.route("/api/config", methods=["GET"])
def api_get_config(): return jsonify(load_config())

@app.route("/api/config", methods=["POST"])
def api_update_config():
    cfg = load_config(); body = request.get_json()
    accepted = ["active_stadiums","dates","target_start","target_end","poll_interval","serverchan_key","stadium_dates","stadiums","schedule_signs","book_signs","available_dates"]
    for key in accepted:
        if key in body: cfg[key] = body[key]
    save_config(cfg)
    active = body.get("active_stadiums", cfg.get("active_stadiums", []))
    sdb = cfg.get("stadiums", {})
    sdates = cfg.get("stadium_dates", {})
    items = []
    for sid in active:
        n = sdb.get(str(sid), f"#{sid}")
        ds = sdates.get(str(sid), [])
        ds_short = [d[5:] for d in ds]
        items.append(f"{n}({','.join(ds_short) if ds_short else '0天'})")
    log(f"活跃场地: {' | '.join(items) if items else '无'}")
    return jsonify({"ok":True})

@app.route("/api/monitor/start", methods=["POST"])
def api_monitor_start():
    ok,msg = monitor_start(); return jsonify({"ok":ok,"msg":msg})

@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop(): monitor_stop(); return jsonify({"ok":True})

@app.route("/api/logs")
def api_logs():
    with _log_lock: lines = list(LOG_BUFFER)
    return jsonify({"logs":lines})

@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    with _log_lock: LOG_BUFFER.clear(); return jsonify({"ok":True})

@app.route("/api/test", methods=["POST"])
def api_test():
    cfg = load_config()
    sids = cfg["active_stadiums"]; dates_map = cfg.get("stadium_dates", {}); sh,eh = cfg["target_start"],cfg["target_end"]
    sign_db = cfg.get("schedule_signs",{}); stadium_db = cfg["stadiums"]
    results = []
    for sid in sids:
        name = stadium_db.get(str(sid),f"场地{sid}")
        stadium_dates = dates_map.get(str(sid), cfg.get("dates",[]))
        for d in stadium_dates:
            ts = date_to_ts(d)
            target,all_slots,err = poll_stadium(sid,ts,sh,eh,sign_db)
            if err: results.append({"stadium":name,"date":d,"error":err}); continue
            nearest = None if target else find_nearest(all_slots,sh,eh)
            results.append({"stadium":name,"date":d,"slots":target,"total_available":len(all_slots),"nearest":nearest})
    return jsonify({"results":results})

def main():
    import argparse
    p = argparse.ArgumentParser(); p.add_argument("--port", type=int, default=5100)
    args = p.parse_args()
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists(): save_config(DEFAULT_CONFIG)
    (BASE_DIR/"templates").mkdir(exist_ok=True)
    log(f"管理后台: http://localhost:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False)

if __name__ == "__main__": main()
