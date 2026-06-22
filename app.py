"""
ETF 扫描仪 Web 应用 - 三合一 (扫描报告 / 信号时间线 / 个股查询)
================================================================
启动: python app.py
访问: http://localhost:8088
功能: 每次打开页面实时加载最新数据, 支持在线刷新扫描和个股实时查询
"""
import json
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
        scan_state["message"] = f"完成! {len(results)} 只"
    except Exception as e:
        scan_state["message"] = f"失败: {e}"
    finally:
        scan_state["running"] = False


# ---------- API routes ----------

@app.route("/")
def index():
    return MAIN_HTML


@app.route("/api/report")
def api_report():
    if SCAN_FILE.exists():
        data = json.loads(SCAN_FILE.read_text(encoding="utf-8"))
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
    snapshots = load_snapshots()
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


@app.route("/api/lookup")
def api_lookup():
    code = request.args.get("code", "").strip().zfill(6)
    name = request.args.get("name", "") or code
    if len(code) != 6:
        return jsonify({"error": "请输入6位代码"}), 400

    try:
        client = TushareMcpClient()
        market = _get_market(client)
        end = datetime.now().strftime("%Y%m%d")
        df = client.fetch_stock_daily(code, START_DATE, end) if _is_stock(code) \
            else client.fetch_fund_daily(code, START_DATE, end)
        if df is None or df.empty:
            return jsonify({"error": "数据为空, 请检查代码"})
        result = analyze_one(code, name, df, market)
        result["_market"] = market
        result["_type"] = "个股" if _is_stock(code) else "ETF"
        chart = _build_chart_data(df)
        return Response(
            json.dumps({"result": result, "chart": chart}, ensure_ascii=False, default=_json_default),
            mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)})


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
#lookup-input{width:280px}
#lookup-chart{width:100%;height:480px}
#lookup-vol{width:100%;height:120px}
#timeline-chart{width:100%;height:calc(100vh - 180px);min-height:500px}
.spin{display:inline-block;width:16px;height:16px;border:2px solid #ddd;border-top-color:#1890ff;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
</style></head><body>
<nav>
  <div class="brand">ETF 扫描仪</div>
  <div class="tabs">
    <div class="tab active" data-page="report">扫描报告</div>
    <div class="tab" data-page="timeline">信号时间线</div>
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
      <div>
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
      <thead><tr><th>标的</th><th>现价</th><th>分类 / 打分</th><th>月/周/日</th><th>位置</th><th>结论 / 信号</th></tr></thead>
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

<!-- ==================== 个股查询 ==================== -->
<div class="page" id="page-lookup">
  <div class="card">
    <h2 style="margin-bottom:12px">个股 / ETF 查询</h2>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <input type="text" id="lookup-input" placeholder="输入6位代码, 如 601919 或 510210">
      <input type="text" id="lookup-name" placeholder="名称(可选)" style="width:140px">
      <button class="btn-primary" id="btn-lookup" onclick="doLookup()">查询</button>
      <span class="meta" id="lookup-status"></span>
    </div>
  </div>
  <div id="lookup-result" style="display:none">
    <div class="card" id="lookup-summary"></div>
    <div class="card">
      <h2>K线 + EMA 13/34/55 (近120日)</h2>
      <div id="lookup-chart"></div>
      <div id="lookup-vol"></div>
    </div>
    <div class="card">
      <h2>详细分析</h2>
      <table id="lookup-detail"></table>
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
      return '<tr style="border-left:4px solid '+c+'"><td><b>'+r.name+'</b><br><span class="meta">'+r.code+'</span></td>'
        +'<td style="font-weight:600;font-size:15px">'+r.price+'</td>'
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
  if (!tlChart) tlChart = echarts.init(el);

  const allDates = tlData.days;
  const series = tlData.etfs.map((etf,i) => {
    const map = {}; etf.points.forEach(p => { map[p.date] = p; });
    return {
      name: etf.name+' '+etf.code,
      type: 'line', symbol: 'circle', symbolSize: 8,
      connectNulls: true,
      lineStyle: { width: 2 },
      itemStyle: { color: LINE_COLORS[i % LINE_COLORS.length] },
      emphasis: { focus:'series', lineStyle:{width:4} },
      blur: { lineStyle:{opacity:.1}, itemStyle:{opacity:.1} },
      data: allDates.map(d => {
        const p = map[d];
        if (!p) return null;
        return { value: CAT_LEVEL[p.cat]??1, _detail: p };
      })
    };
  });

  tlChart.setOption({
    grid: { top:80, right:30, bottom:50, left:70 },
    legend: {
      type:'scroll', top:5, left:10, right:10,
      textStyle:{fontSize:11}, pageIconSize:12,
      selector:[{type:'all',title:'全选'},{type:'inverse',title:'反选'}]
    },
    tooltip: {
      trigger:'item', confine:true,
      backgroundColor:'rgba(255,255,255,.98)', borderColor:'#eee', borderWidth:1,
      textStyle:{color:'#333',fontSize:12},
      extraCssText:'max-width:380px;white-space:normal;line-height:1.7;box-shadow:0 4px 16px rgba(0,0,0,.15);border-radius:8px;padding:12px',
      formatter: function(params) {
        const p = params.data && params.data._detail;
        if (!p) return params.seriesName;
        const cc = CAT_COLOR[p.cat]||'#888';
        const reasons = (p.reasons||[]).join(' · ')||'—';
        return '<div><div style="font-size:14px;font-weight:600;margin-bottom:6px">'+params.seriesName+'</div>'
          +'<span style="color:#666">'+p.date+'</span> · 大盘 <b>'+(p.market||'—')+'</b><br>'
          +'<div style="border-top:1px solid #f0f0f0;margin:5px 0"></div>'
          +'价格 <b style="font-size:16px">'+p.price+'</b>&nbsp;&nbsp;EMA34 '+(p.ema34||'—')+'<br>'
          +'分类 <span class="badge" style="background:'+cc+'">'+p.cat+'</span>'
          +'&nbsp;&nbsp;打分 <b>'+p.score+'</b>&nbsp;&nbsp;位置 '+p.pos+'%<br>'
          +'月 '+(p.month_state||'—')+' · 周 '+(p.week_state||'—')+'<br>'
          +'日 '+(p.day_state||'—')+' ('+(p.day_cross||'—')+')<br>'
          +'<div style="border-top:1px solid #f0f0f0;margin:5px 0"></div>'
          +'<div style="font-size:12px">'+(p.verdict||'—')+'</div>'
          +'<div style="font-size:11px;color:#999;margin-top:4px">'+reasons+'</div></div>';
      }
    },
    xAxis: { type:'category', data:allDates, axisLabel:{fontSize:11,color:'#888'}, axisLine:{lineStyle:{color:'#ddd'}}, axisTick:{show:false} },
    yAxis: { type:'value', min:-0.3, max:6.5, interval:1,
      axisLabel:{ fontSize:11, color:'#888', formatter:v=>LEVEL_LABEL[v]||'' },
      splitLine:{lineStyle:{color:'#f0f0f0'}}, axisLine:{show:false}, axisTick:{show:false} },
    dataZoom: [
      {type:'inside',xAxisIndex:0,filterMode:'none'},
      {type:'slider',xAxisIndex:0,bottom:8,height:20,filterMode:'none',borderColor:'#ddd',fillerColor:'rgba(24,144,255,.15)'}
    ],
    series: series
  }, true);
  new ResizeObserver(()=>tlChart.resize()).observe(el);
}

