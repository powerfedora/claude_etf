"""
510210 半年回测
===============
用 engine.analyze_one 逐日扫描，模拟 100 万单票操作。
买入: category 以 "可关注" 开头时，次日开盘价全仓买入
卖出: ① 收盘跌破日线 EMA34 (止损)  ② 浮盈 ≥12% (止盈)
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from engine import (
    analyze_one, add_emas, EMA_FAST, EMA_MID, EMA_SLOW, PROFIT_TRAIL
)
from tushare_client import TushareMcpClient

CAPITAL = 1_000_000
CODE = "510210"
NAME = "上证综指ETF"
BACKTEST_MONTHS = 6
WARMUP_DAYS = 200  # EMA 需要足够的预热数据


def get_market_state_at(idx_df, i):
    """用沪深300截至第i行的数据判断大盘档位"""
    if i < 60:
        return "谨慎档"
    sub = idx_df.iloc[:i+1].copy()
    sub = add_emas(sub)
    last = sub.iloc[-1]
    p = last["close"]
    ef = last[f"ema{EMA_FAST}"]
    em_ = last[f"ema{EMA_MID}"]
    es = last[f"ema{EMA_SLOW}"]
    es_prev = sub.iloc[-5][f"ema{EMA_SLOW}"] if len(sub) >= 5 else es
    if p > ef > em_ > es:
        return "进攻档"
    if p > es and es > es_prev:
        return "谨慎档"
    if p < es:
        return "防守档"
    return "谨慎档"


def tushare_api(token, api_name, fields, **params):
    """直接调用 Tushare Pro 标准 HTTP API"""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    import requests
    resp = requests.post(
        "https://api.tushare.pro",
        json={"api_name": api_name, "token": token, "params": params, "fields": ",".join(fields)},
        verify=False, timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Tushare 错误: {data.get('msg')}")
    cols = data["data"]["fields"]
    rows = data["data"]["items"]
    return pd.DataFrame(rows, columns=cols)


def fetch_fund_daily_direct(token, code, start_date, end_date):
    ts_code = f"{code}.SH" if code.startswith(("5","6")) else f"{code}.SZ"
    df = tushare_api(token, "fund_daily", ["trade_date","open","high","low","close","vol"],
                     ts_code=ts_code, start_date=start_date, end_date=end_date)
    if df.empty:
        return None
    df = df.rename(columns={"trade_date": "date", "vol": "volume"})
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("date").dropna().reset_index(drop=True)
    # 前复权
    adj = tushare_api(token, "fund_adj", ["trade_date","adj_factor"],
                      ts_code=ts_code, start_date=start_date, end_date=end_date)
    if not adj.empty:
        adj["date"] = pd.to_datetime(adj["trade_date"], format="%Y%m%d")
        adj["adj_factor"] = pd.to_numeric(adj["adj_factor"], errors="coerce")
        adj = adj[["date","adj_factor"]].sort_values("date").dropna().reset_index(drop=True)
        merged = df.merge(adj, on="date", how="left")
        merged["adj_factor"] = merged["adj_factor"].ffill().bfill()
        factor = merged["adj_factor"] / merged["adj_factor"].iloc[-1]
        for c in ("open","high","low","close"):
            df[c] = merged[c] * factor
    return df


def fetch_index_daily_direct(token, ts_code, start_date, end_date):
    df = tushare_api(token, "index_daily", ["trade_date","open","high","low","close","vol"],
                     ts_code=ts_code, start_date=start_date, end_date=end_date)
    if df.empty:
        return None
    df = df.rename(columns={"trade_date": "date", "vol": "volume"})
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").dropna().reset_index(drop=True)


def load_token():
    import json, os
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    cfg_path = ROOT / "tushare_mcp.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        url = cfg.get("mcp_url", "")
        if "token=" in url:
            return url.split("token=")[-1]
    raise RuntimeError("未找到 Tushare token，请配置 tushare_mcp.json 或设置环境变量 TUSHARE_TOKEN")


def run_backtest():
    token = load_token()
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=BACKTEST_MONTHS * 30 + WARMUP_DAYS + 60)).strftime("%Y%m%d")

    print(f"拉取 {CODE} 日线数据 ({start_date} ~ {end_date}) ...")
    df = fetch_fund_daily_direct(token, CODE, start_date, end_date)
    if df is None or df.empty:
        print("数据获取失败"); return
    df = df.sort_values("date").reset_index(drop=True)
    print(f"  共 {len(df)} 条日线")

    print(f"拉取沪深300指数数据 ...")
    idx = fetch_index_daily_direct(token, "000300.SH", start_date, end_date)
    if idx is None or idx.empty:
        print("沪深300数据获取失败"); return
    idx = idx.sort_values("date").reset_index(drop=True)
    print(f"  共 {len(idx)} 条日线\n")

    # 确定回测起止日
    bt_start = datetime.now() - timedelta(days=BACKTEST_MONTHS * 30)
    bt_mask = df["date"] >= pd.Timestamp(bt_start)
    if bt_mask.sum() < 30:
        print("回测区间数据不足"); return

    bt_start_idx = bt_mask.idxmax()
    # 确保有足够预热
    if bt_start_idx < 130:
        bt_start_idx = 130

    print(f"回测区间: {df.loc[bt_start_idx, 'date'].strftime('%Y-%m-%d')} ~ "
          f"{df.iloc[-1]['date'].strftime('%Y-%m-%d')}")
    print(f"初始资金: {CAPITAL:,.0f} 元")
    print(f"止盈线: +{PROFIT_TRAIL*100:.0f}%  |  止损线: 跌破日线EMA34")
    print("=" * 70)

    # --- 逐日模拟 ---
    trades = []         # 已完成的交易
    position = None     # None = 空仓; dict = {buy_date, buy_price, shares, cost}
    cash = CAPITAL
    signals_log = []    # 每日信号记录

    for i in range(bt_start_idx, len(df)):
        today = df.iloc[i]
        date_str = today["date"].strftime("%Y-%m-%d")

        # 用截至今天的数据跑 analyze_one
        history_slice = df.iloc[:i+1].copy()

        # 找对应的沪深300索引
        idx_mask = idx["date"] <= today["date"]
        idx_i = idx_mask.sum() - 1
        market_state = get_market_state_at(idx, idx_i) if idx_i >= 0 else "谨慎档"

        result = analyze_one(CODE, NAME, history_slice, market_state)
        if result.get("error"):
            continue

        category = result["category"]
        price = today["close"]
        ema34 = result["ema_day"][f"EMA{EMA_MID}"]

        # --- 持仓检查: 止盈/止损 ---
        if position is not None:
            pnl_pct = (price - position["buy_price"]) / position["buy_price"]

            # 止盈
            if pnl_pct >= PROFIT_TRAIL:
                profit = position["shares"] * (price - position["buy_price"])
                cash += position["shares"] * price
                trades.append({
                    "buy_date": position["buy_date"],
                    "sell_date": date_str,
                    "buy_price": position["buy_price"],
                    "sell_price": price,
                    "shares": position["shares"],
                    "pnl": profit,
                    "pnl_pct": pnl_pct * 100,
                    "exit_reason": "止盈",
                    "hold_days": (today["date"] - pd.Timestamp(position["buy_date"])).days,
                })
                signals_log.append(f"  {date_str}  🎯止盈  卖出价={price:.3f}  盈亏={profit:+,.0f} ({pnl_pct:+.1%})")
                position = None
                continue

            # 止损: 收盘跌破 EMA34
            if price < ema34:
                profit = position["shares"] * (price - position["buy_price"])
                cash += position["shares"] * price
                trades.append({
                    "buy_date": position["buy_date"],
                    "sell_date": date_str,
                    "buy_price": position["buy_price"],
                    "sell_price": price,
                    "shares": position["shares"],
                    "pnl": profit,
                    "pnl_pct": pnl_pct * 100,
                    "exit_reason": "止损(破EMA34)",
                    "hold_days": (today["date"] - pd.Timestamp(position["buy_date"])).days,
                })
                signals_log.append(f"  {date_str}  🛑止损  卖出价={price:.3f}  盈亏={profit:+,.0f} ({pnl_pct:+.1%})")
                position = None
                continue

        # --- 空仓时检查买入信号 ---
        if position is None and category.startswith("可关注"):
            # 次日开盘买入 (用下一根K线的开盘价)
            if i + 1 < len(df):
                next_day = df.iloc[i + 1]
                buy_price = next_day["open"]
                shares = int(cash / buy_price / 100) * 100  # 整百股
                if shares > 0:
                    cost = shares * buy_price
                    cash -= cost
                    position = {
                        "buy_date": next_day["date"].strftime("%Y-%m-%d"),
                        "buy_price": buy_price,
                        "shares": shares,
                        "cost": cost,
                    }
                    signals_log.append(
                        f"  {next_day['date'].strftime('%Y-%m-%d')}  🟢买入  "
                        f"信号={category} (打分{result['score']})  "
                        f"买入价={buy_price:.3f}  股数={shares}  投入={cost:,.0f}"
                    )

    # --- 未平仓处理 ---
    if position is not None:
        last_price = df.iloc[-1]["close"]
        pnl_pct = (last_price - position["buy_price"]) / position["buy_price"]
        profit = position["shares"] * (last_price - position["buy_price"])
        hold_days = (df.iloc[-1]["date"] - pd.Timestamp(position["buy_date"])).days
        trades.append({
            "buy_date": position["buy_date"],
            "sell_date": df.iloc[-1]["date"].strftime("%Y-%m-%d") + "(未平)",
            "buy_price": position["buy_price"],
            "sell_price": last_price,
            "shares": position["shares"],
            "pnl": profit,
            "pnl_pct": pnl_pct * 100,
            "exit_reason": "未平仓(持有中)",
            "hold_days": hold_days,
        })

    # ============ 输出结果 ============
    print("\n📋 交易记录:")
    print("-" * 70)
    for log in signals_log:
        print(log)

    print("\n" + "=" * 70)
    print("📊 回测统计")
    print("=" * 70)

    if not trades:
        print("回测期间无交易信号。")
        return

    total_trades = len(trades)
    # 未平仓的不算胜负
    closed = [t for t in trades if "未平" not in t["exit_reason"]]
    open_pos = [t for t in trades if "未平" in t["exit_reason"]]

    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0

    total_pnl = sum(t["pnl"] for t in trades)
    total_return = total_pnl / CAPITAL * 100

    avg_win = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl_pct"] for t in losses]) if losses else 0
    avg_hold = np.mean([t["hold_days"] for t in closed]) if closed else 0
    max_win = max([t["pnl_pct"] for t in closed], default=0)
    max_loss = min([t["pnl_pct"] for t in closed], default=0)

    # 盈亏比
    profit_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    print(f"  总交易次数:   {total_trades} 笔 (已平 {len(closed)}, 未平 {len(open_pos)})")
    print(f"  胜率:         {win_rate:.1f}%  ({len(wins)}胜 / {len(losses)}负)")
    print(f"  盈亏比:       {profit_loss_ratio:.2f}")
    print(f"  平均盈利:     {avg_win:+.2f}%")
    print(f"  平均亏损:     {avg_loss:+.2f}%")
    print(f"  最大单笔盈:   {max_win:+.2f}%")
    print(f"  最大单笔亏:   {max_loss:+.2f}%")
    print(f"  平均持仓天数: {avg_hold:.1f} 天")
    print()
    print(f"  总盈亏:       {total_pnl:+,.0f} 元")
    print(f"  总收益率:     {total_return:+.2f}%")
    print(f"  期末资产:     {CAPITAL + total_pnl:,.0f} 元")

    # 止盈/止损分布
    tp_count = len([t for t in closed if t["exit_reason"] == "止盈"])
    sl_count = len([t for t in closed if "止损" in t["exit_reason"]])
    print(f"\n  止盈退出: {tp_count} 次  |  止损退出: {sl_count} 次")

    # 逐笔明细
    print(f"\n{'='*70}")
    print("📝 逐笔明细")
    print(f"{'='*70}")
    print(f"  {'买入日':>10}  {'卖出日':>14}  {'买价':>7}  {'卖价':>7}  {'盈亏%':>7}  {'盈亏额':>10}  {'天数':>4}  退出原因")
    print(f"  {'-'*10}  {'-'*14}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*10}  {'-'*4}  {'-'*12}")
    for t in trades:
        print(f"  {t['buy_date']:>10}  {t['sell_date']:>14}  "
              f"{t['buy_price']:>7.3f}  {t['sell_price']:>7.3f}  "
              f"{t['pnl_pct']:>+7.2f}%  {t['pnl']:>+10,.0f}  "
              f"{t['hold_days']:>4}  {t['exit_reason']}")

    # 资金曲线
    print(f"\n{'='*70}")
    print("📈 资金曲线 (关键节点)")
    print(f"{'='*70}")
    equity = CAPITAL
    print(f"  起始: {equity:>12,.0f}")
    for t in trades:
        equity += t["pnl"]
        tag = "✅" if t["pnl"] > 0 else "❌"
        print(f"  {t['sell_date']:>14}: {equity:>12,.0f}  ({t['pnl']:>+10,.0f}) {tag}")


if __name__ == "__main__":
    run_backtest()
