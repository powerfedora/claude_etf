"""
510210 上证综指ETF 回测
========================
复刻 engine.py 策略, 逐日模拟, 计算半年胜率。
资金 100 万, 仅买卖 510210 一只。

买入条件 (与 engine.py "可关注" 一致):
  1. 周线方向闸通过 (周 EMA13 > 周 EMA34)
  2. 大盘非防守/清仓
  3. 以下任一:
     a) 打分 >= 4 且 (日线金叉 or 回踩EMA34)
     b) 蚂蚁上树 + 周线点头
     c) 粘合共振向上突破

卖出条件:
  1. 周线方向闸失败 (周 EMA13 < EMA34)
  2. 粘合共振向下跌破
  3. 收盘跌破日线 EMA34 (止损)
  4. 浮盈达 12% 后回撤至盈亏平衡 (移动止盈)

用法: python backtest.py
"""

import warnings
warnings.filterwarnings("ignore")

import json
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime

TOKEN = "cf4790f0edb0c2eef691a576590c9f1abd5e670cd4e304bc32911a75"
API_URL = "https://api.tushare.pro"

# ---- 策略参数 (与 engine.py 一致) ----
EMA_FAST, EMA_MID, EMA_SLOW = 13, 34, 55
VOL_LOOKBACK = 5
GAP_THRESHOLD = 0.005
SCORE_ENTER = 4
PROFIT_TRAIL = 0.12
ANT_LOOKBACK = 8
ANT_ABOVE_RATIO = 0.7
ANT_MAX_POS = 60
STICK_THRESHOLD = 0.015
STICK_WINDOW = 10
STICK_BREAK = 0.005

CAPITAL = 1_000_000


# ============ 数据获取 ============

def ts_api(api_name, **params):
    payload = {"api_name": api_name, "token": TOKEN, "params": params}
    r = requests.post(API_URL, json=payload, verify=False, timeout=30)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"Tushare error: {d.get('msg')}")
    fields = d["data"]["fields"]
    items = d["data"]["items"]
    return pd.DataFrame(items, columns=fields)


def fetch_daily(ts_code, start, end):
    df = ts_api("fund_daily", ts_code=ts_code, start_date=start, end_date=end,
                fields="trade_date,open,high,low,close,vol")
    df = df.rename(columns={"trade_date": "date", "vol": "volume"})
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("date").dropna().reset_index(drop=True)

    adj = ts_api("fund_adj", ts_code=ts_code, start_date=start, end_date=end,
                 fields="trade_date,adj_factor")
    if not adj.empty:
        adj["date"] = pd.to_datetime(adj["trade_date"], format="%Y%m%d")
        adj["adj_factor"] = pd.to_numeric(adj["adj_factor"], errors="coerce")
        adj = adj[["date", "adj_factor"]].sort_values("date").dropna().reset_index(drop=True)
        merged = df.merge(adj, on="date", how="left")
        merged["adj_factor"] = merged["adj_factor"].ffill().bfill()
        factor = merged["adj_factor"] / merged["adj_factor"].iloc[-1]
        for c in ("open", "high", "low", "close"):
            df[c] = merged[c] * factor
    return df


def fetch_index(ts_code, start, end):
    df = ts_api("index_daily", ts_code=ts_code, start_date=start, end_date=end,
                fields="trade_date,open,high,low,close,vol")
    df = df.rename(columns={"trade_date": "date", "vol": "volume"})
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values("date").dropna().reset_index(drop=True)


# ============ 指标计算 ============

def add_emas(df):
    df = df.copy()
    for n in (EMA_FAST, EMA_MID, EMA_SLOW):
        df[f"ema{n}"] = df["close"].ewm(span=n, adjust=False).mean()
    return df


def resample_weekly(df):
    d = df.set_index("date")
    w = pd.DataFrame({
        "open": d["open"].resample("W-FRI").first(),
        "high": d["high"].resample("W-FRI").max(),
        "low": d["low"].resample("W-FRI").min(),
        "close": d["close"].resample("W-FRI").last(),
        "volume": d["volume"].resample("W-FRI").sum(),
    }).dropna().reset_index()
    return w


def resample_monthly(df):
    d = df.set_index("date")
    m = pd.DataFrame({
        "open": d["open"].resample("ME").first(),
        "high": d["high"].resample("ME").max(),
        "low": d["low"].resample("ME").min(),
        "close": d["close"].resample("ME").last(),
        "volume": d["volume"].resample("ME").sum(),
    }).dropna().reset_index()
    return m


