"""
ETF 信号时间线: 交互式折线图, 按ETF分类展示每次扫描的历史信号变化
=================================================================
每只ETF一张折线图, X轴=扫描日期, Y轴=信号等级(0-6),
鼠标悬停显示完整分析(分类/打分/结论/买卖理由等)。
"""
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

CAT_LEVEL = {
    "回避": 0, "回避-向下变盘": 0,
    "观望": 1, "观望-变盘待定": 1,
    "观望-待周线点头": 2, "观望-变盘待确认": 2,
    "持有/观察": 3,
    "可关注-蚂蚁上树": 4,
    "可关注-向上变盘": 5, "可关注-金叉": 5,
    "可关注-回踩": 6,
}

CAT_COLOR = {
    "可关注-回踩": "#e53935", "可关注-金叉": "#e53935",
    "可关注-向上变盘": "#e53935", "可关注-蚂蚁上树": "#ff5722",
    "持有/观察": "#fb8c00",
    "观望-变盘待确认": "#ff9800", "观望-变盘待定": "#888",
    "观望-待周线点头": "#888", "观望": "#888",
    "回避-向下变盘": "#bbb", "回避": "#bbb",
}


def build_timeline(out_html=None):
    from history import load_snapshots

    if out_html is None:
        out_html = ROOT / "timeline.html"

    snapshots = load_snapshots()

    by_day = {}
    for snap in snapshots:
        day = snap["ts"][:10]
        by_day[day] = snap
    days = sorted(by_day.keys())
    snaps = [by_day[d] for d in days]

    etf_map = {}
    for snap in snaps:
        date = snap["ts"][:10]
        market = snap.get("market", "")
        for e in snap.get("etfs", []):
            code = e["code"]
            if code not in etf_map:
                etf_map[code] = {"name": e["name"], "code": code, "points": []}
            etf_map[code]["points"].append({
                "date": date,
                "price": e.get("price"),
                "cat": e.get("cat", ""),
                "score": e.get("score", 0),
                "pos": e.get("pos"),
                "ema34": e.get("ema34"),
                "market": market,
                "verdict": e.get("verdict", ""),
                "reasons": e.get("reasons", []),
                "month_state": e.get("month_state", ""),
                "week_state": e.get("week_state", ""),
                "day_state": e.get("day_state", ""),
                "day_cross": e.get("day_cross", ""),
            })

    etf_list = sorted(
        etf_map.values(),
        key=lambda x: (
            -CAT_LEVEL.get(x["points"][-1]["cat"], 1),
            -x["points"][-1].get("score", 0),
        ),
    )

    data_json = json.dumps(etf_list, ensure_ascii=False)
    cat_color_json = json.dumps(CAT_COLOR, ensure_ascii=False)
    cat_level_json = json.dumps(CAT_LEVEL, ensure_ascii=False)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = _build_html(ts, len(etf_list), len(days), data_json, cat_color_json, cat_level_json)

    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    return str(out_html)


def _build_html(ts, n_etfs, n_days, data_json, cat_color_json, cat_level_json):
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ETF 信号时间线</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang SC","Helvetica Neue",sans-serif;
      background:#f0f2f5;color:#1d1d1f;min-height:100vh}}
.header{{background:#fff;padding:16px 24px;box-shadow:0 1px 4px rgba(0,0,0,.08);
         position:sticky;top:0;z-index:100}}
.header h1{{font-size:20px;margin-bottom:4px}}
.meta{{color:#888;font-size:13px;margin-bottom:12px}}
.toolbar{{display:flex;flex-wrap:wrap;gap:10px;align-items:center}}
#search{{flex:1;min-width:200px;max-width:360px;padding:8px 14px;border:1px solid #ddd;
         border-radius:8px;font-size:14px;outline:none;transition:border .2s}}
