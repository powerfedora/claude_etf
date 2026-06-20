"""
报告生成: 把所有ETF分析结果汇总成 HTML + Excel
"""
import pandas as pd
from datetime import datetime

# 分类排序优先级 (越上越值得看)
CAT_ORDER = {
    "可关注-回踩": 0, "可关注-金叉": 1,
    "可关注-向上变盘": 2, "可关注-蚂蚁上树": 3,
    "持有/观察": 4,
    "观望-变盘待定": 5, "观望-待周线点头": 6, "观望": 7,
    "回避-向下变盘": 8, "回避": 9,
}
CAT_COLOR = {
    "可关注-回踩": "#e53935", "可关注-金叉": "#e53935",
    "可关注-向上变盘": "#e53935", "可关注-蚂蚁上树": "#e53935",
    "持有/观察": "#fb8c00",
    "观望-变盘待定": "#888", "观望-待周线点头": "#888", "观望": "#888",
    "回避-向下变盘": "#bbb", "回避": "#bbb",
}


def build_reports(results, market_state, out_html, out_xlsx):
    rows = [r for r in results if not r.get("error")]
    errs = [r for r in results if r.get("error")]
    rows.sort(key=lambda r: (CAT_ORDER.get(r["category"], 9), -r["score"]))

    # ---------- Excel ----------
    df = pd.DataFrame([{
        "代码": r["code"], "名称": r["name"], "现价": r["price"],
        "分类": r["category"], "打分": r["score"],
        "月线": r["month_state"], "周线": r["week_state"],
        "日线": r["day_state"], "金叉死叉": r["day_cross"],
        "周线闸": "过" if r["week_gate_ok"] else "未过",
        "回踩": "是" if r["is_pullback"] else "",
        "蚂蚁上树": ("转强" if r.get("ant_climb") else "")
                   + ("+周线点头" if r.get("ant_climb") and r.get("week_nod") else ""),
        "粘合共振": (f"粘合·{r.get('stick_dir')}" if r.get("is_stick") else ""),
        "放量": "是" if r["is_volume_up"] else "",
        "张口%": r["gap_pct"], "月线位置%": r["pos_pct"],
        "结论": r["verdict"],
    } for r in rows])
    df.to_excel(out_xlsx, index=False)

    # ---------- HTML ----------
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    focus = [r for r in rows if r["category"].startswith("可关注")]

    cards = ""
    for r in rows:
        c = CAT_COLOR.get(r["category"], "#888")
        reasons = " · ".join(r["reasons"]) if r["reasons"] else "—"
        cards += f"""
        <tr style="border-left:4px solid {c}">
          <td><b>{r['name']}</b><br><span class="code">{r['code']}</span></td>
          <td class="px">{r['price']}</td>
          <td><span class="cat" style="background:{c}">{r['category']}</span><br>
              <span class="sc">打分 {r['score']}</span></td>
          <td class="states">月:{r['month_state']}<br>周:{r['week_state']}<br>日:{r['day_state']} ({r['day_cross']})</td>
          <td class="pos">{r['pos_pct']}%</td>
          <td class="verdict">{r['verdict']}<br><span class="rs">{reasons}</span></td>
        </tr>"""

    focus_html = "".join(
        f'<span class="chip">{r["name"]} <b>{r["score"]}分</b></span>' for r in focus
    ) or '<span style="color:#999">本次无达标(≥4分且有信号)标的</span>'

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ETF 扫描报告 {ts}</title>
<style>
  body{{font-family:-apple-system,"PingFang SC",sans-serif;background:#f5f5f7;margin:0;padding:16px;color:#1d1d1f}}
  h1{{font-size:20px;margin:0 0 4px}}
  .meta{{color:#888;font-size:13px;margin-bottom:16px}}
  .market{{display:inline-block;padding:4px 12px;border-radius:6px;font-weight:600;
           background:{'#e53935' if market_state=='进攻档' else '#fb8c00' if market_state=='谨慎档' else '#888'};color:#fff}}
  .focusbox{{background:#fff;border-radius:12px;padding:14px 16px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
  .focusbox h2{{font-size:15px;margin:0 0 10px}}
  .chip{{display:inline-block;background:#fff0f0;color:#e53935;border:1px solid #ffd0d0;
         border-radius:20px;padding:5px 12px;margin:3px;font-size:13px}}
  table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;
         box-shadow:0 1px 3px rgba(0,0,0,.06)}}
  th{{background:#fafafa;text-align:left;padding:10px;font-size:12px;color:#888;font-weight:600}}
  td{{padding:10px;border-top:1px solid #f0f0f0;font-size:13px;vertical-align:top}}
  .code{{color:#aaa;font-size:11px}}
  .px{{font-weight:600;font-size:15px}}
  .cat{{color:#fff;padding:2px 8px;border-radius:5px;font-size:12px;font-weight:600;white-space:nowrap}}
  .sc{{font-size:11px;color:#888}}
  .states{{font-size:11px;color:#666;line-height:1.5}}
  .pos{{color:#999}}
  .verdict{{font-size:12px;line-height:1.5}}
  .rs{{color:#aaa;font-size:11px}}
  .foot{{color:#bbb;font-size:11px;margin-top:16px;line-height:1.6}}
</style></head><body>
<h1>ETF 多周期均线扫描报告</h1>
<div class="meta">生成时间 {ts} ｜ 共 {len(rows)} 只有效 ｜ 大盘环境 <span class="market">{market_state}</span></div>
<div class="focusbox">
  <h2>🎯 本次可关注 ({len(focus)} 只)</h2>
  {focus_html}
</div>
<table>
  <tr><th>标的</th><th>现价</th><th>分类/打分</th><th>月/周/日</th><th>月线位置</th><th>结论 / 触发信号</th></tr>
  {cards}
</table>
<div class="foot">
  说明: 主判据 EMA13/34/55。三层框架 = 月线基调 / 周线方向闸 / 日线买卖点 + 打分制(≥4关注)。<br>
  135战法: 蚂蚁上树(转强)→需周线点头才升级买入,否则观望; 粘合共振(变盘)→方向未明先观望,向上才买、向下就避。<br>
  分类优先级: 可关注-回踩 &gt; 可关注-金叉 &gt; 可关注-向上变盘 &gt; 可关注-蚂蚁上树 &gt; 持有/观察 &gt; 观望(变盘待定/待周线点头) &gt; 回避(向下变盘) &gt; 回避。<br>
  本报告为机械规则扫描结果, 不构成投资建议; 大盘环境弱或节前请按自身纪律降档。
  {"<br>⚠️ 数据不足/抓取失败: " + ", ".join(e["code"] for e in errs) if errs else ""}
</div>
</body></html>"""
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    return len(rows), len(focus), len(errs)
