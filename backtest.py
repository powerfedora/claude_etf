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


def _is_stock(ts_code):
    """判断是股票还是ETF/基金。6开头沪市股票, 0/3开头深市股票; 5开头沪市基金, 1开头深市基金。"""
    code = ts_code.split(".")[0]
    return code.startswith(("6", "0", "3"))


def fetch_daily(ts_code, start, end):
    is_stock = _is_stock(ts_code)
    api_name = "daily" if is_stock else "fund_daily"
    adj_api = "adj_factor" if is_stock else "fund_adj"

    df = ts_api(api_name, ts_code=ts_code, start_date=start, end_date=end,
                fields="trade_date,open,high,low,close,vol")
    df = df.rename(columns={"trade_date": "date", "vol": "volume"})
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("date").dropna().reset_index(drop=True)

    try:
        adj = ts_api(adj_api, ts_code=ts_code, start_date=start, end_date=end,
                     fields="trade_date,adj_factor")
    except Exception:
        adj = pd.DataFrame()
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

STICK_COOLDOWN = 10  # 改进: 粘合止损后冷却期(交易日)


def simulate_strategy_trades(etf_df, idx_df, start_pos=130, end_pos=None, improved=False, close_at_end=False):
    """逐日模拟策略买卖, 返回 [{type, date, price, reason, ...}, ...]"""
    if idx_df is None or idx_df.empty:
        return []
    if end_pos is None:
        end_pos = len(etf_df) - 1
    if start_pos > end_pos or len(etf_df) <= start_pos:
        return []

    cash = CAPITAL
    shares = 0
    cost_price = 0.0
    max_profit_pct = 0.0
    trades = []
    holding = False
    pending_stick_buy = False
    stick_cooldown = 0
    last_cat = ""

    for pos in range(start_pos, end_pos + 1):
        row = etf_df.iloc[pos]
        today = row["date"]
        price = row["close"]

        d_slice = add_emas(etf_df.iloc[: pos + 1])
        w_slice = add_emas(resample_weekly(etf_df.iloc[: pos + 1]))
        m_slice = add_emas(resample_monthly(etf_df.iloc[: pos + 1]))

        idx_mask = idx_df["date"] <= today
        idx_pos = idx_mask.sum() - 1
        market = get_market_state_at(idx_df, idx_pos) if idx_pos >= 10 else "谨慎档"

        cat, score, verdict = daily_signal(d_slice, w_slice, m_slice, market)
        last_cat = cat

        if improved and stick_cooldown > 0:
            stick_cooldown -= 1

        if improved and pending_stick_buy and not holding:
            is_still_stick, still_dir, _ = detect_stick(d_slice)
            ema34_now = d_slice.iloc[-1][f"ema{EMA_MID}"]
            if price > ema34_now and (not is_still_stick or still_dir != "向下"):
                shares = int(cash // price)
                if shares > 0:
                    cost_price = price
                    cash -= shares * price
                    max_profit_pct = 0
                    holding = True
                    trades.append({
                        "type": "buy",
                        "date": today.strftime("%Y-%m-%d"),
                        "price": round(price, 4),
                        "reason": "粘合突破(次日确认)",
                        "category": "可关注-向上变盘",
                    })
            pending_stick_buy = False
            if holding:
                continue

        if holding:
            pnl_pct = (price - cost_price) / cost_price
            max_profit_pct = max(max_profit_pct, pnl_pct)
            sell_reason = None

            if cat == "回避":
                sell_reason = "周线方向闸失败"
            elif cat == "回避-向下变盘":
                sell_reason = "粘合向下跌破"
            elif price < d_slice.iloc[-1][f"ema{EMA_MID}"]:
                sell_reason = f"跌破EMA34({d_slice.iloc[-1][f'ema{EMA_MID}']:.4f})"
            elif max_profit_pct >= PROFIT_TRAIL and pnl_pct < 0.03:
                sell_reason = f"移动止盈(最高浮盈{max_profit_pct*100:.1f}%回落)"

            if sell_reason:
                if improved and trades and "向上变盘" in trades[-1].get("category", ""):
                    stick_cooldown = STICK_COOLDOWN
                cash += shares * price
                trades.append({
                    "type": "sell",
                    "date": today.strftime("%Y-%m-%d"),
                    "price": round(price, 4),
                    "reason": sell_reason,
                    "category": cat,
                })
                shares = 0
                cost_price = 0
                max_profit_pct = 0
                holding = False

        elif not holding and cat.startswith("可关注"):
            is_stick_signal = "向上变盘" in cat
            if improved and is_stick_signal:
                if stick_cooldown > 0:
                    continue
                vol_now = d_slice.iloc[-1]["volume"]
                vol_ma = d_slice["volume"].iloc[-VOL_LOOKBACK - 1:-1].mean()
                if vol_now <= vol_ma:
                    continue
                pending_stick_buy = True
                continue

            shares = int(cash // price)
            if shares > 0:
                cost_price = price
                cash -= shares * price
                max_profit_pct = 0
                holding = True
                trades.append({
                    "type": "buy",
                    "date": today.strftime("%Y-%m-%d"),
                    "price": round(price, 4),
                    "reason": verdict,
                    "category": cat,
                })

    if close_at_end and holding:
        last_price = etf_df.iloc[end_pos]["close"]
        trades.append({
            "type": "sell",
            "date": etf_df.iloc[end_pos]["date"].strftime("%Y-%m-%d"),
            "price": round(last_price, 4),
            "reason": "回测结束平仓",
            "category": last_cat,
        })

    return trades


def run_backtest(code="510210", name=None, idx_df=None, quiet=False, improved=False):
    """回测单只ETF。improved=True 启用3项改进: 次日确认/放量过滤/冷却期。"""
    ts_code = code.zfill(6)
    if "." in code:
        full_code = code
        ts_code = code.split(".")[0]
    else:
        suffix = "SH" if ts_code.startswith(("5", "6")) else "SZ"
        full_code = f"{ts_code}.{suffix}"
    label = name or ts_code

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = "20240101"

    if not quiet:
        print("=" * 60)
        print(f"  {ts_code} {label} 策略回测")
        print("  资金: 100万 | 回测区间: 近6个月")
        print("=" * 60)
        print(f"\n拉取 {ts_code} 日线数据...")

    try:
        etf_df = fetch_daily(full_code, start_date, end_date)
    except Exception as e:
        if not quiet:
            print(f"  数据获取失败: {e}")
        return {"code": ts_code, "name": label, "error": str(e)}

    if etf_df is None or len(etf_df) < 150:
        if not quiet:
            print(f"  数据不足({len(etf_df) if etf_df is not None else 0}条)")
        return {"code": ts_code, "name": label, "error": "数据不足"}

    if not quiet:
        print(f"  共 {len(etf_df)} 条日线, {etf_df['date'].iloc[0].date()} ~ {etf_df['date'].iloc[-1].date()}")

    if idx_df is None:
        time.sleep(1.5)
        if not quiet:
            print("拉取沪深300指数数据(大盘环境)...")
        idx_df = fetch_index("000300.SH", start_date, end_date)

    if not quiet:
        print(f"  共 {len(idx_df)} 条日线")

    # 回测窗口: 最近约 6 个月
    bt_start = etf_df["date"].iloc[-1] - pd.Timedelta(days=183)
    bt_start_idx = etf_df[etf_df["date"] >= bt_start].index[0]
    warmup = 130  # EMA 预热需要足够历史
    if bt_start_idx < warmup:
        bt_start_idx = warmup
    bt_dates = etf_df.iloc[bt_start_idx:].index.tolist()

    if not quiet:
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
    # 改进模式状态
    pending_stick_buy = False    # 粘合突破待确认
    stick_cooldown = 0           # 粘合止损后冷却倒计时
    last_sell_was_stick = False   # 上次卖出是否源于粘合入场

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

        if improved and stick_cooldown > 0:
            stick_cooldown -= 1

        # ---- 改进: 粘合突破次日确认 ----
        if improved and pending_stick_buy and not holding:
            is_still_stick, still_dir, _ = detect_stick(d_slice)
            ema34_now = d_slice.iloc[-1][f"ema{EMA_MID}"]
            if price > ema34_now and (not is_still_stick or still_dir != "向下"):
                shares = int(cash // price)
                if shares > 0:
                    cost_price = price
                    buy_amount = shares * price
                    cash -= buy_amount
                    max_profit_pct = 0
                    holding = True
                    last_sell_was_stick = False
                    trades.append({
                        "type": "buy",
                        "date": today.strftime("%Y-%m-%d"),
                        "price": round(price, 4),
                        "shares": shares,
                        "amount": round(buy_amount, 0),
                        "reason": "粘合突破(次日确认)",
                        "market": market,
                        "category": "可关注-向上变盘",
                        "score": score,
                    })
            pending_stick_buy = False
            if holding:
                continue

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
                # 改进: 粘合入场被止损 -> 启动冷却期
                if improved and "向上变盘" in trades[-1].get("category", ""):
                    stick_cooldown = STICK_COOLDOWN
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
            is_stick_signal = "向上变盘" in cat

            if improved and is_stick_signal:
                # 改进1: 冷却期内不做粘合突破
                if stick_cooldown > 0:
                    continue
                # 改进2: 粘合突破必须放量
                vol_now = d_slice.iloc[-1]["volume"]
                vol_ma = d_slice["volume"].iloc[-VOL_LOOKBACK - 1:-1].mean()
                if vol_now <= vol_ma:
                    continue
                # 改进3: 不立即买, 设为待确认, 次日再决定
                pending_stick_buy = True
                continue

            # 非粘合信号(金叉/回踩/蚂蚁上树): 正常买入
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
    sells = [t for t in trades if t["type"] == "sell"]
    wins = [t for t in sells if t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] <= 0]

    total_pnl = sum(t["pnl"] for t in sells)
    final_value = cash
    total_return = (final_value - CAPITAL) / CAPITAL * 100

    n_trades = len(sells)
    win_rate = len(wins) / n_trades * 100 if n_trades else 0

    hold_days = []
    for i in range(0, len(trades) - 1, 2):
        if trades[i]["type"] == "buy" and i + 1 < len(trades) and trades[i+1]["type"] == "sell":
            bd = pd.Timestamp(trades[i]["date"])
            sd = pd.Timestamp(trades[i+1]["date"])
            hold_days.append((sd - bd).days)

    # 最大回撤
    nav_series = []
    _cash = CAPITAL
    _shares = 0
    trade_idx = 0
    for i_day in bt_dates:
        row = etf_df.loc[i_day]
        today = row["date"].strftime("%Y-%m-%d")
        price = row["close"]
        while trade_idx < len(trades) and trades[trade_idx]["date"] == today:
            t = trades[trade_idx]
            if t["type"] == "buy":
                _shares = t["shares"]
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

    bh_start_price = etf_df.loc[bt_dates[0], "close"]
    bh_end_price = etf_df.loc[bt_dates[-1], "close"]
    bh_return = (bh_end_price - bh_start_price) / bh_start_price * 100

    avg_win = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
    avg_loss = np.mean([t["pnl_pct"] for t in losses]) if losses else 0
    pl_ratio = abs(avg_win) / abs(avg_loss) if losses and avg_loss != 0 else float("inf") if wins else 0

    summary = {
        "code": ts_code, "name": label,
        "total_return": round(total_return, 2),
        "bh_return": round(bh_return, 2),
        "excess": round(total_return - bh_return, 2),
        "n_trades": n_trades,
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "pl_ratio": round(pl_ratio, 2),
        "max_dd": round(max_dd, 2),
        "avg_hold_days": round(np.mean(hold_days)) if hold_days else 0,
        "trades": trades,
    }

    if not quiet:
        print("\n" + "=" * 60)
        print("  回测结果")
        print("=" * 60)
        print(f"\n{'初始资金':>12}: {CAPITAL:>12,.0f} 元")
        print(f"{'期末资金':>12}: {final_value:>12,.0f} 元")
        print(f"{'总收益':>12}: {total_pnl:>+12,.0f} 元")
        print(f"{'总收益率':>12}: {total_return:>+11.2f}%")
        print(f"\n{'总交易轮次':>12}: {n_trades}")
        print(f"{'盈利次数':>12}: {len(wins)}")
        print(f"{'亏损次数':>12}: {len(losses)}")
        print(f"{'胜率':>12}: {win_rate:.1f}%")
        if wins:
            print(f"{'平均盈利':>12}: +{avg_win:.2f}%")
            print(f"{'最大单笔盈利':>12}: +{max(t['pnl_pct'] for t in wins):.2f}%")
        if losses:
            print(f"{'平均亏损':>12}: {avg_loss:.2f}%")
            print(f"{'最大单笔亏损':>12}: {min(t['pnl_pct'] for t in losses):.2f}%")
        if wins and losses:
            print(f"{'盈亏比':>12}: {pl_ratio:.2f}")
        if hold_days:
            print(f"{'平均持仓天数':>12}: {np.mean(hold_days):.0f} 天")
        print(f"{'最大回撤':>12}: {max_dd:.2f}%")

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
        print(f"\n  买入持有对比: {bh_start_price:.4f} -> {bh_end_price:.4f}, 收益率 {bh_return:+.2f}%")
        print(f"  策略 vs 买入持有: {total_return - bh_return:+.2f}% {'超额' if total_return > bh_return else '不及'}")
        print("=" * 60)

    return summary


# ============ 批量回测 ============

def run_batch():
    """读取 etf_list.txt, 逐只回测, 输出汇总排行榜。"""
    from pathlib import Path
    list_file = Path(__file__).resolve().parent / "etf_list.txt"
    codes = []
    with open(list_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").split()
            code = parts[0].zfill(6)
            name = parts[1] if len(parts) > 1 else code
            codes.append((code, name))

    print("=" * 70)
    print(f"  ETF 策略批量回测 | 共 {len(codes)} 只 | 资金 100万/只 | 近6个月")
    print("=" * 70)

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = "20240101"
    print("\n拉取沪深300指数数据(大盘环境, 全局复用)...")
    idx_df = fetch_index("000300.SH", start_date, end_date)
    print(f"  共 {len(idx_df)} 条日线\n")

    results = []
    for i, (code, name) in enumerate(codes, 1):
        print(f"[{i:>2}/{len(codes)}] {code} {name} ...", end=" ", flush=True)
        try:
            s = run_backtest(code, name, idx_df=idx_df, quiet=True)
            if s.get("error"):
                print(f"失败: {s['error']}")
            else:
                print(f"收益{s['total_return']:>+7.2f}% | 胜率{s['win_rate']:>5.1f}% | "
                      f"{s['n_trades']}笔 | 回撤{s['max_dd']:>6.2f}% | "
                      f"超额{s['excess']:>+7.2f}%")
            results.append(s)
        except Exception as e:
            print(f"异常: {e}")
            results.append({"code": code, "name": name, "error": str(e)})
        time.sleep(1.5)

    # ---- 汇总排行榜 ----
    valid = [r for r in results if not r.get("error")]
    failed = [r for r in results if r.get("error")]

    print("\n" + "=" * 70)
    print("  汇总排行榜 (按策略收益率排序)")
    print("=" * 70)
    print(f"{'排名':>4} {'代码':>8} {'名称':<10} {'策略收益':>8} {'买入持有':>8} {'超额':>8} "
          f"{'胜率':>6} {'笔数':>4} {'盈亏比':>6} {'回撤':>7} {'持仓天':>6}")
    print("-" * 100)

    valid.sort(key=lambda r: r["total_return"], reverse=True)
    for rank, r in enumerate(valid, 1):
        print(f"{rank:>4} {r['code']:>8} {r['name']:<10} {r['total_return']:>+7.2f}% "
              f"{r['bh_return']:>+7.2f}% {r['excess']:>+7.2f}% "
              f"{r['win_rate']:>5.1f}% {r['n_trades']:>4} {r['pl_ratio']:>6.2f} "
              f"{r['max_dd']:>6.2f}% {r['avg_hold_days']:>5.0f}")

    # 全局统计
    if valid:
        avg_ret = np.mean([r["total_return"] for r in valid])
        avg_bh = np.mean([r["bh_return"] for r in valid])
        avg_excess = np.mean([r["excess"] for r in valid])
        pos_count = sum(1 for r in valid if r["total_return"] > 0)
        excess_count = sum(1 for r in valid if r["excess"] > 0)
        all_wins = sum(r["wins"] for r in valid)
        all_losses = sum(r["losses"] for r in valid)
        all_trades = sum(r["n_trades"] for r in valid)
        overall_wr = all_wins / all_trades * 100 if all_trades else 0
        no_trade = sum(1 for r in valid if r["n_trades"] == 0)

        print("-" * 100)
        print(f"\n{'有效标的':>12}: {len(valid)} 只 (失败 {len(failed)} 只)")
        print(f"{'平均策略收益':>12}: {avg_ret:+.2f}%")
        print(f"{'平均买入持有':>12}: {avg_bh:+.2f}%")
        print(f"{'平均超额收益':>12}: {avg_excess:+.2f}%")
        print(f"{'盈利标的数':>12}: {pos_count}/{len(valid)} ({pos_count/len(valid)*100:.0f}%)")
        print(f"{'跑赢买入持有':>12}: {excess_count}/{len(valid)} ({excess_count/len(valid)*100:.0f}%)")
        print(f"{'总交易笔数':>12}: {all_trades} (盈{all_wins}/亏{all_losses})")
        print(f"{'全局胜率':>12}: {overall_wr:.1f}%")
        print(f"{'无交易标的':>12}: {no_trade} 只 (半年内无信号触发)")

    if failed:
        print(f"\n失败标的: {', '.join(r['code']+' '+r['name'] for r in failed)}")

    print("=" * 70)


def run_compare():
    """原版 vs 改进版 对比回测。"""
    from pathlib import Path
    list_file = Path(__file__).resolve().parent / "etf_list.txt"
    codes = []
    with open(list_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").split()
            code = parts[0].zfill(6)
            name = parts[1] if len(parts) > 1 else code
            codes.append((code, name))

    print("=" * 90)
    print(f"  原版 vs 改进版 对比回测 | 共 {len(codes)} 只 | 资金 100万/只 | 近6个月")
    print("  改进项: ①粘合突破次日确认 ②突破须放量 ③止损后10日冷却")
    print("=" * 90)

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = "20240101"
    print("\n拉取沪深300指数...")
    idx_df = fetch_index("000300.SH", start_date, end_date)
    print(f"  共 {len(idx_df)} 条\n")

    old_results = []
    new_results = []
    for i, (code, name) in enumerate(codes, 1):
        print(f"[{i:>2}/{len(codes)}] {code} {name} ...", end=" ", flush=True)
        try:
            s_old = run_backtest(code, name, idx_df=idx_df, quiet=True, improved=False)
            s_new = run_backtest(code, name, idx_df=idx_df, quiet=True, improved=True)
            old_results.append(s_old)
            new_results.append(s_new)
            if s_old.get("error"):
                print(f"失败: {s_old['error']}")
            else:
                delta = s_new["total_return"] - s_old["total_return"]
                print(f"原版{s_old['total_return']:>+7.2f}%({s_old['n_trades']}笔) | "
                      f"改进{s_new['total_return']:>+7.2f}%({s_new['n_trades']}笔) | "
                      f"差异{delta:>+6.2f}%")
        except Exception as e:
            print(f"异常: {e}")
            old_results.append({"code": code, "name": name, "error": str(e)})
            new_results.append({"code": code, "name": name, "error": str(e)})
        time.sleep(1.5)

    # ---- 对比排行榜 ----
    valid_old = [r for r in old_results if not r.get("error")]
    valid_new = [r for r in new_results if not r.get("error")]
    by_code_new = {r["code"]: r for r in valid_new}

    print("\n" + "=" * 90)
    print("  对比排行榜 (按改进版收益排序)")
    print("=" * 90)
    print(f"{'排名':>4} {'代码':>8} {'名称':<10} {'原版收益':>8} {'原版胜率':>7} {'原笔数':>5} "
          f"{'改进收益':>8} {'改进胜率':>7} {'改笔数':>5} {'收益变化':>8}")
    print("-" * 105)

    pairs = []
    for r_old in valid_old:
        r_new = by_code_new.get(r_old["code"])
        if r_new and not r_new.get("error"):
            pairs.append((r_old, r_new))
    pairs.sort(key=lambda p: p[1]["total_return"], reverse=True)

    improved_count = 0
    for rank, (r_old, r_new) in enumerate(pairs, 1):
        delta = r_new["total_return"] - r_old["total_return"]
        marker = "+" if delta > 0.01 else ("-" if delta < -0.01 else "=")
        if delta > 0.01:
            improved_count += 1
        print(f"{rank:>4} {r_old['code']:>8} {r_old['name']:<10} "
              f"{r_old['total_return']:>+7.2f}% {r_old['win_rate']:>6.1f}% {r_old['n_trades']:>5} "
              f"{r_new['total_return']:>+7.2f}% {r_new['win_rate']:>6.1f}% {r_new['n_trades']:>5} "
              f"{delta:>+7.2f}% {marker}")
    print("-" * 105)

    # 全局对比
    avg_old = np.mean([r["total_return"] for r in valid_old])
    avg_new = np.mean([r["total_return"] for r in valid_new])
    trades_old = sum(r["n_trades"] for r in valid_old)
    trades_new = sum(r["n_trades"] for r in valid_new)
    wins_old = sum(r["wins"] for r in valid_old)
    wins_new = sum(r["wins"] for r in valid_new)
    losses_old = sum(r["losses"] for r in valid_old)
    losses_new = sum(r["losses"] for r in valid_new)
    wr_old = wins_old / trades_old * 100 if trades_old else 0
    wr_new = wins_new / trades_new * 100 if trades_new else 0
    pos_old = sum(1 for r in valid_old if r["total_return"] > 0)
    pos_new = sum(1 for r in valid_new if r["total_return"] > 0)
    neg_old = sum(1 for r in valid_old if r["total_return"] < 0)
    neg_new = sum(1 for r in valid_new if r["total_return"] < 0)

    print(f"\n{'':>20} {'原版':>12} {'改进版':>12} {'变化':>10}")
    print(f"{'平均收益':>20} {avg_old:>+11.2f}% {avg_new:>+11.2f}% {avg_new-avg_old:>+9.2f}%")
    print(f"{'盈利标的':>20} {pos_old:>12} {pos_new:>12} {pos_new-pos_old:>+10}")
    print(f"{'亏损标的':>20} {neg_old:>12} {neg_new:>12} {neg_new-neg_old:>+10}")
    print(f"{'总交易笔数':>20} {trades_old:>12} {trades_new:>12} {trades_new-trades_old:>+10}")
    print(f"{'全局胜率':>20} {wr_old:>11.1f}% {wr_new:>11.1f}% {wr_new-wr_old:>+9.1f}%")
    print(f"{'改善标的数':>20} {improved_count}/{len(pairs)} ({improved_count/len(pairs)*100:.0f}%)")
    print("=" * 90)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--batch":
        run_batch()
    elif len(sys.argv) > 1 and sys.argv[1] == "--compare":
        run_compare()
    else:
        code = sys.argv[1] if len(sys.argv) > 1 else "510210"
        name = sys.argv[2] if len(sys.argv) > 2 else None
        improved = "--improved" in sys.argv
        run_backtest(code, name, improved=improved)
