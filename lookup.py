"""
个股/ETF 查询工具: 输入代码, 生成含K线+均线+策略分析的HTML页面
=================================================================
用法:
  python lookup.py 601919              # 个股
  python lookup.py 510210              # ETF
  python lookup.py 601919 中远海控      # 指定名称
"""
import sys
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from engine import (
    analyze_one, add_emas, resample_weekly, resample_monthly,
    EMA_FAST, EMA_MID, EMA_SLOW, STICK_THRESHOLD,
)
from tushare_client import TushareMcpClient, to_ts_code

ROOT = Path(__file__).resolve().parent
START_DATE = "20240101"

STOCK_PREFIXES = (
    "600", "601", "603", "605",
    "000", "001", "002", "003",
    "300", "301",
    "688", "689",
)


def is_stock(code: str) -> bool:
    return code.zfill(6).startswith(STOCK_PREFIXES)


def fetch_data(client: TushareMcpClient, code: str) -> pd.DataFrame:
    end = datetime.now().strftime("%Y%m%d")
    if is_stock(code):
        return client.fetch_stock_daily(code, START_DATE, end)
    return client.fetch_fund_daily(code, START_DATE, end)


def get_market_state(client: TushareMcpClient) -> str:
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


def build_chart_data(df: pd.DataFrame, n_days: int = 120) -> str:
    d = add_emas(df)
    tail = d.tail(n_days)
    rows = []
    for _, r in tail.iterrows():
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
    return json.dumps(rows, ensure_ascii=False)


def build_html(code, name, result, chart_json):
    cat = result.get("category", "—")
    score = result.get("score", 0)
    price = result.get("price", 0)
    verdict = result.get("verdict", "—")
    reasons = result.get("reasons", [])
    reasons_str = " · ".join(reasons) if reasons else "—"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    code_type = "个股" if is_stock(code) else "ETF"

    cat_color_map = {
        "可关注-回踩": "#e53935", "可关注-金叉": "#e53935",
        "可关注-向上变盘": "#e53935", "可关注-蚂蚁上树": "#ff5722",
        "持有/观察": "#fb8c00",
        "观望-变盘待确认": "#ff9800", "观望-变盘待定": "#888",
        "观望-待周线点头": "#888", "观望": "#888",
        "回避-向下变盘": "#bbb", "回避": "#bbb",
    }
    cat_color = cat_color_map.get(cat, "#888")

    ema_day = result.get("ema_day", {})
    detail_rows = [
        ("分类", f'<span style="background:{cat_color};color:#fff;padding:2px 10px;border-radius:5px;font-weight:600">{cat}</span>'),
        ("打分", f"<b>{score}</b> 分"),
        ("现价", f"<b style='font-size:18px'>{price}</b>"),
        ("EMA13 / EMA34 / EMA55", f"{ema_day.get('EMA13','—')} / {ema_day.get('EMA34','—')} / {ema_day.get('EMA55','—')}"),
        ("月线状态", result.get("month_state", "—")),
        ("周线状态", result.get("week_state", "—")),
        ("日线状态", f"{result.get('day_state','—')} ({result.get('day_cross','—')})"),
        ("周线方向闸", "✅ 通过" if result.get("week_gate_ok") else "❌ 未通过"),
        ("周线点头", "✅ 是" if result.get("week_nod") else "❌ 否"),
        ("月线位置", f"{result.get('pos_pct', '—')}%"),
        ("放量", "✅ 是" if result.get("is_volume_up") else "❌ 否"),
        ("金叉张口", f"{result.get('gap_pct', '—')}%"),
        ("回踩EMA34", "✅ 是" if result.get("is_pullback") else "—"),
        ("蚂蚁上树", "✅ 是" if result.get("ant_climb") else "—"),
        ("三线粘合", f"{'✅ 是' if result.get('is_stick') else '—'}"
                    + (f" · 方向: {result.get('stick_dir','—')} · 间距: {result.get('stick_spread','—')}%" if result.get("is_stick") else "")),
        ("结论", f"<div style='font-weight:500;line-height:1.6'>{verdict}</div>"),
        ("打分依据", f"<div style='color:#888;font-size:12px'>{reasons_str}</div>"),
    ]
    detail_html = "".join(
        f"<tr><td style='color:#888;white-space:nowrap;padding-right:16px;vertical-align:top'>{k}</td><td>{v}</td></tr>"
        for k, v in detail_rows
    )

    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} ({code}) 策略分析</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang SC","Helvetica Neue",sans-serif;background:#f5f5f7;color:#1d1d1f}}