#search:focus{{border-color:#1890ff}}
.filters{{display:flex;flex-wrap:wrap;gap:6px}}
.fbtn{{padding:4px 12px;border-radius:16px;font-size:12px;border:1px solid #ddd;
       background:#fff;cursor:pointer;transition:all .15s;white-space:nowrap}}
.fbtn:hover{{border-color:#1890ff;color:#1890ff}}
.fbtn.active{{background:#1890ff;color:#fff;border-color:#1890ff}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(370px,1fr));
       gap:16px;padding:20px 24px}}
.card{{background:#fff;border-radius:12px;overflow:hidden;
       box-shadow:0 1px 3px rgba(0,0,0,.06);transition:box-shadow .2s}}
.card:hover{{box-shadow:0 4px 12px rgba(0,0,0,.12)}}
.card-head{{padding:12px 16px 8px;display:flex;justify-content:space-between;align-items:flex-start}}
.card-title{{font-size:15px;font-weight:600}}
.card-code{{font-size:11px;color:#aaa;margin-top:2px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:5px;font-size:11px;
        font-weight:600;color:#fff;white-space:nowrap}}
.card-info{{padding:0 16px 6px;font-size:12px;color:#666;display:flex;gap:12px}}
.chart-box{{width:100%;height:200px}}
.empty{{text-align:center;color:#999;padding:60px 20px;font-size:15px}}
.legend{{display:flex;flex-wrap:wrap;gap:12px;padding:4px 24px 16px;font-size:12px;color:#666}}
.legend-item{{display:flex;align-items:center;gap:4px}}
.legend-dot{{width:10px;height:10px;border-radius:50%;display:inline-block}}
</style></head><body>
<div class="header">
  <h1>ETF 信号时间线</h1>
  <div class="meta">更新于 {ts} · 共 {n_etfs} 只 · {n_days} 个时间点(每日去重取最后一次)</div>
  <div class="toolbar">
    <input type="text" id="search" placeholder="搜索 ETF 名称或代码...">
    <div class="filters" id="filters"></div>
  </div>
</div>
<div class="legend" id="legend"></div>
<div class="grid" id="grid"></div>
<div class="empty" id="empty-msg" style="display:none">无匹配结果</div>

<script>
const DATA = {data_json};
const CAT_COLOR = {cat_color_json};
const CAT_LEVEL = {cat_level_json};

const LEVEL_LABEL = {{0:'回避',1:'观望',2:'待确认',3:'持有',4:'蚂蚁上树',5:'可关注',6:'回踩买'}};
const ZONE_COLORS = [
  {{min:0,max:1,color:'rgba(200,200,200,0.08)'}},
  {{min:1,max:3,color:'rgba(255,152,0,0.06)'}},
  {{min:3,max:6,color:'rgba(229,57,53,0.06)'}},
];

let activeFilter = 'all';
let charts = {{}};

function initFilters() {{
  const cats = ['all','可关注','持有/观察','观望','回避'];
  const labels = ['全部','可关注','持有/观察','观望','回避'];
  const fBox = document.getElementById('filters');
  cats.forEach((c, i) => {{
    const btn = document.createElement('span');
    btn.className = 'fbtn' + (c === 'all' ? ' active' : '');
    btn.textContent = labels[i];
    btn.onclick = () => {{
      document.querySelectorAll('.fbtn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeFilter = c;
      applyFilters();
    }};
    fBox.appendChild(btn);
  }});
}}

function initLegend() {{
  const items = [
    ['可关注-回踩','#e53935'],['可关注-金叉','#e53935'],['可关注-向上变盘','#e53935'],
    ['可关注-蚂蚁上树','#ff5722'],['持有/观察','#fb8c00'],
    ['观望-变盘待确认','#ff9800'],['观望','#888'],['回避','#bbb']
  ];
  const box = document.getElementById('legend');
  items.forEach(([label, color]) => {{
    box.innerHTML += `<span class="legend-item"><span class="legend-dot" style="background:${{color}}"></span>${{label}}</span>`;
  }});
}}

function applyFilters() {{
  const q = document.getElementById('search').value.trim().toLowerCase();
  let visible = 0;
  DATA.forEach(etf => {{
    const card = document.getElementById('card-' + etf.code);
    if (!card) return;
    const latest = etf.points[etf.points.length - 1];
    const matchQ = !q || etf.name.toLowerCase().includes(q) || etf.code.includes(q);
    const matchF = activeFilter === 'all' || latest.cat.startsWith(activeFilter);
    const show = matchQ && matchF;
    card.style.display = show ? '' : 'none';
    if (show) {{
      visible++;
      if (charts[etf.code]) charts[etf.code].resize();
    }}
  }});
  document.getElementById('empty-msg').style.display = visible ? 'none' : '';
}}

function renderCards() {{
  const grid = document.getElementById('grid');
  DATA.forEach(etf => {{
    const latest = etf.points[etf.points.length - 1];
    const catColor = CAT_COLOR[latest.cat] || '#888';
    const card = document.createElement('div');
    card.className = 'card';
    card.id = 'card-' + etf.code;
    card.innerHTML = `
      <div class="card-head">
        <div><div class="card-title">${{etf.name}}</div><div class="card-code">${{etf.code}}</div></div>
        <span class="badge" style="background:${{catColor}}">${{latest.cat}}</span>
      </div>
      <div class="card-info">
        <span>现价 ${{latest.price}}</span>
        <span>打分 ${{latest.score}}</span>
        <span>位置 ${{latest.pos}}%</span>
        <span>EMA34 ${{latest.ema34 || '—'}}</span>
      </div>
      <div class="chart-box" id="chart-${{etf.code}}"></div>`;
    grid.appendChild(card);
  }});
}}

function initCharts() {{
  const observer = new IntersectionObserver((entries) => {{
    entries.forEach(entry => {{
      if (entry.isIntersecting) {{
        const code = entry.target.id.replace('chart-', '');
        if (!charts[code]) {{
          const etf = DATA.find(e => e.code === code);
          if (etf) initSingleChart(etf);
        }}
        observer.unobserve(entry.target);
      }}
    }});
  }}, {{ rootMargin: '200px' }});

  DATA.forEach(etf => {{
    const el = document.getElementById('chart-' + etf.code);
    if (el) observer.observe(el);
  }});
}}

function initSingleChart(etf) {{
  const container = document.getElementById('chart-' + etf.code);
  if (!container || container.offsetWidth === 0) return;
  const chart = echarts.init(container);
  charts[etf.code] = chart;

  const dates = etf.points.map(p => p.date);
  const levels = etf.points.map(p => CAT_LEVEL[p.cat] ?? 1);

  const pieces = etf.points.map((p, i) => ({{
    value: levels[i],
    itemStyle: {{ color: CAT_COLOR[p.cat] || '#888' }}
  }}));

  const markAreas = [
    [{{ yAxis: 4, itemStyle: {{ color: 'rgba(229,57,53,0.06)' }} }}, {{ yAxis: 6.5 }}],
    [{{ yAxis: 2.5, itemStyle: {{ color: 'rgba(255,152,0,0.04)' }} }}, {{ yAxis: 4 }}],
    [{{ yAxis: -0.5, itemStyle: {{ color: 'rgba(180,180,180,0.04)' }} }}, {{ yAxis: 2.5 }}],
  ];

  const option = {{
    grid: {{ top: 15, right: 16, bottom: 28, left: 52 }},
    xAxis: {{
      type: 'category',
      data: dates,
      axisLabel: {{ fontSize: 10, color: '#999', rotate: dates.length > 10 ? 30 : 0 }},
      axisLine: {{ lineStyle: {{ color: '#eee' }} }},
      axisTick: {{ show: false }}
    }},
    yAxis: {{
      type: 'value',
      min: -0.3, max: 6.5,
      interval: 1,
      axisLabel: {{
        fontSize: 9, color: '#aaa',
        formatter: v => LEVEL_LABEL[v] || ''
      }},
      splitLine: {{ lineStyle: {{ color: '#f5f5f5' }} }},
      axisLine: {{ show: false }},
      axisTick: {{ show: false }}
    }},
    tooltip: {{
      trigger: 'item',
      confine: true,
      backgroundColor: 'rgba(255,255,255,0.98)',
      borderColor: '#eee',
      borderWidth: 1,
      textStyle: {{ color: '#333', fontSize: 12 }},
      extraCssText: 'max-width:360px;white-space:normal;line-height:1.6;box-shadow:0 4px 16px rgba(0,0,0,.12)',
      formatter: function(params) {{
        const p = etf.points[params.dataIndex];
        if (!p) return '';
        const cc = CAT_COLOR[p.cat] || '#888';
        const reasons = (p.reasons || []).join(' · ') || '—';
        const verdict = p.verdict || '—';
        return '<div style="font-size:13px">'
          + '<b>' + etf.name + '</b> <span style="color:#aaa">' + etf.code + '</span><br>'
          + '<span style="color:#666">' + p.date + '</span>'
          + ' · 大盘 <b>' + (p.market||'—') + '</b><br>'
          + '<div style="border-top:1px solid #f0f0f0;margin:5px 0"></div>'
          + '价格 <b style="font-size:15px">' + p.price + '</b>'
          + ' · EMA34 ' + (p.ema34 || '—') + '<br>'
          + '分类 <b style="color:' + cc + '">' + p.cat + '</b>'
          + ' · 打分 <b>' + p.score + '</b><br>'
          + '月线 ' + (p.month_state||'—')
          + ' · 周线 ' + (p.week_state||'—') + '<br>'
          + '日线 ' + (p.day_state||'—') + ' (' + (p.day_cross||'—') + ')'
          + ' · 位置 ' + p.pos + '%<br>'
          + '<div style="border-top:1px solid #f0f0f0;margin:5px 0"></div>'
          + '<div style="font-size:12px;color:#333">' + verdict + '</div>'
          + '<div style="font-size:11px;color:#999;margin-top:4px">' + reasons + '</div>'
          + '</div>';
      }}
    }},
    series: [{{
      type: 'line',
      data: pieces,
      symbol: 'circle',
      symbolSize: etf.points.length === 1 ? 14 : 10,
      lineStyle: {{ width: 2, color: '#ccc' }},
      emphasis: {{
        itemStyle: {{ borderWidth: 3, borderColor: '#fff', shadowBlur: 8, shadowColor: 'rgba(0,0,0,.2)' }}
      }},
      markArea: {{ silent: true, data: markAreas }},
      animationDuration: 600
    }}]
  }};
  chart.setOption(option);
}}

window.addEventListener('resize', () => {{
  Object.values(charts).forEach(c => c.resize());
}});

document.getElementById('search').addEventListener('input', applyFilters);

initFilters();
initLegend();
renderCards();
initCharts();
</script></body></html>"""


if __name__ == "__main__":
    path = build_timeline()
    print(f"时间线页面已生成: {path}")
