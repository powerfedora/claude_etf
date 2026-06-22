"""
ETF 扫描仪 Web 应用 - 三合一 (扫描报告 / 信号时间线 / 个股查询)
================================================================
启动: python app.py
访问: http://localhost:8088
功能: 每次打开页面实时加载最新数据, 支持在线刷新扫描和个股实时查询
"""
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, Response

from engine import analyze_one, add_emas, EMA_FAST, EMA_MID, EMA_SLOW
from tushare_client import TushareMcpClient
from history import load_snapshots, record_snapshot

app = Flask(__name__)
ROOT = Path(__file__).resolve().parent
SCAN_FILE = ROOT / "last_scan.json"
LIST_FILE = ROOT / "etf_list.txt"
START_DATE = "20240101"
REQUEST_INTERVAL = 1.2

STOCK_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003", "300", "301", "688", "689")

CAT_LEVEL = {
    "回避": 0, "回避-向下变盘": 0,
    "观望": 1, "观望-变盘待定": 1,
    "观望-待周线点头": 2, "观望-变盘待确认": 2,
    "持有/观察": 3,
    "可关注-蚂蚁上树": 4,
    "可关注-向上变盘": 5, "可关注-金叉": 5,
    "可关注-回踩": 6,
}

scan_state = {"running": False, "progress": 0, "total": 0, "message": ""}
_timeline_snapshots = []
_timeline_lock = threading.Lock()


def _json_default(o):
    if hasattr(o, 'item'):
        return o.item()
    if hasattr(o, 'isoformat'):
        return o.isoformat()
    return str(o)


def _is_stock(code):
    return code.zfill(6).startswith(STOCK_PREFIXES)


def _load_list():
    codes = []
    with open(LIST_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").split()
            codes.append((parts[0].zfill(6), parts[1] if len(parts) > 1 else parts[0]))
    return codes


def _get_market(client):
    try:
        end = datetime.now().strftime("%Y%m%d")
        idx = client.fetch_index_daily("000300.SH", START_DATE, end)
        if idx is None or idx.empty:
            return "谨慎档"
        idx = add_emas(idx)
        last = idx.iloc[-1]
        p, ef, em_, es = last["close"], last[f"ema{EMA_FAST}"], last[f"ema{EMA_MID}"], last[f"ema{EMA_SLOW}"]
        if p > ef > em_ > es:
            return "进攻档"
        if p > es and es > idx.iloc[-5][f"ema{EMA_SLOW}"]:
            return "谨慎档"
        if p < es:
            return "防守档"
        return "谨慎档"
    except Exception:
        return "谨慎档"


def _build_chart_data(df, n=120):
    d = add_emas(df)
    rows = []
    for _, r in d.tail(n).iterrows():
        rows.append({
            "date": r["date"].strftime("%Y-%m-%d"),
            "open": round(float(r["open"]), 3),
            "high": round(float(r["high"]), 3),
            "low": round(float(r["low"]), 3),
            "close": round(float(r["close"]), 3),
            "volume": round(float(r["volume"])),
            "ema13": round(float(r[f"ema{EMA_FAST}"]), 3),
            "ema34": round(float(r[f"ema{EMA_MID}"]), 3),
            "ema55": round(float(r[f"ema{EMA_SLOW}"]), 3),
        })
    return rows


# ---------- Background scan ----------

def _save_timeline_snapshot(results, market):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    etfs = []
    for r in results:
        if r.get("error"):
            continue
        etfs.append({
            "code": r["code"], "name": r["name"], "price": r.get("price"),
            "cat": r.get("category", ""), "score": r.get("score", 0),
            "pos": r.get("pos_pct"), "ema34": (r.get("ema_day") or {}).get("EMA34"),
            "verdict": r.get("verdict", ""), "reasons": r.get("reasons", []),
            "month_state": r.get("month_state", ""),
            "week_state": r.get("week_state", ""),
            "day_state": r.get("day_state", ""),
            "day_cross": r.get("day_cross", ""),
        })
    snap = {"ts": ts, "market": market, "etfs": etfs}
    with _timeline_lock:
        _timeline_snapshots.append(snap)

def _run_scan():
    global scan_state
    scan_state = {"running": True, "progress": 0, "total": 0, "message": "初始化..."}
    try:
        client = TushareMcpClient()
        codes = _load_list()
        scan_state["total"] = len(codes)
        scan_state["message"] = "判断大盘..."
        market = _get_market(client)
        scan_state["message"] = f"大盘: {market}"

        results = []
        for i, (code, name) in enumerate(codes):
            scan_state["progress"] = i + 1
            scan_state["message"] = f"[{i+1}/{len(codes)}] {name}"
            try:
                end = datetime.now().strftime("%Y%m%d")
                df = client.fetch_fund_daily(code, START_DATE, end)
                df, _ = _append_realtime_bar(df, code)
                results.append(analyze_one(code, name, df, market))
            except Exception as e:
                results.append({"code": code, "name": name, "error": str(e)})
            time.sleep(REQUEST_INTERVAL)

        data = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "market": market, "results": results}
        SCAN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        try:
            record_snapshot(results, market)
        except Exception:
            pass
        _save_timeline_snapshot(results, market)
        scan_state["message"] = f"完成! {len(results)} 只"
    except Exception as e:
        scan_state["message"] = f"失败: {e}"
    finally:
        scan_state["running"] = False


# ---------- 实时行情 (通达信 TCP → 东方财富 HTTPS 双通道) ----------

_tdx_client = None
_tdx_lock = threading.Lock()
_tdx_failed = False


def _get_tdx_client():
    global _tdx_client, _tdx_failed
    if _tdx_failed:
        return None
    if _tdx_client is not None:
        return _tdx_client
    with _tdx_lock:
        if _tdx_client is not None:
            return _tdx_client
        try:
            from eltdx import TdxClient
            c = TdxClient(timeout=6.0, probe_hosts=True, probe_timeout=1.2)
            c.connect()
            _tdx_client = c
        except Exception:
            _tdx_failed = True
            _tdx_client = None
    return _tdx_client


def _tdx_code(code):
    code = code.zfill(6)
    if code.startswith(("5", "6", "9")):
        return "sh" + code
    return "sz" + code


def _fetch_realtime_tdx(codes):
    client = _get_tdx_client()
    if client is None:
        return {}
    try:
        tdx_codes = [_tdx_code(c) for c in codes]
        records = client.get_quote(tdx_codes)
        if not records:
            return {}
        if hasattr(records, 'records'):
            records = records.records
        result = {}
        for rec in records:
            raw_code = rec.code
            price = rec.last_price
            pre_close = rec.last_close_price if hasattr(rec, 'last_close_price') else (rec.pre_close_price if hasattr(rec, 'pre_close_price') else 0)
            change_pct = round((price - pre_close) / pre_close * 100, 2) if pre_close else 0
            result[raw_code] = {
                "price": price,
                "open": rec.open_price,
                "high": rec.high_price,
                "low": rec.low_price,
                "pre_close": pre_close,
                "volume": rec.total_hand * 100 if hasattr(rec, 'total_hand') else 0,
                "amount": rec.amount if hasattr(rec, 'amount') else 0,
                "change_pct": change_pct,
            }
        return result
    except Exception:
        global _tdx_client
        _tdx_client = None
        return {}


def _em_secid(code):
    code = code.zfill(6)
    if code.startswith(("5", "6", "9")):
        return "1." + code
    return "0." + code


def _fetch_realtime_eastmoney(codes):
    """东方财富 HTTPS 行情 (备用通道, 无需认证)"""
    import requests as _req
    if not codes:
        return {}
    secids = ",".join(_em_secid(c) for c in codes)
    try:
        url = (
            "https://push2.eastmoney.com/api/qt/ulist.np/get"
            "?fields=f12,f14,f2,f3,f15,f16,f17,f6,f5,f18"
            "&secids=" + secids
        )
        r = _req.get(url, timeout=8, headers={"Referer": "https://quote.eastmoney.com"})
        data = r.json().get("data", {})
        rows = data.get("diff") if data else None
        if not rows:
            return {}
        result = {}
        for item in rows:
            raw_code = item.get("f12", "")
            price = item.get("f2")
            if price is None or price == "-":
                continue
            price = float(price) / 100 if isinstance(price, int) else float(price)
            pre_close = item.get("f18")
            pre_close = float(pre_close) / 100 if isinstance(pre_close, int) and pre_close else 0
            high = item.get("f15")
            high = float(high) / 100 if isinstance(high, int) and high else price
            low = item.get("f16")
            low = float(low) / 100 if isinstance(low, int) and low else price
            opn = item.get("f17")
            opn = float(opn) / 100 if isinstance(opn, int) and opn else price
            vol = item.get("f5", 0)
            vol = int(vol) * 100 if vol and vol != "-" else 0
            change_pct = item.get("f3")
            change_pct = float(change_pct) / 100 if isinstance(change_pct, int) else (float(change_pct) if change_pct and change_pct != "-" else 0)
            result[raw_code] = {
                "price": price,
                "open": opn,
                "high": high,
                "low": low,
                "pre_close": pre_close,
                "volume": vol,
                "amount": float(item.get("f6", 0)) if item.get("f6") and item.get("f6") != "-" else 0,
                "change_pct": round(change_pct, 2),
            }
        return result
    except Exception:
        return {}


