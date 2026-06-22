"""
ETF 信号时间线: 一张大折线图, 所有ETF作为不同折线
================================================================
X轴=扫描日期, Y轴=信号等级(0-6), 每只ETF一条线,
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
      background:#f5f5f7;color:#1d1d1f;display:flex;flex-direction:column;height:100vh;overflow:hidden}}
.header{{background:#fff;padding:14px 24px;box-shadow:0 1px 4px rgba(0,0,0,.08);flex-shrink:0;z-index:10}}
.header h1{{font-size:20px;margin-bottom:4px}}
.meta{{color:#888;font-size:13px;margin-bottom:10px}}
.toolbar{{display:flex;flex-wrap:wrap;gap:10px;align-items:center}}
#search{{width:280px;padding:7px 14px;border:1px solid #ddd;border-radius:8px;font-size:13px;outline:none}}
#search:focus{{border-color:#1890ff}}
.filters{{display:flex;flex-wrap:wrap;gap:6px}}
.fbtn{{padding:4px 12px;border-radius:16px;font-size:12px;border:1px solid #ddd;
       background:#fff;cursor:pointer;transition:all .15s;white-space:nowrap;user-select:none}}
.fbtn:hover{{border-color:#1890ff;color:#1890ff}}
.fbtn.active{{background:#1890ff;color:#fff;border-color:#1890ff}}
.hint{{font-size:11px;color:#bbb;margin-left:auto}}
#chart-container{{flex:1;min-height:0;padding:8px}}
</style></head><body>
<div class="header">
  <h1>ETF 信号时间线</h1>
  <div class="meta">更新于 {ts} · 共 {n_etfs} 只ETF · {n_days} 个时间点 · 每日去重取最后一次</div>
  <div class="toolbar">
    <input type="text" id="search" placeholder="搜索ETF, 回车高亮, 清空恢复全部...">
    <div class="filters" id="filters"></div>
    <span class="hint">点击图例切换显示 | 搜索高亮特定ETF</span>
  </div>
</div>
<div id="chart-container"></div>

<script>
const DATA = {data_json};
const CAT_COLOR = {cat_color_json};
const CAT_LEVEL = {cat_level_json};
const LEVEL_LABEL = {{0:'回避',1:'观望',2:'待确认',3:'持有/观察',4:'蚂蚁上树',5:'可关注',6:'回踩买'}};

const LINE_COLORS = [
  '#e53935','#1e88e5','#43a047','#fb8c00','#8e24aa','#00acc1','#d81b60',
  '#3949ab','#7cb342','#f4511e','#6d4c41','#546e7a','#c0ca33','#00897b',
  '#5e35b1','#039be5','#c62828','#2e7d32','#ef6c00','#4527a0',
  '#00838f','#ad1457','#283593','#558b2f','#bf360c','#4e342e',
  '#37474f','#827717','#004d40','#311b92','#006064','#880e4f',
  '#1a237e','#33691e','#e65100','#3e2723','#263238','#9e9d24',
  '#00695c','#4a148c','#01579b','#b71c1c','#1b5e20','#e64a19','#455a64'
];

let chart = null;
let activeFilter = 'all';

const allDates = [...new Set(DATA.flatMap(e => e.points.map(p => p.date)))].sort();

function buildSeries() {{
  return DATA.map((etf, i) => {{
    const dataMap = {{}};
    etf.points.forEach(p => {{ dataMap[p.date] = p; }});
    const seriesData = allDates.map(d => {{
      const p = dataMap[d];
      if (!p) return null;
      return {{
        value: CAT_LEVEL[p.cat] ?? 1,
        _detail: p
      }};
    }});
    return {{
      name: etf.name + ' ' + etf.code,
      type: 'line',
      data: seriesData,
      symbol: 'circle',
      symbolSize: 8,
      connectNulls: true,
      lineStyle: {{ width: 2 }},
      itemStyle: {{ color: LINE_COLORS[i % LINE_COLORS.length] }},
      emphasis: {{
        focus: 'series',
        lineStyle: {{ width: 4 }},
        itemStyle: {{ borderWidth: 3, borderColor: '#fff', shadowBlur: 6, shadowColor: 'rgba(0,0,0,.3)' }}
      }},
      blur: {{ lineStyle: {{ opacity: 0.1 }}, itemStyle: {{ opacity: 0.1 }} }},
    }};
  }});
}}

function initChart() {{
  const container = document.getElementById('chart-container');
  chart = echarts.init(container);

  const option = {{
    grid: {{ top: 80, right: 30, bottom: 50, left: 70 }},
    legend: {{
      type: 'scroll',
      top: 5,
      left: 10, right: 10,
      textStyle: {{ fontSize: 11 }},
      pageIconSize: 12,
      pageTextStyle: {{ fontSize: 11 }},
      selector: [
        {{ type: 'all', title: '全选' }},
        {{ type: 'inverse', title: '反选' }}
      ]
    }},
    tooltip: {{
      trigger: 'item',
      confine: true,
      backgroundColor: 'rgba(255,255,255,0.98)',
      borderColor: '#eee',
      borderWidth: 1,
      textStyle: {{ color: '#333', fontSize: 12 }},
      extraCssText: 'max-width:380px;white-space:normal;line-height:1.7;box-shadow:0 4px 16px rgba(0,0,0,.15);border-radius:8px;padding:12px 14px',
      formatter: function(params) {{
        const p = params.data && params.data._detail;
        if (!p) return params.seriesName;
        const cc = CAT_COLOR[p.cat] || '#888';
        const reasons = (p.reasons || []).join(' · ') || '—';
        const verdict = p.verdict || '—';
        return '<div>'
          + '<div style="font-size:14px;font-weight:600;margin-bottom:6px">' + params.seriesName + '</div>'
          + '<span style="color:#666">' + p.date + '</span>'
          + ' · 大盘 <b>' + (p.market||'—') + '</b><br>'
          + '<div style="border-top:1px solid #f0f0f0;margin:6px 0"></div>'
          + '价格 <b style="font-size:16px">' + p.price + '</b>'
          + '&nbsp;&nbsp;EMA34 ' + (p.ema34 || '—') + '<br>'
          + '分类 <span style="display:inline-block;background:' + cc + ';color:#fff;padding:1px 8px;border-radius:4px;font-size:11px;font-weight:600">' + p.cat + '</span>'
          + '&nbsp;&nbsp;打分 <b>' + p.score + '</b>&nbsp;&nbsp;位置 ' + p.pos + '%<br>'
          + '月线 ' + (p.month_state||'—')
          + ' · 周线 ' + (p.week_state||'—') + '<br>'
          + '日线 ' + (p.day_state||'—') + ' (' + (p.day_cross||'—') + ')<br>'
          + '<div style="border-top:1px solid #f0f0f0;margin:6px 0"></div>'
          + '<div style="font-size:12px;color:#222;font-weight:500">' + verdict + '</div>'
          + '<div style="font-size:11px;color:#999;margin-top:4px">' + reasons + '</div>'
          + '</div>';
      }}
    }},
    xAxis: {{
      type: 'category',
      data: allDates,
      axisLabel: {{ fontSize: 11, color: '#888' }},
      axisLine: {{ lineStyle: {{ color: '#ddd' }} }},
      axisTick: {{ show: false }}
    }},
    yAxis: {{
      type: 'value',
      min: -0.3, max: 6.5,
      interval: 1,
      axisLabel: {{
        fontSize: 11, color: '#888',
        formatter: v => LEVEL_LABEL[v] || ''
      }},
      splitLine: {{ lineStyle: {{ color: '#f0f0f0' }} }},
      axisLine: {{ show: false }},
      axisTick: {{ show: false }}
    }},
    dataZoom: [
      {{ type: 'inside', xAxisIndex: 0, filterMode: 'none' }},
      {{ type: 'slider', xAxisIndex: 0, bottom: 8, height: 20, filterMode: 'none',
         borderColor: '#ddd', fillerColor: 'rgba(24,144,255,0.15)' }}
    ],
    series: buildSeries()
  }};
  chart.setOption(option);

  new ResizeObserver(() => chart.resize()).observe(container);
}}

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
      applyFilter();
    }};
    fBox.appendChild(btn);
  }});
}}

function applyFilter() {{
  if (!chart) return;
  const q = document.getElementById('search').value.trim().toLowerCase();
  const legend = {{}};
  DATA.forEach(etf => {{
    const key = etf.name + ' ' + etf.code;
    const latest = etf.points[etf.points.length - 1];
    const matchQ = !q || etf.name.toLowerCase().includes(q) || etf.code.includes(q);
    const matchF = activeFilter === 'all' || latest.cat.startsWith(activeFilter);
    legend[key] = matchQ && matchF;
  }});
  chart.setOption({{ legend: {{ selected: legend }} }});
}}

document.getElementById('search').addEventListener('input', applyFilter);

initFilters();
initChart();
</script></body></html>"""


if __name__ == "__main__":
    path = build_timeline()
    print(f"时间线页面已生成: {path}")