.container{{max-width:1200px;margin:0 auto;padding:20px}}
.summary{{background:#fff;border-radius:14px;padding:20px 24px;margin-bottom:16px;
          box-shadow:0 1px 4px rgba(0,0,0,.06)}}
.summary h1{{font-size:22px;margin-bottom:4px}}
.summary .sub{{color:#888;font-size:13px;margin-bottom:14px}}
.badge{{display:inline-block;padding:4px 14px;border-radius:6px;font-size:14px;font-weight:600;color:#fff}}
.verdict-box{{background:#fafafa;border-radius:8px;padding:12px 16px;margin-top:14px;
              font-size:14px;line-height:1.7;border-left:4px solid {cat_color}}}
.chart-panel{{background:#fff;border-radius:14px;padding:16px;margin-bottom:16px;
              box-shadow:0 1px 4px rgba(0,0,0,.06)}}
.chart-panel h2{{font-size:15px;margin-bottom:10px;color:#333}}
#main-chart{{width:100%;height:480px}}
#vol-chart{{width:100%;height:120px}}
.detail{{background:#fff;border-radius:14px;padding:20px 24px;margin-bottom:16px;
         box-shadow:0 1px 4px rgba(0,0,0,.06)}}
.detail h2{{font-size:15px;margin-bottom:14px;color:#333}}
.detail table{{width:100%}}
.detail td{{padding:6px 0;font-size:13px;border-bottom:1px solid #f5f5f5;vertical-align:middle}}
.foot{{color:#bbb;font-size:11px;text-align:center;padding:20px;line-height:1.6}}
</style></head><body>
<div class="container">
  <div class="summary">
    <h1>{name} <span style="color:#aaa;font-size:16px">({code})</span></h1>
    <div class="sub">{code_type} · 生成于 {ts} · 大盘 {result.get('_market','—')}</div>
    <span class="badge" style="background:{cat_color}">{cat}</span>
    <span style="margin-left:12px;font-size:15px">打分 <b>{score}</b></span>
    <span style="margin-left:12px;font-size:15px">现价 <b>{price}</b></span>
    <span style="margin-left:12px;font-size:13px;color:#888">位置 {result.get('pos_pct','—')}%</span>
    <div class="verdict-box">{verdict}</div>
  </div>

  <div class="chart-panel">
    <h2>K线 + EMA 13/34/55 (近120个交易日)</h2>
    <div id="main-chart"></div>
    <div id="vol-chart"></div>
  </div>

  <div class="detail">
    <h2>详细分析</h2>
    <table>{detail_html}</table>
  </div>

  <div class="foot">
    策略框架: 月线基调 / 周线方向闸 / 日线买卖点 + EMA 13/34/55 打分制(≥4关注)<br>
    本页面为机械规则扫描结果, 不构成投资建议。
  </div>
</div>

<script>
const RAW = {chart_json};
const dates = RAW.map(r => r.date);
const ohlc = RAW.map(r => [r.open, r.close, r.low, r.high]);
const volumes = RAW.map(r => r.volume);
const ema13 = RAW.map(r => r.ema13);
const ema34 = RAW.map(r => r.ema34);
const ema55 = RAW.map(r => r.ema55);

const upColor = '#e53935', dnColor = '#2196f3';

const mainChart = echarts.init(document.getElementById('main-chart'));
mainChart.setOption({{
  animation: false,
  grid: {{ left: 60, right: 20, top: 20, bottom: 30 }},
  xAxis: {{
    type: 'category', data: dates, boundaryGap: true,
    axisLine: {{ lineStyle: {{ color: '#ddd' }} }},
    axisLabel: {{ fontSize: 11, color: '#888' }}
  }},
  yAxis: {{
    scale: true,
    splitLine: {{ lineStyle: {{ color: '#f0f0f0' }} }},
    axisLabel: {{ fontSize: 11, color: '#888' }}
  }},
  tooltip: {{
    trigger: 'axis',
    axisPointer: {{ type: 'cross' }},
    backgroundColor: 'rgba(255,255,255,.96)',
    borderColor: '#eee', borderWidth: 1,
    textStyle: {{ color: '#333', fontSize: 12 }},
    extraCssText: 'box-shadow:0 2px 12px rgba(0,0,0,.1)',
    formatter: function(params) {{
      const d = params[0].axisValue;
      let s = '<b>' + d + '</b><br>';
      params.forEach(p => {{
        if (p.seriesType === 'candlestick') {{
          const v = p.data;
          s += '开 ' + v[1] + '  收 ' + v[2] + '<br>低 ' + v[3] + '  高 ' + v[4] + '<br>';
        }} else {{
          s += '<span style="color:' + p.color + '">●</span> ' + p.seriesName + ': <b>' + p.data + '</b><br>';
        }}
      }});
      const idx = dates.indexOf(d);
      if (idx >= 0) s += '成交量: ' + (volumes[idx]/10000).toFixed(0) + '万';
      return s;
    }}
  }},
  dataZoom: [
    {{ type: 'inside', xAxisIndex: [0], start: 0, end: 100 }},
  ],
  series: [
    {{
      type: 'candlestick', data: ohlc,
      itemStyle: {{
        color: upColor, color0: dnColor,
        borderColor: upColor, borderColor0: dnColor
      }}
    }},
    {{
      name: 'EMA13', type: 'line', data: ema13,
      symbol: 'none', lineStyle: {{ width: 1.5, color: '#1e88e5' }}, z: 5
    }},
    {{
      name: 'EMA34', type: 'line', data: ema34,
      symbol: 'none', lineStyle: {{ width: 2, color: '#ff9800' }}, z: 5
    }},
    {{
      name: 'EMA55', type: 'line', data: ema55,
      symbol: 'none', lineStyle: {{ width: 1.5, color: '#66bb6a' }}, z: 5
    }}
  ]
}});

const volChart = echarts.init(document.getElementById('vol-chart'));
volChart.setOption({{
  animation: false,
  grid: {{ left: 60, right: 20, top: 5, bottom: 24 }},
  xAxis: {{
    type: 'category', data: dates, boundaryGap: true,
    axisLabel: {{ show: false }}, axisTick: {{ show: false }}, axisLine: {{ lineStyle: {{ color: '#eee' }} }}
  }},
  yAxis: {{
    scale: true, show: false
  }},
  series: [{{
    type: 'bar',
    data: RAW.map((r, i) => ({{
      value: r.volume,
      itemStyle: {{ color: r.close >= r.open ? upColor : dnColor, opacity: 0.5 }}
    }})),
    barWidth: '60%'
  }}]
}});

echarts.connect([mainChart, volChart]);

window.addEventListener('resize', () => {{ mainChart.resize(); volChart.resize(); }});
</script></body></html>"""


def main():
    if len(sys.argv) < 2:
        print("用法: python lookup.py <代码> [名称]")
        print("示例: python lookup.py 601919 中远海控")
        print("      python lookup.py 510210")
        sys.exit(1)

    code = sys.argv[1].strip().zfill(6)
    name = sys.argv[2] if len(sys.argv) > 2 else code

    print(f"查询 {code} ({name}) ({'个股' if is_stock(code) else 'ETF'})...")
    client = TushareMcpClient()

    print("获取大盘环境...")
    market = get_market_state(client)
    print(f"大盘: {market}")

    print("获取行情数据...")
    df = fetch_data(client, code)
    if df is None or df.empty:
        print("数据为空, 请检查代码是否正确")
        sys.exit(1)
    print(f"获取到 {len(df)} 条日线数据")

    result = analyze_one(code, name, df, market)
    if result.get("error"):
        print(f"分析失败: {result['error']}")
        sys.exit(1)
    result["_market"] = market

    chart_json = build_chart_data(df)

    out = ROOT / f"lookup_{code}.html"
    html = build_html(code, name, result, chart_json)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n{'='*50}")
    print(f"  {name} ({code})")
    print(f"  分类: {result['category']}  打分: {result['score']}")
    print(f"  现价: {result['price']}  位置: {result['pos_pct']}%")
    print(f"  {result['verdict']}")
    print(f"{'='*50}")
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