def fetch_realtime(codes):
    """批量获取实时行情, 优先通达信 TCP, 失败回退东方财富 HTTPS"""
    if not codes:
        return {}
    result = _fetch_realtime_tdx(codes)
    if not result:
        result = _fetch_realtime_eastmoney(codes)
    return result


def _fetch_kline_eastmoney(code, limit=5):
    """从东方财富获取最近几根日K线 (HTTPS), 用于补齐 Tushare 延迟"""
    import pandas as pd
    import requests as _req
    secid = _em_secid(code)
    try:
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            f"?secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
            f"&klt=101&fqt=1&end=20500101&lmt={limit}"
        )
        r = _req.get(url, timeout=8, headers={"Referer": "https://quote.eastmoney.com"})
        data = r.json().get("data", {})
        klines = data.get("klines") if data else None
        if not klines:
            return pd.DataFrame()
        rows = []
        for line in klines:
            parts = line.split(",")
            if len(parts) < 6:
                continue
            rows.append({
                "date": pd.Timestamp(parts[0]),
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
            })
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


def _append_realtime_bar(df, code):
    """用实时行情 + 东方财富日K 补上 Tushare 缺失的交易日数据"""
    import pandas as pd
    last_date = df["date"].iloc[-1] if not df.empty else pd.Timestamp("2000-01-01")
    today = pd.Timestamp(datetime.now().date())
    if today.weekday() >= 5:
        today = today - pd.Timedelta(days=today.weekday() - 4)

    gap_days = (today - last_date).days
    q = None
    if gap_days > 1:
        extra = _fetch_kline_eastmoney(code, limit=gap_days + 2)
        if not extra.empty:
            new_rows = extra[extra["date"] > last_date]
            if not new_rows.empty:
                df = pd.concat([df, new_rows], ignore_index=True)
                last_date = df["date"].iloc[-1]

    live = fetch_realtime([code])
    q = live.get(code)
    if q and q.get("price"):
        if today > last_date and today.weekday() < 5:
            new_row = pd.DataFrame([{
                "date": today,
                "open": q["open"] if q["open"] else q["price"],
                "high": q["high"] if q["high"] else q["price"],
                "low": q["low"] if q["low"] else q["price"],
                "close": q["price"],
                "volume": q["volume"],
            }])
            df = pd.concat([df, new_row], ignore_index=True)
        elif today == last_date:
            df.loc[df.index[-1], "close"] = q["price"]
            if q["high"] and q["high"] > df.iloc[-1]["high"]:
                df.loc[df.index[-1], "high"] = q["high"]
            if q["low"] and q["low"] < df.iloc[-1]["low"]:
                df.loc[df.index[-1], "low"] = q["low"]
            if q["volume"]:
                df.loc[df.index[-1], "volume"] = q["volume"]

    return df, q


# ---------- API routes ----------

@app.route("/")
def index():
    return MAIN_HTML