def cross_recently(df, fast, mid, window=5):
    f = df[f"ema{fast}"]
    m = df[f"ema{mid}"]
    diff = f - m
    sign = np.sign(diff)
    crosses = sign.diff()
    recent = crosses.iloc[-window:]
    if (recent > 0).any():
        return "金叉"
    if (recent < 0).any():
        return "死叉"
    return "无"


def detect_ant_climb(d, lookback=ANT_LOOKBACK):
    if len(d) < lookback + 4:
        return False
    last = d.iloc[-1]
    ef, em_ = last[f"ema{EMA_FAST}"], last[f"ema{EMA_MID}"]
    price = last["close"]
    short_bull = price > ef > em_
    gap_now = ef - em_
    gap_prev = d.iloc[-4][f"ema{EMA_FAST}"] - d.iloc[-4][f"ema{EMA_MID}"]
    fan_up = ef > d.iloc[-4][f"ema{EMA_FAST}"] and gap_now > gap_prev
    recent = d.iloc[-lookback:]
    above = (recent["close"] >= recent[f"ema{EMA_FAST}"]).mean() >= ANT_ABOVE_RATIO
    climbing = recent["close"].iloc[-1] > recent["close"].iloc[0]
    return bool(short_bull and fan_up and above and climbing)


def detect_stick(d, threshold=STICK_THRESHOLD, window=STICK_WINDOW):
    cols = [f"ema{EMA_FAST}", f"ema{EMA_MID}", f"ema{EMA_SLOW}"]
    sub = d.iloc[-window:]
    spreads = (sub[cols].max(axis=1) - sub[cols].min(axis=1)) / sub["close"]
    min_spread = float(spreads.min())
    is_stick = min_spread < threshold
    last = d.iloc[-1]
    ef, em_, es = last[f"ema{EMA_FAST}"], last[f"ema{EMA_MID}"], last[f"ema{EMA_SLOW}"]
    price = last["close"]
    band_top, band_bot = max(ef, em_, es), min(ef, em_, es)
    if price > band_top * (1 + STICK_BREAK) and ef > em_:
        direction = "向上"
    elif price < band_bot * (1 - STICK_BREAK):
        direction = "向下"
    else:
        direction = "未明"
    return is_stick, direction, round(min_spread * 100, 2)


def get_market_state_at(idx_df, idx_pos):
    if idx_pos < 5:
        return "谨慎档"
    sub = add_emas(idx_df.iloc[:idx_pos + 1])
    last = sub.iloc[-1]
    p = last["close"]
    ef = last[f"ema{EMA_FAST}"]
    em_ = last[f"ema{EMA_MID}"]
    es = last[f"ema{EMA_SLOW}"]
    es_rising = last[f"ema{EMA_SLOW}"] > sub.iloc[-5][f"ema{EMA_SLOW}"]
    if p > ef > em_ > es:
        return "进攻档"
    if p > es and es_rising:
        return "谨慎档"
    if p < es:
        return "防守档"
    return "谨慎档"


# ============ 逐日信号判断 ============

