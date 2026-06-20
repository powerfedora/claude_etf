"""
报告生成: 把所有ETF分析结果汇总成 HTML
"""
import json
from datetime import datetime
from pathlib import Path

PORTFOLIO_FILE = Path(__file__).resolve().parent / "portfolio.json"

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


def _wan(x):
    """元 -> 万 显示, 整数省略小数。"""
    try:
        return f"{x/10000:g}万"
    except Exception:
        return "—"


def build_portfolio_panel(rows):
    """读取 portfolio.json, 渲染"我的组合"面板; 止损位取当日 EMA34, 自动刷新并提醒破位。"""
    if not PORTFOLIO_FILE.exists():
        return ""
    try:
        pf = json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return f'<div class="focusbox"><h2>💼 我的组合</h2><span style="color:#e53935">portfolio.json 解析失败: {e}</span></div>'

    by_code = {r["code"]: r for r in rows}
    cap = pf.get("capital", 0)
    trs = ""
    filled_sum = 0
    for p in pf.get("positions", []):
        r = by_code.get(p["code"], {})
        price = r.get("price")
        cat = r.get("category", "—")
        pos = r.get("pos_pct")
        ema34 = (r.get("ema_day") or {}).get("EMA34")
        filled = p.get("filled", 0)
        filled_sum += filled

        # 止损提醒: 现价 vs 当日 EMA34
        if price is not None and ema34:
            stop_txt = f"{ema34}"
            if price < ema34:
                stop_alert = '<span style="color:#e53935;font-weight:700">⚠跌破EMA34·考虑止损</span>'
            else:
                d = (price - ema34) / ema34 * 100
                stop_alert = f'<span style="color:#43a047">距止损 +{d:.1f}%</span>'
        else:
            stop_txt = "—"; stop_alert = "—"

        # 浮盈(若已填成交均价 cost)
        pl = ""
        cost = p.get("cost") or 0
        if cost and price is not None:
            plpct = (price - cost) / cost * 100
            color = "#43a047" if plpct >= 0 else "#e53935"
            tag = " 🎯达止盈线" if plpct >= 12 else ""
            pl = f'<span style="color:{color}">{plpct:+.1f}%{tag}</span>'

        trs += f"""<tr>
          <td><b>{p['name']}</b><br><span class="code">{p['code']}</span></td>
          <td class="px">{price if price is not None else '—'}</td>
          <td style="font-size:12px">{cat}</td>
          <td class="pos">{pos if pos is not None else '—'}%</td>
          <td><b>{p.get('target_pct',0)}%</b></td>
          <td>{_wan(p.get('target_amt',0))}</td>
          <td>{_wan(p.get('first_buy',0))}</td>
          <td><b>{_wan(filled)}</b>{('<br>'+pl) if pl else ''}</td>
          <td>{stop_txt}</td>
          <td>{stop_alert}</td>
          <td style="font-size:12px;line-height:1.5">{p.get('status','')}</td>
        </tr>"""

    cash = cap - filled_sum
    invested_pct = filled_sum / cap * 100 if cap else 0
    trs += f"""<tr style="background:#fafafa">
          <td><b>现金</b></td><td>—</td><td>—</td><td>—</td>
          <td><b>{pf.get('cash_pct','')}%</b></td><td>—</td><td>—</td>
          <td><b>{_wan(cash)}</b></td><td>—</td><td>—</td>
          <td style="font-size:12px">子弹: 回踩加仓/防守</td></tr>"""

    return f"""<div class="focusbox">
      <h2>💼 我的组合 &nbsp;总{_wan(cap)} ｜ 已投{_wan(filled_sum)}({invested_pct:.0f}%) ｜ 现金{_wan(cash)}</h2>
      <table>
       <tr><th>标的</th><th>现价</th><th>当前信号</th><th>月线位置</th><th>目标占比</th><th>目标金额</th><th>今日首笔</th><th>已建仓/浮盈</th><th>止损(EMA34)</th><th>止损提醒</th><th>状态/动作</th></tr>
       {trs}
      </table>
      <div style="font-size:11px;color:#aaa;margin-top:8px">止损位取当日日线 EMA34, 每次重跑自动刷新; 跌破并转死叉再离场。"已建仓/成交均价(cost)/状态" 在 portfolio.json 中维护。</div>
    </div>"""


def build_history_panel():
    """渲染"操作记录"(trades) 与 "近期运行"(snapshots 时间线) 两块面板。"""
    try:
        from history import load_trades, load_snapshots
    except Exception:
        return ""

    trades = load_trades()
    snaps = load_snapshots()
    if not trades and not snaps:
        return ""

    out = ""
    # 操作记录
    if trades:
        rows = ""
        for t in reversed(trades[-20:]):
            act = t.get("action")
            color = "#e53935" if act == "sell" else "#43a047"
            label = "卖出" if act == "sell" else "买入"
            rows += (f'<tr><td style="font-size:12px">{t["ts"]}</td>'
                     f'<td><b>{t.get("name","")}</b> <span class="code">{t["code"]}</span></td>'
                     f'<td style="color:{color};font-weight:600">{label}</td>'
                     f'<td>{_wan(t["amount"])}</td><td>{t["price"]}</td>'
                     f'<td>{_wan(t.get("after_filled",0))}/{t.get("after_cost",0)}</td>'
                     f'<td style="font-size:12px;color:#888">{t.get("note","")}</td></tr>')
        out += f"""<div class="focusbox">
          <h2>📜 操作记录 ({len(trades)} 笔)</h2>
          <table><tr><th>时间</th><th>标的</th><th>方向</th><th>金额</th><th>成交价</th><th>后:已建仓/均价</th><th>备注</th></tr>
          {rows}</table></div>"""

    # 近期运行时间线
    if snaps:
        rows = ""
        for s in reversed(snaps[-12:]):
            pf = s.get("portfolio") or {}
            focus = sum(1 for e in s["etfs"] if e["cat"].startswith("可关注"))
            rows += (f'<tr><td style="font-size:12px">{s["ts"]}</td>'
                     f'<td>{s["market"]}</td><td>{focus} 只</td>'
                     f'<td>{_wan(pf.get("filled",0))}</td><td>{_wan(pf.get("cash",0))}</td></tr>')
        out += f"""<div class="focusbox">
          <h2>🕒 近期运行 ({len(snaps)} 次快照)</h2>
          <table><tr><th>时间</th><th>大盘</th><th>可关注</th><th>已投</th><th>现金</th></tr>
          {rows}</table>
          <div style="font-size:11px;color:#aaa;margin-top:8px">完整历史在 history/ 目录; 查询某只变化: <code>python history.py code 代码</code></div></div>"""
    return out


def build_reports(results, market_state, out_html):
    rows = [r for r in results if not r.get("error")]
    errs = [r for r in results if r.get("error")]
    rows.sort(key=lambda r: (CAT_ORDER.get(r["category"], 9), -r["score"]))

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

    portfolio_panel = build_portfolio_panel(rows)
    history_panel = build_history_panel()

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
{portfolio_panel}
{history_panel}
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