@app.route("/api/report")
def api_report():
    if SCAN_FILE.exists():
        data = json.loads(SCAN_FILE.read_text(encoding="utf-8"))
        codes = [r["code"] for r in data.get("results", []) if not r.get("error")]
        live = fetch_realtime(codes)
        if live:
            for r in data["results"]:
                q = live.get(r.get("code", ""))
                if q:
                    r["live_price"] = q["price"]
                    r["live_change"] = q["change_pct"]
            data["has_live"] = True
        return jsonify(data)
    return jsonify({"ts": None, "market": None, "results": []})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    if scan_state["running"]:
        return jsonify({"status": "already_running"})
    threading.Thread(target=_run_scan, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/scan/status")
def api_scan_status():
    return jsonify(scan_state)


@app.route("/api/timeline")
def api_timeline():
    snapshots = []
    try:
        snapshots = load_snapshots()
    except Exception:
        pass

    with _timeline_lock:
        snapshots = snapshots + list(_timeline_snapshots)

    if not snapshots and SCAN_FILE.exists():
        try:
            data = json.loads(SCAN_FILE.read_text(encoding="utf-8"))
            ts = data.get("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            market = data.get("market", "")
            etfs = []
            for r in data.get("results", []):
                if r.get("error"):
                    continue
                etfs.append({
                    "code": r["code"], "name": r["name"], "price": r.get("price"),
                    "cat": r.get("category", ""), "score": r.get("score", 0),
                    "pos": r.get("pos_pct"), "ema34": (r.get("ema_day") or {}).get("EMA34"),
                    "verdict": r.get("verdict", ""), "reasons": r.get("reasons", []),
                    "month_state": r.get("month_state", ""),
                    "week_state": r.get("week_state", ""),
                    "day_state": r.get("day_state", ""),
                    "day_cross": r.get("day_cross", ""),
                })
            if etfs:
                snapshots.append({"ts": ts, "market": market, "etfs": etfs})
        except Exception:
            pass

    if not snapshots:
        return jsonify({"days": [], "etfs": [], "empty": True})

    by_day = {}
    for snap in snapshots:
        by_day[snap["ts"][:10]] = snap
    days = sorted(by_day.keys())

    etf_map = {}
    for d in days:
        snap = by_day[d]
        market = snap.get("market", "")
        for e in snap.get("etfs", []):
            code = e["code"]
            if code not in etf_map:
                etf_map[code] = {"name": e["name"], "code": code, "points": []}
            etf_map[code]["points"].append({
                "date": d, "price": e.get("price"), "cat": e.get("cat", ""),
                "score": e.get("score", 0), "pos": e.get("pos"), "ema34": e.get("ema34"),
                "market": market, "verdict": e.get("verdict", ""),
                "reasons": e.get("reasons", []),
                "month_state": e.get("month_state", ""), "week_state": e.get("week_state", ""),
                "day_state": e.get("day_state", ""), "day_cross": e.get("day_cross", ""),
            })

    etf_list = sorted(etf_map.values(),
                      key=lambda x: (-CAT_LEVEL.get(x["points"][-1]["cat"], 1), -x["points"][-1].get("score", 0)))
    return jsonify({"days": days, "etfs": etf_list})


# ---------- 股票名称搜索 (拼音首字母 + 中文 + 代码) ----------

_stock_list = []
_stock_list_lock = threading.Lock()
_stock_list_loaded = False


def _pinyin_initials(name):
    try:
        from pypinyin import pinyin, Style
        return "".join(p[0][0] for p in pinyin(name, style=Style.FIRST_LETTER)).lower()
    except Exception:
        return ""


def _load_stock_list():
    global _stock_list, _stock_list_loaded
    if _stock_list_loaded:
        return _stock_list
    with _stock_list_lock:
        if _stock_list_loaded:
            return _stock_list
        items = []
        etf_codes = set()
        try:
            for code, name in _load_list():
                py = _pinyin_initials(name)
                items.append({"code": code, "name": name, "py": py, "type": "ETF"})
                etf_codes.add(code)
        except Exception:
            pass
        try:
            client = TushareMcpClient()
            for api_name, stype in [("stock_basic", "个股")]:
                rows = client.call_tool(api_name, {
                    "exchange": "",
                    "list_status": "L",
                    "fields": ["ts_code", "name"],
                })
                for r in (rows or []):
                    code = r.get("ts_code", "")[:6]
                    name = r.get("name", "")
                    if code and name and code not in etf_codes:
                        py = _pinyin_initials(name)
                        items.append({"code": code, "name": name, "py": py, "type": stype})
        except Exception:
            pass
        try:
            rows = client.call_tool("fund_basic", {
                "market": "E",
                "status": "L",
                "fields": ["ts_code", "name"],
            })
            for r in (rows or []):
                code = r.get("ts_code", "")[:6]
                name = r.get("name", "")
                if code and name and code not in etf_codes:
                    py = _pinyin_initials(name)
                    items.append({"code": code, "name": name, "py": py, "type": "ETF"})
        except Exception:
            pass
        _stock_list = items
        _stock_list_loaded = True
    return _stock_list


def _do_load_stock_list_bg():
    _load_stock_list()

threading.Thread(target=_do_load_stock_list_bg, daemon=True).start()


@app.route("/api/suggest")
def api_suggest():
    q = request.args.get("q", "").strip().lower()
    if not q or len(q) < 1:
        return jsonify([])
    stocks = _load_stock_list()
    results = []
    for s in stocks:
        if (q in s["code"]
                or q in s["name"].lower()
                or q in s["py"]
                or s["py"].startswith(q)):
            results.append(s)
            if len(results) >= 15:
                break
    return jsonify(results)


def _resolve_code(raw):
    """如果输入不是纯数字, 尝试通过名称/拼音匹配到股票代码"""
    raw = raw.strip()
    if raw.isdigit():
        return raw.zfill(6), None
    q = raw.lower()
    stocks = _load_stock_list()
    for s in stocks:
        if s["name"] == raw or s["py"] == q:
            return s["code"], s["name"]
    for s in stocks:
        if q in s["name"].lower() or s["py"].startswith(q):
            return s["code"], s["name"]
    return raw.zfill(6), None


@app.route("/api/lookup")
def api_lookup():
    raw_input = request.args.get("code", "").strip()
    name_input = request.args.get("name", "").strip()
    code, resolved_name = _resolve_code(raw_input)
    name = name_input or resolved_name or code
    if len(code) != 6 or not code.isdigit():
        return jsonify({"error": "未找到匹配的股票, 请输入6位代码或选择建议项"}), 400

    try:
        client = TushareMcpClient()
        market = _get_market(client)
        end = datetime.now().strftime("%Y%m%d")
        df = client.fetch_stock_daily(code, START_DATE, end) if _is_stock(code) \
            else client.fetch_fund_daily(code, START_DATE, end)
        if df is None or df.empty:
            return jsonify({"error": "数据为空, 请检查代码"})
        df, q = _append_realtime_bar(df, code)
        result = analyze_one(code, name, df, market)
        result["_market"] = market
        result["_type"] = "个股" if _is_stock(code) else "ETF"
        chart = _build_chart_data(df)
        if q:
            result["live_price"] = q["price"]
            result["live_change"] = q["change_pct"]
        return Response(
            json.dumps({"result": result, "chart": chart}, ensure_ascii=False, default=_json_default),
            mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/constituents")
def api_constituents():
    code = request.args.get("code", "").strip().zfill(6)
    if not code or len(code) != 6:
        return jsonify({"error": "无效代码"}), 400

    stocks = _fetch_etf_constituents(code)
    if not stocks:
        return jsonify({"stocks": [], "msg": "未找到成份股数据"})

    stock_codes = [s["code"] for s in stocks]
    quotes = _fetch_realtime_eastmoney(stock_codes)
    for s in stocks:
        q = quotes.get(s["code"], {})
        s["price"] = q.get("price", 0)
        s["change"] = q.get("change_pct", 0)
        s["volume"] = q.get("volume", 0)
        s["amount"] = q.get("amount", 0)
        s["volume_ratio"] = 0

    vr_map = _fetch_volume_ratio_eastmoney(stock_codes)
    for s in stocks:
        s["volume_ratio"] = vr_map.get(s["code"], 0)

    try:
        client = TushareMcpClient()
        market = _get_market(client)
    except Exception:
        market = "未知"

    for s in stocks:
        try:
            kdf = _fetch_kline_eastmoney(s["code"], limit=200)
            if kdf is not None and len(kdf) >= 55:
                result = analyze_one(s["code"], s["name"], kdf, market)
                s["category"] = result.get("category", "—")
                s["score"] = result.get("score", 0)
                s["verdict"] = result.get("verdict", "")
            else:
                s["category"] = "—"
                s["score"] = 0
                s["verdict"] = "K线数据不足"
        except Exception:
            s["category"] = "—"
            s["score"] = 0
            s["verdict"] = ""

    return Response(
        json.dumps({"stocks": stocks}, ensure_ascii=False, default=_json_default),
        mimetype="application/json")


def _fetch_etf_constituents(code):
    import requests as _req
    secid = _em_secid(code)
    try:
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get"
            f"?np=1&fltt=2&invt=2&fid=f3&fs=b:{secid}&pn=1&pz=200"
            "&fields=f12,f14"
        )
        r = _req.get(url, timeout=10, headers={
            "Referer": "https://quote.eastmoney.com",
            "User-Agent": "Mozilla/5.0",
        })
        data = r.json().get("data", {})
        rows = data.get("diff") if data else None
        if not rows:
            return []
        return [{"code": str(item.get("f12", "")), "name": item.get("f14", "")} for item in rows if item.get("f12")]
    except Exception:
        return []


def _fetch_volume_ratio_eastmoney(codes):
    import requests as _req
    if not codes:
        return {}
    secids = ",".join(_em_secid(c) for c in codes)
    try:
        url = (
            "https://push2.eastmoney.com/api/qt/ulist.np/get"
            "?fields=f12,f10&secids=" + secids
        )
        r = _req.get(url, timeout=8, headers={"Referer": "https://quote.eastmoney.com"})
        data = r.json().get("data", {})
        rows = data.get("diff") if data else None
        if not rows:
            return {}
        result = {}
        for item in rows:
            c = item.get("f12", "")
            vr = item.get("f10")
            if c and vr and vr != "-":
                result[c] = round(float(vr) / 100, 2) if isinstance(vr, int) else round(float(vr), 2)
        return result
    except Exception:
        return {}


PORTFOLIO_FILE = ROOT / "portfolio.json"


@app.route("/api/portfolio")
def api_portfolio():
    from history import load_trades
    if not PORTFOLIO_FILE.exists():
        return jsonify({"error": "未找到 portfolio.json"})
    try:
        pf = json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return jsonify({"error": f"portfolio.json 解析失败: {e}"})

    positions = pf.get("positions", [])
    cap = pf.get("capital", 0)

    codes = [p["code"] for p in positions]
    quotes = _fetch_realtime_eastmoney(codes)

    try:
        client = TushareMcpClient()
        market = _get_market(client)
    except Exception:
        market = "未知"

    result_positions = []
    filled_sum = 0
    for p in positions:
        code = p["code"]
        q = quotes.get(code, {})
        price = q.get("price") or 0
        live_change = q.get("change_pct", 0)
        filled = p.get("filled", 0) or 0
        filled_sum += filled
        cost = p.get("cost", 0) or 0

        cat = "—"
        ema34 = None
        pos_pct = None
        verdict = ""
        score = 0
        try:
            kdf = _fetch_kline_eastmoney(code, limit=200)
            if kdf is not None and len(kdf) >= 55:
                analysis = analyze_one(code, p.get("name", code), kdf, market)
                cat = analysis.get("category", "—")
                ema34 = (analysis.get("ema_day") or {}).get("EMA34")
                pos_pct = analysis.get("pos_pct")
                verdict = analysis.get("verdict", "")
                score = analysis.get("score", 0)
        except Exception:
            pass

        stop_txt = ""
        stop_alert = ""
        if price and ema34:
            stop_txt = str(ema34)
            if price < ema34:
                stop_alert = "warn"
                stop_alert_text = f"⚠跌破EMA34"
            else:
                d = (price - ema34) / ema34 * 100
                stop_alert = "ok"
                stop_alert_text = f"距止损 +{d:.1f}%"
        else:
            stop_alert = ""
            stop_alert_text = "—"

        pl_pct = None
        if cost and price:
            pl_pct = round((price - cost) / cost * 100, 1)

        result_positions.append({
            "code": code,
            "name": p.get("name", ""),
            "price": price,
            "live_change": live_change,
            "category": cat,
            "score": score,
            "pos_pct": pos_pct,
            "target_pct": p.get("target_pct", 0),
            "target_amt": p.get("target_amt", 0),
            "first_buy": p.get("first_buy", 0),
            "filled": filled,
            "cost": cost,
            "pl_pct": pl_pct,
            "ema34": ema34,
            "stop_alert": stop_alert,
            "stop_alert_text": stop_alert_text,
            "status": p.get("status", ""),
            "verdict": verdict,
        })

    cash = cap - filled_sum
    invested_pct = round(filled_sum / cap * 100, 1) if cap else 0

    trades = []
    try:
        raw_trades = load_trades()
        for t in reversed(raw_trades[-30:]):
            trades.append({
                "ts": t.get("ts", ""),
                "code": t.get("code", ""),
                "name": t.get("name", ""),
                "action": t.get("action", ""),
                "amount": t.get("amount", 0),
                "price": t.get("price", 0),
                "after_filled": t.get("after_filled", 0),
                "after_cost": t.get("after_cost", 0),
                "note": t.get("note", ""),
            })
    except Exception:
        pass

    return Response(
        json.dumps({
            "capital": cap,
            "filled": filled_sum,
            "invested_pct": invested_pct,
            "cash": cash,
            "cash_pct": pf.get("cash_pct", 0),
            "market": market,
            "positions": result_positions,
            "trades": trades,
        }, ensure_ascii=False, default=_json_default),
        mimetype="application/json")


# ---------- Main HTML (SPA) ----------

MAIN_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ETF 扫描仪</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"PingFang SC","Helvetica Neue",sans-serif;background:#f0f2f5;color:#1d1d1f;min-height:100vh}
nav{background:#fff;display:flex;align-items:center;padding:0 24px;box-shadow:0 1px 4px rgba(0,0,0,.08);position:sticky;top:0;z-index:100}
nav .brand{font-size:17px;font-weight:700;margin-right:24px;padding:14px 0;white-space:nowrap}
nav .tabs{display:flex;gap:0}
nav .tab{padding:14px 20px;font-size:14px;cursor:pointer;border-bottom:3px solid transparent;transition:all .15s;color:#666}
nav .tab:hover{color:#1890ff}
nav .tab.active{color:#1890ff;border-color:#1890ff;font-weight:600}
.page{display:none;padding:20px 24px;max-width:1400px;margin:0 auto}
.page.active{display:block}
.card{background:#fff;border-radius:12px;padding:16px 20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card h2{font-size:15px;margin-bottom:12px}
button{padding:8px 18px;border:none;border-radius:8px;font-size:13px;cursor:pointer;transition:all .15s}
.btn-primary{background:#1890ff;color:#fff}
.btn-primary:hover{background:#40a9ff}
.btn-primary:disabled{background:#bbb;cursor:not-allowed}
input[type=text]{padding:8px 14px;border:1px solid #ddd;border-radius:8px;font-size:14px;outline:none}
input[type=text]:focus{border-color:#1890ff}
table{width:100%;border-collapse:collapse}
th{background:#fafafa;text-align:left;padding:10px;font-size:12px;color:#888;font-weight:600}
td{padding:10px;border-top:1px solid #f0f0f0;font-size:13px;vertical-align:top}
.badge{display:inline-block;padding:2px 10px;border-radius:5px;font-size:11px;font-weight:600;color:#fff;white-space:nowrap}
.meta{color:#888;font-size:12px}
.progress{background:#f0f0f0;border-radius:8px;height:6px;margin:8px 0;overflow:hidden}
.progress-bar{height:100%;background:#1890ff;border-radius:8px;transition:width .3s}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.fbtn{padding:4px 12px;border-radius:16px;font-size:12px;border:1px solid #ddd;background:#fff;cursor:pointer}
.fbtn:hover{border-color:#1890ff;color:#1890ff}
.fbtn.active{background:#1890ff;color:#fff;border-color:#1890ff}
#lookup-input{width:220px}
#lookup-chart{width:100%;height:480px}
#lookup-wrap{display:flex;gap:16px;align-items:flex-start}
#lookup-sidebar{width:200px;flex-shrink:0;position:sticky;top:70px}
#lookup-main{flex:1;min-width:0}
#lookup-history{list-style:none;max-height:calc(100vh - 200px);overflow-y:auto}
#lookup-history li{padding:10px 14px;cursor:pointer;border-bottom:1px solid #f0f0f0;font-size:13px;transition:background .12s;display:flex;justify-content:space-between;align-items:center}
#lookup-history li:hover{background:#f5f7fa}
#lookup-history li.active{background:#e6f7ff;border-left:3px solid #1890ff}
#lookup-history .h-code{font-weight:600}
#lookup-history .h-name{color:#888;font-size:11px}
#lookup-history .h-cat{font-size:10px;white-space:nowrap}
#lookup-history .h-remove{color:#ccc;font-size:14px;padding:0 4px;cursor:pointer;visibility:hidden}
#lookup-history li:hover .h-remove{visibility:visible}
#lookup-history .h-remove:hover{color:#e53935}
@media(max-width:768px){#lookup-wrap{flex-direction:column}#lookup-sidebar{width:100%;position:static}#lookup-history{max-height:160px}}
.suggest-wrap{position:relative;display:inline-block}
.suggest-drop{position:absolute;top:100%;left:0;width:320px;max-height:300px;overflow-y:auto;background:#fff;border:1px solid #e0e0e0;border-radius:8px;box-shadow:0 6px 20px rgba(0,0,0,.12);z-index:200;display:none;margin-top:4px}
.suggest-drop.show{display:block}
.suggest-item{padding:8px 14px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;font-size:13px;border-bottom:1px solid #f5f5f5}
.suggest-item:last-child{border-bottom:none}
.suggest-item:hover,.suggest-item.active{background:#e6f7ff}
.suggest-item .s-name{font-weight:600}
.suggest-item .s-code{color:#888;font-size:12px;margin-left:6px}
.suggest-item .s-type{color:#bbb;font-size:11px}
.suggest-item em{font-style:normal;color:#1890ff;font-weight:600}
#timeline-chart{width:100%;min-height:400px}
.spin{display:inline-block;width:16px;height:16px;border:2px solid #ddd;border-top-color:#1890ff;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
</style></head><body>
<nav>
  <div class="brand">ETF 扫描仪</div>
  <div class="tabs">
    <div class="tab active" data-page="report">扫描报告</div>
    <div class="tab" data-page="timeline">信号时间线</div>
    <div class="tab" data-page="portfolio">买卖交易表</div>
    <div class="tab" data-page="lookup">个股查询</div>
  </div>
</nav>

<!-- ==================== 扫描报告 ==================== -->
<div class="page active" id="page-report">
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
      <div>
        <h2 style="display:inline">扫描报告</h2>
        <span class="meta" id="report-meta"></span>
      </div>
      <div style="display:flex;align-items:center;gap:10px">
        <span class="meta" id="refresh-timer"></span>
        <button class="btn-primary" id="btn-scan" onclick="startScan()">刷新全量扫描</button>
      </div>
    </div>
    <div id="scan-progress" style="display:none">
      <div class="progress"><div class="progress-bar" id="scan-bar"></div></div>
      <span class="meta" id="scan-msg"><span class="spin"></span>扫描中...</span>
    </div>
  </div>
  <div class="card" id="report-focus"></div>
  <div class="card" style="padding:0;overflow-x:auto">
    <table id="report-table">
      <thead><tr><th>标的</th><th>实时价格</th><th>分类 / 打分</th><th>月/周/日</th><th>位置</th><th>结论 / 信号</th></tr></thead>
      <tbody id="report-body"></tbody>
    </table>
  </div>
</div>

<!-- ==================== 信号时间线 ==================== -->
<div class="page" id="page-timeline">
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
      <h2>信号时间线</h2>
      <div style="display:flex;gap:8px;align-items:center">
        <input type="text" id="tl-search" placeholder="搜索ETF..." style="width:200px">
        <div class="filters" id="tl-filters"></div>
      </div>
    </div>
    <div class="meta" id="tl-meta"></div>
  </div>
  <div class="card" style="padding:8px">
    <div id="timeline-chart"></div>
  </div>
</div>

<!-- ==================== 买卖交易表 ==================== -->
<div class="page" id="page-portfolio">
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
      <h2 style="margin:0" id="pf-title">我的组合</h2>
      <button class="btn-primary" onclick="loadPortfolio()">刷新数据</button>
    </div>
    <div class="meta" id="pf-summary"></div>
  </div>
  <div class="card" id="pf-table-card" style="display:none">
    <div style="overflow-x:auto">
      <table>
        <thead><tr>
          <th>标的</th><th>现价</th><th>当前信号</th><th>月线位置</th>
          <th>目标占比</th><th>目标金额</th><th>今日首笔</th>
          <th>已建仓/浮盈</th><th>止损(EMA34)</th><th>止损提醒</th><th>状态/动作</th>
        </tr></thead>
        <tbody id="pf-body"></tbody>
      </table>
    </div>
    <div class="meta" style="margin-top:8px">止损位取当日日线 EMA34, 每次刷新自动更新; 跌破并转死叉再离场。"已建仓/成交均价(cost)/状态" 在 portfolio.json 中维护。</div>
  </div>
  <div class="card" id="pf-trades-card" style="display:none">
    <h2 id="pf-trades-title">操作记录</h2>
    <div style="overflow-x:auto">
      <table>
        <thead><tr><th>时间</th><th>标的</th><th>方向</th><th>金额</th><th>成交价</th><th>后:已建仓/均价</th><th>备注</th></tr></thead>
        <tbody id="pf-trades-body"></tbody>
      </table>
    </div>
  </div>
  <div id="pf-loading" class="meta" style="text-align:center;padding:40px">点击「刷新数据」加载组合信息</div>
</div>

<!-- ==================== 个股查询 ==================== -->
<div class="page" id="page-lookup">
  <div class="card">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <h2 style="margin:0;white-space:nowrap">个股 / ETF 查询</h2>
      <div class="suggest-wrap">
        <input type="text" id="lookup-input" placeholder="代码 / 拼音首字母 / 中文名" autocomplete="off">
        <div class="suggest-drop" id="suggest-code"></div>
      </div>
      <input type="hidden" id="lookup-name" value="">
      <button class="btn-primary" id="btn-lookup" onclick="doLookup()">查询</button>
      <span class="meta" id="lookup-status"></span>
    </div>
  </div>
  <div id="lookup-wrap">
    <div id="lookup-sidebar">
      <div class="card" style="padding:8px 0">
        <div style="display:flex;justify-content:space-between;align-items:center;padding:4px 14px 8px">
          <span style="font-size:13px;font-weight:600">查询历史</span>
          <span id="history-clear" style="font-size:11px;color:#1890ff;cursor:pointer">清空</span>
        </div>
        <ul id="lookup-history"></ul>
        <div id="history-empty" class="meta" style="padding:16px;text-align:center">暂无记录</div>
      </div>
    </div>
    <div id="lookup-main">
      <div id="lookup-result" style="display:none">
        <div class="card" id="lookup-summary"></div>
        <div class="card">
          <h2>K线 + EMA 13/34/55 (近120日)</h2>
          <div id="lookup-chart"></div>
        </div>
        <div class="card">
          <h2>详细分析</h2>
          <table id="lookup-detail"></table>
        </div>
        <div class="card" id="constituents-card" style="display:none">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px">
            <h2 style="margin:0">成份股明细</h2>
            <span class="meta" id="const-meta"></span>
          </div>
          <div style="overflow-x:auto">
            <table id="constituents-table">
              <thead><tr>
                <th style="cursor:pointer" onclick="sortConst('name')">名称 ↕</th>
                <th style="cursor:pointer" onclick="sortConst('code')">代码 ↕</th>
                <th style="cursor:pointer;text-align:right" onclick="sortConst('price')">现价 ↕</th>
                <th style="cursor:pointer;text-align:right" onclick="sortConst('change')">涨幅% ↕</th>
                <th style="cursor:pointer;text-align:right" onclick="sortConst('volume_ratio')">量比 ↕</th>
                <th style="cursor:pointer" onclick="sortConst('category')">操作建议 ↕</th>
                <th style="cursor:pointer;text-align:right" onclick="sortConst('score')">打分 ↕</th>
              </tr></thead>
              <tbody id="const-body"></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const CAT_COLOR = {
  '可关注-回踩':'#e53935','可关注-金叉':'#e53935','可关注-向上变盘':'#e53935','可关注-蚂蚁上树':'#ff5722',
  '持有/观察':'#fb8c00',
  '观望-变盘待确认':'#ff9800','观望-变盘待定':'#888','观望-待周线点头':'#888','观望':'#888',
  '回避-向下变盘':'#bbb','回避':'#bbb',
};
const CAT_ORDER = {
  '可关注-回踩':0,'可关注-金叉':1,'可关注-向上变盘':2,'可关注-蚂蚁上树':3,
  '持有/观察':4,
  '观望-变盘待确认':5,'观望-变盘待定':6,'观望-待周线点头':7,'观望':8,
  '回避-向下变盘':9,'回避':10,
};
const CAT_LEVEL = {
  '回避':0,'回避-向下变盘':0,'观望':1,'观望-变盘待定':1,
  '观望-待周线点头':2,'观望-变盘待确认':2,'持有/观察':3,
  '可关注-蚂蚁上树':4,'可关注-向上变盘':5,'可关注-金叉':5,'可关注-回踩':6,
};
const LEVEL_LABEL = {0:'回避',1:'观望',2:'待确认',3:'持有/观察',4:'蚂蚁上树',5:'可关注',6:'回踩买'};
const LINE_COLORS = [
  '#e53935','#1e88e5','#43a047','#fb8c00','#8e24aa','#00acc1','#d81b60',
  '#3949ab','#7cb342','#f4511e','#6d4c41','#546e7a','#c0ca33','#00897b',
  '#5e35b1','#039be5','#c62828','#2e7d32','#ef6c00','#4527a0',
  '#00838f','#ad1457','#283593','#558b2f','#bf360c','#4e342e',
  '#37474f','#827717','#004d40','#311b92','#006064','#880e4f',
  '#1a237e','#33691e','#e65100','#3e2723','#263238','#9e9d24',
  '#00695c','#4a148c','#01579b','#b71c1c','#1b5e20','#e64a19','#455a64'
];

// ============ Tab switching ============
document.querySelectorAll('.tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    const page = document.getElementById('page-' + tab.dataset.page);
    page.classList.add('active');
    if (tab.dataset.page === 'report') loadReport();
    if (tab.dataset.page === 'timeline') loadTimeline();
    if (tab.dataset.page === 'portfolio') loadPortfolio();
  };
});

// ============ Report ============
function loadReport() {
  fetch('/api/report').then(r=>r.json()).then(data => {
    if (!data.ts) { document.getElementById('report-meta').textContent = '暂无数据, 点击刷新'; return; }
    document.getElementById('report-meta').textContent =
      '更新于 ' + data.ts + ' · 大盘 ' + data.market;
    const rows = (data.results||[]).filter(r=>!r.error);
    rows.sort((a,b) => (CAT_ORDER[a.category]??9) - (CAT_ORDER[b.category]??9) || b.score - a.score);
    const focus = rows.filter(r => (r.category||'').startsWith('可关注'));
    document.getElementById('report-focus').innerHTML =
      '<h2>🎯 可关注 (' + focus.length + ' 只)</h2>' +
      (focus.length ? focus.map(r =>
        '<span style="display:inline-block;background:#fff0f0;color:#e53935;border:1px solid #ffd0d0;border-radius:20px;padding:5px 12px;margin:3px;font-size:13px">'
        + r.name + ' <b>' + r.score + '分</b></span>').join('') : '<span class="meta">无达标标的</span>');
    const tbody = document.getElementById('report-body');
    tbody.innerHTML = rows.map(r => {
      const c = CAT_COLOR[r.category]||'#888';
      const reasons = (r.reasons||[]).join(' · ') || '—';
      let priceHtml = '<span style="font-weight:600;font-size:15px">'+r.price+'</span>';
      if (r.live_price) {
        const lc = r.live_change >= 0 ? '#e53935' : '#2196f3';
        priceHtml = '<span style="font-weight:700;font-size:16px;color:'+lc+'">'+r.live_price.toFixed(3)+'</span>'
          +'<br><span style="font-size:11px;color:'+lc+'">'+(r.live_change>=0?'+':'')+r.live_change+'%</span>'
          +'<br><span class="meta">昨收 '+r.price+'</span>';
      }
      return '<tr style="border-left:4px solid '+c+'"><td><b>'+r.name+'</b><br><span class="meta">'+r.code+'</span></td>'
        +'<td>'+priceHtml+'</td>'
        +'<td><span class="badge" style="background:'+c+'">'+r.category+'</span><br><span class="meta">打分 '+r.score+'</span></td>'
        +'<td class="meta" style="line-height:1.6">月:'+r.month_state+'<br>周:'+r.week_state+'<br>日:'+r.day_state+' ('+r.day_cross+')</td>'
        +'<td class="meta">'+(r.pos_pct||'—')+'%</td>'
        +'<td style="font-size:12px;line-height:1.6">'+r.verdict+'<br><span class="meta">'+reasons+'</span></td></tr>';
    }).join('');
  });
}

let scanPoll = null;
function startScan() {
  fetch('/api/scan',{method:'POST'}).then(r=>r.json()).then(d => {
    if (d.status==='already_running') return;
    scan_state_running = true;
    document.getElementById('btn-scan').disabled = true;
    document.getElementById('scan-progress').style.display = '';
    scanPoll = setInterval(pollScan, 1500);
  });
}
function pollScan() {
  fetch('/api/scan/status').then(r=>r.json()).then(d => {
    const pct = d.total ? (d.progress/d.total*100) : 0;
    document.getElementById('scan-bar').style.width = pct+'%';
    document.getElementById('scan-msg').innerHTML = d.running
      ? '<span class="spin"></span>' + d.message
      : '✅ ' + d.message;
    if (!d.running) {
      scan_state_running = false;
      clearInterval(scanPoll);
      document.getElementById('btn-scan').disabled = false;
      setTimeout(() => { document.getElementById('scan-progress').style.display='none'; }, 3000);
      loadReport();
    }
  });
}

// ============ Timeline ============
let tlChart = null;
let tlData = null;
let tlFilter = 'all';

function loadTimeline() {
  fetch('/api/timeline').then(r=>r.json()).then(data => {
    tlData = data;
    if (data.empty || !data.etfs || data.etfs.length === 0) {
      document.getElementById('tl-meta').textContent = '';
      document.getElementById('timeline-chart').innerHTML =
        '<div style="text-align:center;padding:80px 20px;color:#888;font-size:16px">'
        + '暂无时间线数据<br><span style="font-size:13px;color:#aaa">请先在「扫描报告」页点击「刷新全量扫描」生成数据</span></div>';
      return;
    }
    document.getElementById('tl-meta').textContent = data.etfs.length + ' 只ETF · ' + data.days.length + ' 个时间点';
    initTlFilters();
    renderTimeline();
  });
}

function initTlFilters() {
  const box = document.getElementById('tl-filters');
  if (box.children.length) return;
  ['all','可关注','持有/观察','观望','回避'].forEach((c,i) => {
    const labels = ['全部','可关注','持有/观察','观望','回避'];
    const btn = document.createElement('span');
    btn.className = 'fbtn' + (c==='all'?' active':'');
    btn.textContent = labels[i];
    btn.onclick = () => {
      box.querySelectorAll('.fbtn').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      tlFilter = c;
      applyTlFilter();
    };
    box.appendChild(btn);
  });
}

function renderTimeline() {
  if (!tlData) return;
  const el = document.getElementById('timeline-chart');

  const q = (document.getElementById('tl-search').value||'').trim().toLowerCase();
  const etfs = tlData.etfs.filter(etf => {
    const latest = etf.points[etf.points.length-1];
    const matchQ = !q || etf.name.toLowerCase().includes(q) || etf.code.includes(q);
    const matchF = tlFilter==='all' || latest.cat.startsWith(tlFilter);
    return matchQ && matchF;
  });

  if (etfs.length === 0) {
    if (tlChart) { tlChart.dispose(); tlChart = null; }
    el.innerHTML = '<div style="text-align:center;padding:60px;color:#aaa">无匹配的ETF</div>';
    return;
  }

  const allDates = tlData.days;
  const etfNames = etfs.map(e => e.name);
  const chartHeight = Math.max(400, etfs.length * 28 + 160);
  el.style.height = chartHeight + 'px';
  el.innerHTML = '';

  if (tlChart) { tlChart.dispose(); tlChart = null; }
  tlChart = echarts.init(el);

  const LEVEL_COLORS = ['#bdbdbd','#9e9e9e','#ffa726','#fb8c00','#ff5722','#e53935','#b71c1c'];
  const heatData = [];
  etfs.forEach((etf, yIdx) => {
    const map = {}; etf.points.forEach(p => { map[p.date] = p; });
    allDates.forEach((d, xIdx) => {
      const p = map[d];
      if (p) heatData.push({ value:[xIdx, yIdx, CAT_LEVEL[p.cat]??1], _detail:p, _name:etf.name+' '+etf.code });
    });
  });

  tlChart.setOption({
    grid: { top:20, right:20, bottom:90, left:130 },
    tooltip: {
      confine:true,
      backgroundColor:'rgba(255,255,255,.98)', borderColor:'#eee', borderWidth:1,
      textStyle:{color:'#333',fontSize:12},
      extraCssText:'max-width:380px;white-space:normal;line-height:1.7;box-shadow:0 4px 16px rgba(0,0,0,.15);border-radius:8px;padding:12px',
      formatter: function(params) {
        const d = params.data; if (!d||!d._detail) return '';
        const p = d._detail, cc = CAT_COLOR[p.cat]||'#888';
        const lv = d.value[2], lbl = LEVEL_LABEL[lv]||'';
        const reasons = (p.reasons||[]).join(' · ')||'—';
        return '<div><div style="font-size:14px;font-weight:600;margin-bottom:6px">'+d._name+'</div>'
          +'<span style="color:#666">'+p.date+'</span> · 大盘 <b>'+(p.market||'—')+'</b><br>'
          +'<div style="border-top:1px solid #f0f0f0;margin:5px 0"></div>'
          +'价格 <b style="font-size:16px">'+p.price+'</b>&nbsp;&nbsp;EMA34 '+(p.ema34||'—')+'<br>'
          +'分类 <span class="badge" style="background:'+cc+'">'+p.cat+'</span> ('+lbl+')'
          +'&nbsp;&nbsp;打分 <b>'+p.score+'</b>&nbsp;&nbsp;位置 '+p.pos+'%<br>'
          +'月 '+(p.month_state||'—')+' · 周 '+(p.week_state||'—')+'<br>'
          +'日 '+(p.day_state||'—')+' ('+(p.day_cross||'—')+')<br>'
          +'<div style="border-top:1px solid #f0f0f0;margin:5px 0"></div>'
          +'<div style="font-size:12px">'+(p.verdict||'—')+'</div>'
          +'<div style="font-size:11px;color:#999;margin-top:4px">'+reasons+'</div></div>';
      }
    },
    xAxis: {
      type:'category', data:allDates, position:'bottom',
      axisLabel:{ fontSize:11, color:'#666', rotate: allDates.length>5?45:0 },
      axisLine:{lineStyle:{color:'#ddd'}}, axisTick:{show:false},
      splitLine:{ show:true, lineStyle:{color:'#f5f5f5'} }
    },
    yAxis: {
      type:'category', data:etfNames, inverse:true,
      axisLabel:{ fontSize:11, color:'#333', width:120, overflow:'truncate' },
      axisLine:{show:false}, axisTick:{show:false},
      splitLine:{ show:true, lineStyle:{color:'#f5f5f5'} }
    },
    visualMap: {
      type:'piecewise', orient:'horizontal', left:'center', bottom:4,
      itemWidth:18, itemHeight:12, textStyle:{fontSize:11},
      pieces:[
        {value:0,label:'回避',color:LEVEL_COLORS[0]},
        {value:1,label:'观望',color:LEVEL_COLORS[1]},
        {value:2,label:'待确认',color:LEVEL_COLORS[2]},
        {value:3,label:'持有/观察',color:LEVEL_COLORS[3]},
        {value:4,label:'蚂蚁上树',color:LEVEL_COLORS[4]},
        {value:5,label:'可关注',color:LEVEL_COLORS[5]},
        {value:6,label:'回踩买',color:LEVEL_COLORS[6]}
      ]
    },
    series: [{
      type:'heatmap', data:heatData,
      label:{ show: allDates.length<=7, fontSize:10, color:'#fff',
        formatter:function(p){ return LEVEL_LABEL[p.value[2]]||''; } },
      itemStyle:{ borderColor:'#fff', borderWidth:2, borderRadius:3 },
      emphasis:{ itemStyle:{ shadowBlur:6, shadowColor:'rgba(0,0,0,.3)' } }
    }]
  }, true);
  new ResizeObserver(()=>{ if(tlChart) tlChart.resize(); }).observe(el);
}

function applyTlFilter() {
  renderTimeline();
}
document.getElementById('tl-search').addEventListener('input', applyTlFilter);

// ============ Portfolio ============
let pfLoaded = false;
function loadPortfolio() {
  document.getElementById('pf-loading').innerHTML = '<span class="spin"></span>加载组合数据...';
  document.getElementById('pf-table-card').style.display = 'none';
  document.getElementById('pf-trades-card').style.display = 'none';
  fetch('/api/portfolio').then(r=>r.json()).then(d => {
    if (d.error) { document.getElementById('pf-loading').textContent = '❌ '+d.error; return; }
    pfLoaded = true;
    document.getElementById('pf-loading').style.display = 'none';

    const wan = v => (v/10000).toFixed(v%10000===0?0:1)+'万';
    document.getElementById('pf-title').textContent = '💼 我的组合';
    document.getElementById('pf-summary').innerHTML =
      '总 <b>'+wan(d.capital)+'</b> ｜ 已投 <b>'+wan(d.filled)+'</b>('+d.invested_pct+'%) ｜ 现金 <b>'+wan(d.cash)+'</b> ｜ 大盘 <b>'+d.market+'</b>';

    // Positions table
    let tbody = '';
    d.positions.forEach(p => {
      const cc = CAT_COLOR[p.category]||'#ccc';
      const priceColor = p.live_change>=0?'#e53935':'#2196f3';
      let plHtml = '';
      if (p.pl_pct !== null && p.pl_pct !== undefined) {
        const plColor = p.pl_pct>=0?'#43a047':'#e53935';
        const plTag = p.pl_pct>=12?' 🎯达止盈':'';
        plHtml = '<br><span style="color:'+plColor+'">'+(p.pl_pct>=0?'+':'')+p.pl_pct+'%'+plTag+'</span>';
      }
      let stopHtml = '—';
      if (p.stop_alert === 'warn') stopHtml = '<span style="color:#e53935;font-weight:700">'+p.stop_alert_text+'</span>';
      else if (p.stop_alert === 'ok') stopHtml = '<span style="color:#43a047">'+p.stop_alert_text+'</span>';

      tbody += '<tr>'
        +'<td><b>'+p.name+'</b><br><span class="meta">'+p.code+'</span></td>'
        +'<td style="font-weight:600;color:'+priceColor+'">'+(p.price?p.price.toFixed(3):'—')+'</td>'
        +'<td><span class="badge" style="background:'+cc+'">'+p.category+'</span></td>'
        +'<td>'+(p.pos_pct!==null?p.pos_pct+'%':'—')+'</td>'
        +'<td><b>'+p.target_pct+'%</b></td>'
        +'<td>'+wan(p.target_amt)+'</td>'
        +'<td>'+(p.first_buy?wan(p.first_buy):'—')+'</td>'
        +'<td><b>'+wan(p.filled)+'</b>'+plHtml+'</td>'
        +'<td>'+(p.ema34||'—')+'</td>'
        +'<td>'+stopHtml+'</td>'
        +'<td style="font-size:12px;line-height:1.6">'+p.status+'</td>'
        +'</tr>';
    });
    // Cash row
    tbody += '<tr style="background:#fafafa">'
      +'<td><b>现金</b></td><td>—</td><td>—</td><td>—</td>'
      +'<td><b>'+(d.cash_pct||'')+'%</b></td><td>—</td><td>—</td>'
      +'<td><b>'+wan(d.cash)+'</b></td><td>—</td><td>—</td>'
      +'<td style="font-size:12px">子弹: 回踩加仓/防守</td></tr>';
    document.getElementById('pf-body').innerHTML = tbody;
    document.getElementById('pf-table-card').style.display = '';

    // Trades
    if (d.trades && d.trades.length) {
      document.getElementById('pf-trades-title').textContent = '📜 操作记录 ('+d.trades.length+' 笔)';
      let trows = '';
      d.trades.forEach(t => {
        const actColor = t.action==='sell'?'#e53935':'#43a047';
        const actLabel = t.action==='sell'?'卖出':'买入';
        trows += '<tr>'
          +'<td class="meta">'+t.ts+'</td>'
          +'<td><b>'+t.name+'</b> <span class="meta">'+t.code+'</span></td>'
          +'<td style="color:'+actColor+';font-weight:600">'+actLabel+'</td>'
          +'<td>'+wan(t.amount)+'</td>'
          +'<td>'+t.price+'</td>'
          +'<td>'+wan(t.after_filled)+'/'+t.after_cost+'</td>'
          +'<td class="meta">'+t.note+'</td></tr>';
      });
      document.getElementById('pf-trades-body').innerHTML = trows;
      document.getElementById('pf-trades-card').style.display = '';
    }
  }).catch(e => {
    document.getElementById('pf-loading').textContent = '❌ 加载失败: '+e;
  });
}

// ============ Lookup ============
let lookupMainChart = null;
let lookupHistory = JSON.parse(localStorage.getItem('lookupHistory')||'[]');
let activeCode = '';

// ---- Autocomplete ----
let suggestTimer = null;
let suggestIdx = -1;
const suggestEl = document.getElementById('suggest-code');
const inputEl = document.getElementById('lookup-input');

function highlightMatch(text, q) {
  if (!q) return text;
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i < 0) return text;
  return text.slice(0,i)+'<em>'+text.slice(i,i+q.length)+'</em>'+text.slice(i+q.length);
}

function showSuggestions(items, q) {
  if (!items.length) { suggestEl.classList.remove('show'); return; }
  suggestIdx = -1;
  suggestEl.innerHTML = items.map((s,i) =>
    '<div class="suggest-item" data-idx="'+i+'" data-code="'+s.code+'" data-name="'+s.name+'">'
    +'<div><span class="s-name">'+highlightMatch(s.name, q)+'</span><span class="s-code">'+highlightMatch(s.code, q)+'</span></div>'
    +'<span class="s-type">'+s.type+'</span></div>'
  ).join('');
  suggestEl.classList.add('show');
  suggestEl.querySelectorAll('.suggest-item').forEach(el => {
    el.onmousedown = e => {
      e.preventDefault();
      pickSuggestion(el.dataset.code, el.dataset.name);
    };
  });
}

function pickSuggestion(code, name) {
  inputEl.value = code;
  document.getElementById('lookup-name').value = name;
  suggestEl.classList.remove('show');
  doLookup();
}

inputEl.addEventListener('input', () => {
  const q = inputEl.value.trim();
  if (q.length < 1) { suggestEl.classList.remove('show'); return; }
  if (/^\d{6}$/.test(q)) { suggestEl.classList.remove('show'); return; }
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(() => {
    fetch('/api/suggest?q='+encodeURIComponent(q))
      .then(r=>r.json()).then(items => showSuggestions(items, q))
      .catch(()=>{});
  }, 200);
});

inputEl.addEventListener('keydown', e => {
  const items = suggestEl.querySelectorAll('.suggest-item');
  if (suggestEl.classList.contains('show') && items.length) {
    if (e.key==='ArrowDown') { e.preventDefault(); suggestIdx = Math.min(suggestIdx+1, items.length-1); items.forEach((el,i) => el.classList.toggle('active', i===suggestIdx)); return; }
    if (e.key==='ArrowUp') { e.preventDefault(); suggestIdx = Math.max(suggestIdx-1, 0); items.forEach((el,i) => el.classList.toggle('active', i===suggestIdx)); return; }
    if (e.key==='Enter' && suggestIdx>=0) { e.preventDefault(); const el=items[suggestIdx]; pickSuggestion(el.dataset.code, el.dataset.name); return; }
    if (e.key==='Escape') { suggestEl.classList.remove('show'); return; }
  }
  if (e.key==='Enter') doLookup();
});

inputEl.addEventListener('blur', () => { setTimeout(()=>suggestEl.classList.remove('show'), 150); });

function saveHistory() { localStorage.setItem('lookupHistory', JSON.stringify(lookupHistory)); }

function renderHistory() {
  const ul = document.getElementById('lookup-history');
  const empty = document.getElementById('history-empty');
  if (!lookupHistory.length) { ul.innerHTML=''; empty.style.display=''; return; }
  empty.style.display='none';
  ul.innerHTML = lookupHistory.map((h,i) => {
    const cc = CAT_COLOR[h.cat]||'#ccc';
    const isActive = h.code === activeCode;
    return '<li class="'+(isActive?'active':'')+'" onclick="lookupFromHistory('+i+')">'
      +'<div><span class="h-code">'+h.name+'</span><br><span class="h-name">'+h.code+'</span></div>'
      +'<div style="text-align:right"><span class="h-cat badge" style="background:'+cc+'">'+h.cat+'</span>'
      +'<span class="h-remove" onclick="event.stopPropagation();removeHistory('+i+')">&times;</span></div></li>';
  }).join('');
}

function addHistory(code, name, cat) {
  lookupHistory = lookupHistory.filter(h => h.code !== code);
  lookupHistory.unshift({code, name, cat});
  if (lookupHistory.length > 30) lookupHistory = lookupHistory.slice(0, 30);
  saveHistory();
  renderHistory();
}

function removeHistory(idx) {
  lookupHistory.splice(idx, 1);
  saveHistory();
  renderHistory();
}

function lookupFromHistory(idx) {
  const h = lookupHistory[idx];
  document.getElementById('lookup-input').value = h.code;
  document.getElementById('lookup-name').value = h.name !== h.code ? h.name : '';
  doLookup();
}

document.getElementById('history-clear').onclick = () => {
  lookupHistory = []; saveHistory(); renderHistory();
};

function doLookup() {
  const code = document.getElementById('lookup-input').value.trim();
  const name = document.getElementById('lookup-name').value.trim() || code;
  if (!code) return;
  activeCode = code;
  document.getElementById('btn-lookup').disabled = true;
  document.getElementById('lookup-status').innerHTML = '<span class="spin"></span>查询中...';
  document.getElementById('lookup-result').style.display = 'none';

  fetch('/api/lookup?code='+encodeURIComponent(code)+'&name='+encodeURIComponent(name))
    .then(r=>r.json()).then(data => {
      document.getElementById('btn-lookup').disabled = false;
      if (data.error) { document.getElementById('lookup-status').textContent = '❌ '+data.error; return; }
      document.getElementById('lookup-status').textContent = '';
      document.getElementById('lookup-result').style.display = '';
      const r = data.result;
      addHistory(r.code, r.name||name, r.category||'');
      renderLookup(r, data.chart);
    }).catch(e => {
      document.getElementById('btn-lookup').disabled = false;
      document.getElementById('lookup-status').textContent = '❌ '+e;
    });
}

renderHistory();

function renderLookup(r, chart) {
  const cc = CAT_COLOR[r.category]||'#888';
  document.getElementById('lookup-summary').innerHTML =
    '<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">'
    +'<div><h2 style="font-size:20px">'+(r.name||r.code)+' <span class="meta">'+r.code+' · '+(r._type||'')+'</span></h2>'
    +'<div class="meta">大盘 '+(r._market||'—')+'</div></div>'
    +'<span class="badge" style="background:'+cc+';font-size:13px;padding:4px 14px">'+r.category+'</span></div>'
    +'<div style="margin-top:12px;font-size:15px">'
    +'打分 <b>'+r.score+'</b>&nbsp;&nbsp;'
    +(r.live_price
      ? (function(){var lc=r.live_change>=0?'#e53935':'#2196f3'; return '实时 <b style="font-size:18px;color:'+lc+'">'+r.live_price.toFixed(3)+'</b> <span style="font-size:12px;color:'+lc+'">'+(r.live_change>=0?'+':'')+r.live_change+'%</span>&nbsp;&nbsp;昨收 '+r.price;})()
      : '现价 <b style="font-size:18px">'+r.price+'</b>')
    +'&nbsp;&nbsp;位置 '+r.pos_pct+'%</div>'
    +'<div style="margin-top:10px;padding:10px 14px;background:#fafafa;border-radius:8px;border-left:4px solid '+cc+';font-size:13px;line-height:1.7">'+r.verdict+'</div>';

  const ema = r.ema_day||{};
  const rows = [
    ['EMA13 / EMA34 / EMA55', (ema.EMA13||'—')+' / '+(ema.EMA34||'—')+' / '+(ema.EMA55||'—')],
    ['月线', r.month_state||'—'], ['周线', r.week_state||'—'],
    ['日线', (r.day_state||'—')+' ('+(r.day_cross||'—')+')'],
    ['周线方向闸', r.week_gate_ok?'✅ 通过':'❌ 未通过'],
    ['周线点头', r.week_nod?'✅ 是':'❌ 否'],
    ['放量', r.is_volume_up?'✅ 是':'❌ 否'],
    ['金叉张口', (r.gap_pct||'—')+'%'],
    ['回踩EMA34', r.is_pullback?'✅ 是':'—'],
    ['蚂蚁上树', r.ant_climb?'✅ 是':'—'],
    ['三线粘合', r.is_stick?('✅ 方向:'+r.stick_dir+' 间距:'+r.stick_spread+'%'):'—'],
    ['打分依据', '<span class="meta">'+ ((r.reasons||[]).join(' · ')||'—') +'</span>'],
  ];
  document.getElementById('lookup-detail').innerHTML = rows.map(([k,v])=>
    '<tr><td style="color:#888;white-space:nowrap;width:130px">'+k+'</td><td>'+v+'</td></tr>').join('');

  // K-line chart
  const dates = chart.map(r=>r.date);
  const ohlc = chart.map(r=>[r.open,r.close,r.low,r.high]);
  const upC='#e53935', dnC='#2196f3';

  const el1 = document.getElementById('lookup-chart');
  if (!lookupMainChart) lookupMainChart = echarts.init(el1);

  lookupMainChart.setOption({
    animation:false,
    grid:{left:60,right:20,top:20,bottom:30},
    xAxis:{type:'category',data:dates,boundaryGap:true,axisLabel:{fontSize:11,color:'#888'},axisLine:{lineStyle:{color:'#ddd'}}},
    yAxis:{scale:true,splitLine:{lineStyle:{color:'#f0f0f0'}},axisLabel:{fontSize:11,color:'#888'}},
    tooltip:{
      trigger:'axis',axisPointer:{type:'cross'},
      backgroundColor:'rgba(255,255,255,.96)',borderColor:'#eee',borderWidth:1,
      textStyle:{color:'#333',fontSize:12},
      formatter:function(params){
        let s='<b>'+params[0].axisValue+'</b><br>';
        params.forEach(p=>{
          if(p.seriesType==='candlestick'){const v=p.data;s+='开 '+v[1]+' 收 '+v[2]+'<br>低 '+v[3]+' 高 '+v[4]+'<br>';}
          else s+='<span style="color:'+p.color+'">●</span> '+p.seriesName+': <b>'+p.data+'</b><br>';
        });
        return s;
      }
    },
    dataZoom:[{type:'inside',xAxisIndex:[0],start:0,end:100}],
    series:[
      {type:'candlestick',data:ohlc,itemStyle:{color:upC,color0:dnC,borderColor:upC,borderColor0:dnC}},
      {name:'EMA13',type:'line',data:chart.map(r=>r.ema13),symbol:'none',lineStyle:{width:1.5,color:'#1e88e5'},z:5},
      {name:'EMA34',type:'line',data:chart.map(r=>r.ema34),symbol:'none',lineStyle:{width:2,color:'#ff9800'},z:5},
      {name:'EMA55',type:'line',data:chart.map(r=>r.ema55),symbol:'none',lineStyle:{width:1.5,color:'#66bb6a'},z:5},
    ]
  }, true);
  setTimeout(()=>{lookupMainChart.resize();},100);

  // Load constituents for ETF
  if (r._type === 'ETF') {
    document.getElementById('constituents-card').style.display = '';
    document.getElementById('const-meta').innerHTML = '<span class="spin"></span>加载成份股...';
    document.getElementById('const-body').innerHTML = '';
    fetch('/api/constituents?code='+encodeURIComponent(r.code))
      .then(res=>res.json()).then(d => {
        if (d.stocks && d.stocks.length) {
          constData = d.stocks;
          constSortKey = 'change'; constSortAsc = false;
          constData.sort((a,b) => (b.change||0) - (a.change||0));
          document.getElementById('const-meta').textContent = d.stocks.length + ' 只成份股';
          renderConstTable();
        } else {
          document.getElementById('const-meta').textContent = d.msg || '未找到成份股';
        }
      }).catch(()=>{ document.getElementById('const-meta').textContent = '加载失败'; });
  } else {
    document.getElementById('constituents-card').style.display = 'none';
  }
}

let constData = [], constSortKey = 'change', constSortAsc = false;

function sortConst(key) {
  if (constSortKey === key) constSortAsc = !constSortAsc;
  else { constSortKey = key; constSortAsc = key === 'name' || key === 'code'; }
  const dir = constSortAsc ? 1 : -1;
  constData.sort((a,b) => {
    let va = a[key], vb = b[key];
    if (typeof va === 'string') return va.localeCompare(vb,'zh') * dir;
    return ((va||0) - (vb||0)) * dir;
  });
  renderConstTable();
}

function renderConstTable() {
  document.getElementById('const-body').innerHTML = constData.map(s => {
    const cc = CAT_COLOR[s.category]||'#ccc';
    const chgColor = s.change > 0 ? '#e53935' : (s.change < 0 ? '#2196f3' : '#333');
    const chgSign = s.change > 0 ? '+' : '';
    return '<tr style="cursor:pointer" onclick="document.getElementById(\\'lookup-input\\').value=\\''+s.code+'\\';doLookup()">'
      +'<td>'+s.name+'</td>'
      +'<td class="meta">'+s.code+'</td>'
      +'<td style="text-align:right;font-weight:600">'+((s.price||0).toFixed(2))+'</td>'
      +'<td style="text-align:right;color:'+chgColor+';font-weight:600">'+chgSign+(s.change||0).toFixed(2)+'%</td>'
      +'<td style="text-align:right">'+(s.volume_ratio||'—')+'</td>'
      +'<td><span class="badge" style="background:'+cc+'">'+( s.category||'—')+'</span></td>'
      +'<td style="text-align:right;font-weight:600">'+(s.score||0)+'</td>'
      +'</tr>';
  }).join('');
}

// ============ Init ============
window.addEventListener('resize', () => {
  if (tlChart) tlChart.resize();
  if (lookupMainChart) lookupMainChart.resize();
});
loadReport();
let scan_state_running = false;
let refreshCountdown = 60;
setInterval(() => {
  refreshCountdown--;
  const el = document.getElementById('refresh-timer');
  const reportActive = document.getElementById('page-report').classList.contains('active');
  if (refreshCountdown <= 0) {
    refreshCountdown = 60;
    if (reportActive && !scan_state_running) loadReport();
  }
  if (reportActive && !scan_state_running) {
    el.textContent = refreshCountdown + 's 后刷新';
  } else {
    el.textContent = '';
  }
}, 1000);
</script></body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8088))
    print("ETF 扫描仪 Web 应用")
    print(f"访问: http://localhost:{port}")
    print("按 Ctrl+C 停止\n")
    app.run(host="0.0.0.0", port=port, debug=False)