def daily_signal(d_all, w_all, m_all, market_state):
    """对截至当日的全部数据, 返回 (category, score, verdict)"""
    d = d_all
    w = w_all
    m = m_all
    if len(d) < 60 or len(w) < 10 or len(m) < 3:
        return "数据不足", 0, ""

    last_d = d.iloc[-1]
    last_w = w.iloc[-1]
    price = last_d["close"]

    # 均线状态
    ef_d, em_d, es_d = last_d[f"ema{EMA_FAST}"], last_d[f"ema{EMA_MID}"], last_d[f"ema{EMA_SLOW}"]
    if price > ef_d > em_d > es_d:
        day_state = "多头排列"
    elif price < ef_d < em_d < es_d:
        day_state = "空头排列"
    elif price > es_d:
        day_state = "偏多缠绕"
    else:
        day_state = "偏空缠绕"

    day_cross = cross_recently(d, EMA_FAST, EMA_MID, window=5)

    # 量能
    vol_now = last_d["volume"]
    vol_ma = d["volume"].iloc[-VOL_LOOKBACK - 1:-1].mean()
    is_volume_up = vol_now > vol_ma

    # 金叉张口
    gap = (ef_d - em_d) / em_d
    gap_ok = gap >= GAP_THRESHOLD

    # 回踩
    near_mid = abs(price - em_d) / em_d < 0.02
    is_pullback = near_mid and day_state == "多头排列" and day_cross != "死叉"

    # 打分
    score = 0
    if price > es_d and es_d > d.iloc[-3][f"ema{EMA_SLOW}"]:
        score += 2
    if is_volume_up:
        score += 1
    if gap_ok:
        score += 1
    if market_state == "进攻档":
        score += 1
    ef_w = last_w[f"ema{EMA_FAST}"]
    em_w = last_w[f"ema{EMA_MID}"]
    if price > ef_w > em_w:  # 简化: 周线多头
        score += 1

    # 周线方向闸
    week_gate_ok = ef_w > em_w
    week_nod = week_gate_ok and ef_w > w.iloc[-2][f"ema{EMA_FAST}"]

    # 月线位置
    m_high = m["high"].max()
    m_low = m["low"].min()
    pos_pct = (price - m_low) / (m_high - m_low) * 100 if m_high > m_low else 50

    # 135 信号
    ant_shape = detect_ant_climb(d)
    ant_climb = ant_shape and pos_pct <= ANT_MAX_POS
    is_stick, stick_dir, stick_spread = detect_stick(d)

    # 分类
    if not week_gate_ok:
        return "回避", score, "周线方向闸未过"
    if market_state in ("防守档", "清仓"):
        return "观望", score, "大盘防守"
    if is_stick:
        if stick_dir == "向下":
            return "回避-向下变盘", score, f"粘合向下跌破"
        elif stick_dir == "向上":
            return "可关注-向上变盘", score, f"粘合向上突破"
        else:
            return "观望-变盘待定", score, "粘合方向未明"
    if score >= SCORE_ENTER and (day_cross == "金叉" or is_pullback):
        if is_pullback:
            return "可关注-回踩", score, f"回踩EMA34"
        return "可关注-金叉", score, f"日线金叉"
    if ant_climb:
        if week_nod:
            return "可关注-蚂蚁上树", score, "蚂蚁上树+周线点头"
        return "观望-待周线点头", score, "蚂蚁上树但周线未点头"
    if day_state == "多头排列":
        return "持有/观察", score, "多头排列无新信号"
    return "观望", score, f"信号不足(打分{score})"


# ============ 回测主逻辑 ============