function applyTlFilter() {
  if (!tlChart || !tlData) return;
  const q = document.getElementById('tl-search').value.trim().toLowerCase();
  const legend = {};
  tlData.etfs.forEach(etf => {
    const key = etf.name+' '+etf.code;
    const latest = etf.points[etf.points.length-1];
    const matchQ = !q || etf.name.toLowerCase().includes(q) || etf.code.includes(q);
    const matchF = tlFilter==='all' || latest.cat.startsWith(tlFilter);
    legend[key] = matchQ && matchF;
  });
  tlChart.setOption({ legend:{ selected:legend } });
}
document.getElementById('tl-search').addEventListener('input', applyTlFilter);

// ============ Lookup ============
let lookupMainChart = null, lookupVolChart = null;

document.getElementById('lookup-input').addEventListener('keydown', e => { if(e.key==='Enter') doLookup(); });

function doLookup() {
  const code = document.getElementById('lookup-input').value.trim();
  const name = document.getElementById('lookup-name').value.trim() || code;
  if (!code) return;
  document.getElementById('btn-lookup').disabled = true;
  document.getElementById('lookup-status').innerHTML = '<span class="spin"></span>查询中...';
  document.getElementById('lookup-result').style.display = 'none';

  fetch('/api/lookup?code='+encodeURIComponent(code)+'&name='+encodeURIComponent(name))
    .then(r=>r.json()).then(data => {
      document.getElementById('btn-lookup').disabled = false;
      if (data.error) { document.getElementById('lookup-status').textContent = '❌ '+data.error; return; }
      document.getElementById('lookup-status').textContent = '';
      document.getElementById('lookup-result').style.display = '';
      renderLookup(data.result, data.chart);
    }).catch(e => {
      document.getElementById('btn-lookup').disabled = false;
      document.getElementById('lookup-status').textContent = '❌ '+e;
    });
}