def run_backtest(code="510210", name=None):
    ts_code = code.zfill(6)
    suffix = "SH" if ts_code.startswith(("5", "6")) else "SZ"
    full_code = f"{ts_code}.{suffix}"
    label = name or ts_code

    print("=" * 60)
    print(f"  {ts_code} {label} 策略回测")
    print("  资金: 100万 | 回测区间: 近6个月")
    print("=" * 60)

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = "20240101"  # 拉长数据用于 EMA 预热

    print(f"\n拉取 {ts_code} 日线数据...")
    etf_df = fetch_daily(full_code, start_date, end_date)
    print(f"  共 {len(etf_df)} 条日线, {etf_df['date'].iloc[0].date()} ~ {etf_df['date'].iloc[-1].date()}")

    time.sleep(1.5)
    print("拉取沪深300指数数据(大盘环境)...")
    idx_df = fetch_index("000300.SH", start_date, end_date)
    print(f"  共 {len(idx_df)} 条日线")

    # 回测窗口: 最近约 6 个月
    bt_start = etf_df["date"].iloc[-1] - pd.Timedelta(days=183)
    bt_start_idx = etf_df[etf_df["date"] >= bt_start].index[0]
    warmup = 130  # EMA 预热需要足够历史
    if bt_start_idx < warmup:
        bt_start_idx = warmup
    bt_dates = etf_df.iloc[bt_start_idx:].index.tolist()

    print(f"\n回测窗口: {etf_df.loc[bt_dates[0], 'date'].date()} ~ {etf_df.loc[bt_dates[-1], 'date'].date()}")
    print(f"回测交易日数: {len(bt_dates)}")
    print("-" * 60)

    # 状态
    cash = CAPITAL
    shares = 0
    cost_price = 0.0
    max_profit_pct = 0.0
    trades = []
    holding = False

    for i_day in bt_dates:
        row = etf_df.loc[i_day]
        today = row["date"]
        price = row["close"]

        # 截至今天的数据切片
        d_slice = add_emas(etf_df.loc[:i_day])
        w_slice = add_emas(resample_weekly(etf_df.loc[:i_day]))
        m_slice = add_emas(resample_monthly(etf_df.loc[:i_day]))

        # 大盘环境
        idx_mask = idx_df["date"] <= today
        idx_pos = idx_mask.sum() - 1
        market = get_market_state_at(idx_df, idx_pos) if idx_pos >= 10 else "谨慎档"

        cat, score, verdict = daily_signal(d_slice, w_slice, m_slice, market)

        # ---- 持仓中: 检查卖出条件 ----
        if holding:
            pnl_pct = (price - cost_price) / cost_price
            max_profit_pct = max(max_profit_pct, pnl_pct)

            sell_reason = None

            # 1) 周线闸失败
            if cat == "回避":
                sell_reason = "周线方向闸失败"
            # 2) 粘合向下变盘
            elif cat == "回避-向下变盘":
                sell_reason = "粘合向下跌破"
            # 3) 跌破 EMA34 止损
            elif price < d_slice.iloc[-1][f"ema{EMA_MID}"]:
                ema34_val = d_slice.iloc[-1][f"ema{EMA_MID}"]
                sell_reason = f"跌破EMA34({ema34_val:.4f})"
            # 4) 移动止盈: 浮盈曾达12%, 回落到盈亏平衡附近(3%以下)就锁利
            elif max_profit_pct >= PROFIT_TRAIL and pnl_pct < 0.03:
                sell_reason = f"移动止盈(最高浮盈{max_profit_pct*100:.1f}%回落)"

            if sell_reason:
                sell_amount = shares * price
                pnl = sell_amount - shares * cost_price
                cash += sell_amount
                trades.append({
                    "type": "sell",
                    "date": today.strftime("%Y-%m-%d"),
                    "price": round(price, 4),
                    "shares": shares,
                    "amount": round(sell_amount, 0),
                    "pnl": round(pnl, 0),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "reason": sell_reason,
                    "market": market,
                    "category": cat,
                })
                shares = 0
                cost_price = 0
                max_profit_pct = 0
                holding = False

        # ---- 空仓: 检查买入条件 ----
        elif not holding and cat.startswith("可关注"):
            shares = int(cash // price)
            if shares > 0:
                cost_price = price
                buy_amount = shares * price
                cash -= buy_amount
                max_profit_pct = 0
                holding = True
                trades.append({
                    "type": "buy",
                    "date": today.strftime("%Y-%m-%d"),
                    "price": round(price, 4),
                    "shares": shares,
                    "amount": round(buy_amount, 0),
                    "reason": verdict,
                    "market": market,
                    "category": cat,
                    "score": score,
                })

    # ---- 回测结束, 若仍持仓则按最后收盘价平仓 ----
    if holding:
        last_price = etf_df.iloc[-1]["close"]
        pnl_pct = (last_price - cost_price) / cost_price
        sell_amount = shares * last_price
        pnl = sell_amount - shares * cost_price
        cash += sell_amount
        trades.append({
            "type": "sell",
            "date": etf_df.iloc[-1]["date"].strftime("%Y-%m-%d"),
            "price": round(last_price, 4),
            "shares": shares,
            "amount": round(sell_amount, 0),
            "pnl": round(pnl, 0),
            "pnl_pct": round(pnl_pct * 100, 2),
            "reason": "回测结束平仓",
            "market": market,
            "category": cat,
        })
        shares = 0
        holding = False

    # ============ 统计 ============
    buys = [t for t in trades if t["type"] == "buy"]
    sells = [t for t in trades if t["type"] == "sell"]
    wins = [t for t in sells if t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] <= 0]

    total_pnl = sum(t["pnl"] for t in sells)
    final_value = cash
    total_return = (final_value - CAPITAL) / CAPITAL * 100

    print("\n" + "=" * 60)
    print("  回测结果")
    print("=" * 60)

    print(f"\n{'初始资金':>12}: {CAPITAL:>12,.0f} 元")
    print(f"{'期末资金':>12}: {final_value:>12,.0f} 元")
    print(f"{'总收益':>12}: {total_pnl:>+12,.0f} 元")
    print(f"{'总收益率':>12}: {total_return:>+11.2f}%")

    n_trades = len(sells)
    win_rate = len(wins) / n_trades * 100 if n_trades else 0
    print(f"\n{'总交易轮次':>12}: {n_trades}")
    print(f"{'盈利次数':>12}: {len(wins)}")
    print(f"{'亏损次数':>12}: {len(losses)}")
    print(f"{'胜率':>12}: {win_rate:.1f}%")

    if wins:
        avg_win = np.mean([t["pnl_pct"] for t in wins])
        max_win = max(t["pnl_pct"] for t in wins)
        print(f"{'平均盈利':>12}: +{avg_win:.2f}%")
        print(f"{'最大单笔盈利':>12}: +{max_win:.2f}%")
    if losses:
        avg_loss = np.mean([t["pnl_pct"] for t in losses])
        max_loss = min(t["pnl_pct"] for t in losses)
        print(f"{'平均亏损':>12}: {avg_loss:.2f}%")
        print(f"{'最大单笔亏损':>12}: {max_loss:.2f}%")
    if wins and losses:
        profit_loss_ratio = abs(np.mean([t["pnl_pct"] for t in wins])) / abs(np.mean([t["pnl_pct"] for t in losses]))
        print(f"{'盈亏比':>12}: {profit_loss_ratio:.2f}")

    # 持仓天数统计
    hold_days = []
    for i in range(0, len(trades) - 1, 2):
        if trades[i]["type"] == "buy" and i + 1 < len(trades) and trades[i+1]["type"] == "sell":
            bd = pd.Timestamp(trades[i]["date"])
            sd = pd.Timestamp(trades[i+1]["date"])
            hold_days.append((sd - bd).days)
    if hold_days:
        print(f"{'平均持仓天数':>12}: {np.mean(hold_days):.0f} 天")

    # 最大回撤 (逐日净值)
    nav_series = []
    _cash = CAPITAL
    _shares = 0
    _cost = 0
    trade_idx = 0
    for i_day in bt_dates:
        row = etf_df.loc[i_day]
        today = row["date"].strftime("%Y-%m-%d")
        price = row["close"]
        while trade_idx < len(trades) and trades[trade_idx]["date"] == today:
            t = trades[trade_idx]
            if t["type"] == "buy":
                _shares = t["shares"]
                _cost = t["price"]
                _cash -= t["amount"]
            elif t["type"] == "sell":
                _cash += t["amount"]
                _shares = 0
            trade_idx += 1
        nav = _cash + _shares * price
        nav_series.append(nav)

    nav_arr = np.array(nav_series)
    peak = np.maximum.accumulate(nav_arr)
    drawdown = (nav_arr - peak) / peak
    max_dd = drawdown.min() * 100
    print(f"{'最大回撤':>12}: {max_dd:.2f}%")

    # ---- 逐笔明细 ----
    print("\n" + "-" * 60)
    print("  逐笔交易明细")
    print("-" * 60)
    for t in trades:
        if t["type"] == "buy":
            print(f"  买入 | {t['date']} | 价格 {t['price']:.4f} | "
                  f"金额 {t['amount']:>10,.0f} | {t['category']}(打分{t.get('score','')}) | "
                  f"大盘:{t['market']} | {t['reason']}")
        else:
            color = "盈" if t["pnl"] > 0 else "亏"
            print(f"  卖出 | {t['date']} | 价格 {t['price']:.4f} | "
                  f"金额 {t['amount']:>10,.0f} | {color} {t['pnl']:>+8,.0f}({t['pnl_pct']:>+.2f}%) | "
                  f"{t['reason']}")
    print("-" * 60)

    # Buy & Hold 对比
    bh_start_price = etf_df.loc[bt_dates[0], "close"]
    bh_end_price = etf_df.loc[bt_dates[-1], "close"]
    bh_return = (bh_end_price - bh_start_price) / bh_start_price * 100
    print(f"\n  买入持有对比: {bh_start_price:.4f} -> {bh_end_price:.4f}, 收益率 {bh_return:+.2f}%")
    print(f"  策略 vs 买入持有: {total_return - bh_return:+.2f}% {'超额' if total_return > bh_return else '不及'}")
    print("=" * 60)


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "510210"
    name = sys.argv[2] if len(sys.argv) > 2 else None
    run_backtest(code, name)