function renderLookup(r, chart) {
  const cc = CAT_COLOR[r.category]||'#888';
  document.getElementById('lookup-summary').innerHTML =
    '<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">'
    +'<div><h2 style="font-size:20px">'+(r.name||r.code)+' <span class="meta">'+r.code+' · '+(r._type||'')+'</span></h2>'
    +'<div class="meta">大盘 '+(r._market||'—')+'</div></div>'
    +'<span class="badge" style="background:'+cc+';font-size:13px;padding:4px 14px">'+r.category+'</span></div>'
    +'<div style="margin-top:12px;font-size:15px">'
    +'打分 <b>'+r.score+'</b>&nbsp;&nbsp;现价 <b style="font-size:18px">'+r.price+'</b>&nbsp;&nbsp;位置 '+r.pos_pct+'%</div>'
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
  const el2 = document.getElementById('lookup-vol');
  if (!lookupMainChart) lookupMainChart = echarts.init(el1);
  if (!lookupVolChart) lookupVolChart = echarts.init(el2);

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
        const idx=dates.indexOf(params[0].axisValue);
        if(idx>=0)s+='成交量: '+(chart[idx].volume/10000).toFixed(0)+'万';
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

  lookupVolChart.setOption({
    animation:false,
    grid:{left:60,right:20,top:5,bottom:24},
    xAxis:{type:'category',data:dates,boundaryGap:true,axisLabel:{show:false},axisTick:{show:false},axisLine:{lineStyle:{color:'#eee'}}},
    yAxis:{scale:true,show:false},
    series:[{type:'bar',data:chart.map(r=>({value:r.volume,itemStyle:{color:r.close>=r.open?upC:dnC,opacity:.5}})),barWidth:'60%'}]
  }, true);

  echarts.connect([lookupMainChart, lookupVolChart]);
  setTimeout(()=>{lookupMainChart.resize();lookupVolChart.resize();},100);
}

// ============ Init ============
window.addEventListener('resize', () => {
  if (tlChart) tlChart.resize();
  if (lookupMainChart) lookupMainChart.resize();
  if (lookupVolChart) lookupVolChart.resize();
});
loadReport();
</script></body></html>"""


if __name__ == "__main__":
    print("ETF 扫描仪 Web 应用")
    print("访问: http://localhost:8088")
    print("按 Ctrl+C 停止\n")
    app.run(host="0.0.0.0", port=8088, debug=False)
