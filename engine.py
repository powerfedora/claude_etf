"""
ETF 多周期均线扫描 - 核心引擎
=================================
不依赖任何外部数据源, 纯计算 + 打分 + 信号判断。
输入: 一只ETF的日线 DataFrame (date, open, high, low, close, volume)
输出: 该ETF的完整分析结果 dict

复刻的策略框架(与对话中一致):
  - 三层: 月线基调 / 周线方向闸 / 日线买卖点
  - 双均线套: EMA 13/34/55 (主判据) + EMA 12/50/120 (同花顺EXPMA对照)
  - 打分制: 价在长均线上方+2, 放量+1, 金叉张口+1, 多头排列+1, 大盘环境+1, ≥4 关注
"""

import pandas as pd
import numpy as np

# ============ 可配置参数 (改这里即可) ============
EMA_FAST, EMA_MID, EMA_SLOW = 13, 34, 55          # 主判据均线
EMA_FAST2, EMA_MID2, EMA_SLOW2 = 12, 50, 120      # 同花顺EXPMA对照均线
VOL_LOOKBACK = 5                                    # 放量对比的近N日均量
GAP_THRESHOLD = 0.005                               # 金叉张口阈值 (EMA_FAST-EMA_MID)/EMA_MID
SCORE_ENTER = 4                                     # 进场打分门槛
PROFIT_TRAIL = 0.12                                 # 移动止盈触发浮盈
# --- 135战法参数 ---
ANT_LOOKBACK = 8                                    # 蚂蚁上树: 价格贴均线上爬的回溯天数
ANT_ABOVE_RATIO = 0.7                               # 蚂蚁上树: 近N日收盘站上EMA13的比例门槛
STICK_THRESHOLD = 0.015                             # 粘合共振: 三线最大间距/价 < 此值视为粘合
STICK_WINDOW = 10                                   # 粘合共振: 回溯N日内是否出现过粘合
STICK_BREAK = 0.005                                 # 粘合共振: 突破粘合带的缓冲幅度
# ================================================


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """日线重采样为周线"""
    d = df.set_index("date")
    w = pd.DataFrame({
        "open": d["open"].resample("W-FRI").first(),
        "high": d["high"].resample("W-FRI").max(),
        "low":  d["low"].resample("W-FRI").min(),
        "close": d["close"].resample("W-FRI").last(),
        "volume": d["volume"].resample("W-FRI").sum(),
    }).dropna().reset_index()
    return w


def resample_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """日线重采样为月线"""
    d = df.set_index("date")
    m = pd.DataFrame({
        "open": d["open"].resample("ME").first(),
        "high": d["high"].resample("ME").max(),
        "low":  d["low"].resample("ME").min(),
        "close": d["close"].resample("ME").last(),
        "volume": d["volume"].resample("ME").sum(),
    }).dropna().reset_index()
    return m


def add_emas(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for n in (EMA_FAST, EMA_MID, EMA_SLOW, EMA_FAST2, EMA_MID2, EMA_SLOW2):
        df[f"ema{n}"] = ema(df["close"], n)
    return df


def trend_state(row, fast, mid, slow):
    """判断某一周期的均线排列状态"""
    price = row["close"]
    ef, em_, es = row[f"ema{fast}"], row[f"ema{mid}"], row[f"ema{slow}"]
    if price > ef > em_ > es:
        return "多头排列"
    if price < ef < em_ < es:
        return "空头排列"
    if price > es:
        return "偏多缠绕"
    return "偏空缠绕"


def cross_recently(df, fast, mid, window=5):
    """近window根内 fast 是否上穿/下穿 mid"""
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


def detect_ant_climb(d: pd.DataFrame, lookback=ANT_LOOKBACK) -> bool:
    """蚂蚁上树(转强信号): 短均线拐头向上、张口扩大, 价格贴着EMA13连续小步上爬。
    像一串蚂蚁(小阳线)缓慢爬在均线上方。"""
    if len(d) < lookback + 4:
        return False
    last = d.iloc[-1]
    ef, em_ = last[f"ema{EMA_FAST}"], last[f"ema{EMA_MID}"]
    price = last["close"]
    # 1) 短中均线已转多(13>34)且价站上13 —— 转强初期
    short_bull = price > ef > em_
    # 2) EMA13拐头向上, 且 13-34张口比3日前扩大(均线开始向上发散)
    gap_now = ef - em_
    gap_prev = d.iloc[-4][f"ema{EMA_FAST}"] - d.iloc[-4][f"ema{EMA_MID}"]
    fan_up = ef > d.iloc[-4][f"ema{EMA_FAST}"] and gap_now > gap_prev
    # 3) 近lookback日多数收盘站上EMA13且整体向上(末>首) —— 像蚂蚁缓慢上爬
    recent = d.iloc[-lookback:]
    above = (recent["close"] >= recent[f"ema{EMA_FAST}"]).mean() >= ANT_ABOVE_RATIO
    climbing = recent["close"].iloc[-1] > recent["close"].iloc[0]
    return bool(short_bull and fan_up and above and climbing)


def detect_stick(d: pd.DataFrame, threshold=STICK_THRESHOLD, window=STICK_WINDOW):
    """粘合共振(变盘信号): 近window日内 13/34/55 三线曾高度粘合(变盘前兆),
    再看价格相对粘合带的突破方向。
    返回 (is_stick, direction, spread_pct); direction ∈ {'向上','向下','未明'}。"""
    cols = [f"ema{EMA_FAST}", f"ema{EMA_MID}", f"ema{EMA_SLOW}"]
    sub = d.iloc[-window:]
    spreads = (sub[cols].max(axis=1) - sub[cols].min(axis=1)) / sub["close"]
    min_spread = float(spreads.min())
    is_stick = min_spread < threshold          # 近期出现过粘合
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


def analyze_one(code: str, name: str, df_daily: pd.DataFrame, market_state: str) -> dict:
    """
    分析单只ETF。
    df_daily: 必须含 date(datetime), open, high, low, close, volume, 按日期升序
    market_state: 大盘环境档位 '进攻档'/'谨慎档'/'防守档'/'清仓'
    """
    if df_daily is None or len(df_daily) < 130:
        return {"code": code, "name": name, "error": "数据不足(需≥130日)"}

    df_daily = df_daily.sort_values("date").reset_index(drop=True)

    d = add_emas(df_daily)
    w = add_emas(resample_weekly(df_daily))
    m = add_emas(resample_monthly(df_daily))

    last_d = d.iloc[-1]
    last_w = w.iloc[-1]
    last_m = m.iloc[-1]
    price = last_d["close"]

    # --- 三层状态 (主判据 13/34/55) ---
    month_state = trend_state(last_m, EMA_FAST, EMA_MID, EMA_SLOW)
    week_state = trend_state(last_w, EMA_FAST, EMA_MID, EMA_SLOW)
    day_state = trend_state(last_d, EMA_FAST, EMA_MID, EMA_SLOW)
    day_cross = cross_recently(d, EMA_FAST, EMA_MID, window=5)

    # --- 量能 ---
    vol_now = last_d["volume"]
    vol_ma = df_daily["volume"].iloc[-VOL_LOOKBACK-1:-1].mean()
    is_volume_up = vol_now > vol_ma

    # --- 金叉张口 ---
    gap = (last_d[f"ema{EMA_FAST}"] - last_d[f"ema{EMA_MID}"]) / last_d[f"ema{EMA_MID}"]
    gap_ok = gap >= GAP_THRESHOLD

    # --- 回踩判断: 价在34附近(±2%)但未死叉, 且多头 ---
    near_mid = abs(price - last_d[f"ema{EMA_MID}"]) / last_d[f"ema{EMA_MID}"] < 0.02
    is_pullback = near_mid and day_state == "多头排列" and day_cross != "死叉"

    # --- 打分 ---
    score = 0
    reasons = []
    if price > last_d[f"ema{EMA_SLOW}"] and last_d[f"ema{EMA_SLOW}"] > d.iloc[-3][f"ema{EMA_SLOW}"]:
        score += 2; reasons.append("价在EMA55上方且55向上(+2)")
    if is_volume_up:
        score += 1; reasons.append("放量(+1)")
    if gap_ok:
        score += 1; reasons.append("金叉张口够大(+1)")
    if market_state == "进攻档":
        score += 1; reasons.append("大盘进攻档(+1)")
    if week_state == "多头排列":
        score += 1; reasons.append("周线多头(+1)")

    # --- 周线方向闸: 周线13在34上方才允许做 ---
    week_gate_ok = last_w[f"ema{EMA_FAST}"] > last_w[f"ema{EMA_MID}"]
    # --- 周线点头(135蚂蚁上树升级条件): 周线闸过 且 周EMA13本周仍在上行 ---
    week_nod = week_gate_ok and last_w[f"ema{EMA_FAST}"] > w.iloc[-2][f"ema{EMA_FAST}"]

    # --- 135战法信号 ---
    ant_climb = detect_ant_climb(d)
    is_stick, stick_dir, stick_spread = detect_stick(d)

    # --- 综合分类 ---
    if not week_gate_ok:
        category = "回避"
        verdict = (
            f"周线方向闸未过(周EMA13={last_w[f'ema{EMA_FAST}']:.3f}"
            f"<周EMA34={last_w[f'ema{EMA_MID}']:.3f}), 一律不做"
        )
    elif market_state in ("防守档", "清仓"):
        category = "观望"; verdict = "大盘防守, 不开新仓"
    elif is_stick:
        # 粘合共振(变盘信号): 方向没出来前一律观望, 向上才买、向下就避
        if stick_dir == "向下":
            category = "回避-向下变盘"
            verdict = f"三线粘合后向下跌破(最小间距{stick_spread}%), 向下变盘, 规避"
        elif stick_dir == "向上":
            category = "可关注-向上变盘"
            verdict = f"三线粘合后向上突破(最小间距{stick_spread}%, 打分{score}), 变盘向上进场候选"
        else:
            category = "观望-变盘待定"
            verdict = f"三线粘合(最小间距{stick_spread}%)变盘前兆, 方向未明先观望"
    elif score >= SCORE_ENTER and (day_cross == "金叉" or is_pullback):
        if is_pullback:
            category = "可关注-回踩"; verdict = f"回踩EMA34不破(打分{score}), 轻仓低吸候选"
        else:
            category = "可关注-金叉"; verdict = f"日线金叉+打分{score}, 进场候选"
    elif ant_climb:
        # 蚂蚁上树(转强信号): 周线点头才升级买入, 否则观望
        if week_nod:
            category = "可关注-蚂蚁上树"
            verdict = f"蚂蚁上树转强+周线点头(打分{score}), 进场候选"
        else:
            category = "观望-待周线点头"
            verdict = f"蚂蚁上树转强但周线未点头(打分{score}), 有仓可持/无仓观望"
    elif day_state == "多头排列":
        category = "持有/观察"; verdict = f"多头排列但无新信号(打分{score}), 持有不追"
    else:
        category = "观望"; verdict = f"信号不足(打分{score})"

    # --- 位置高低: 价格在月线区间的百分位 ---
    m_high = m["high"].max(); m_low = m["low"].min()
    pos_pct = (price - m_low) / (m_high - m_low) * 100 if m_high > m_low else 50

    return {
        "code": code, "name": name, "price": round(price, 3),
        "category": category, "score": score, "verdict": verdict,
        "month_state": month_state, "week_state": week_state,
        "day_state": day_state, "day_cross": day_cross,
        "week_gate_ok": week_gate_ok, "is_pullback": is_pullback,
        "week_nod": week_nod, "ant_climb": ant_climb,
        "is_stick": is_stick, "stick_dir": stick_dir, "stick_spread": stick_spread,
        "is_volume_up": is_volume_up, "gap_pct": round(gap*100, 2),
        "pos_pct": round(pos_pct, 1),
        "week_ema13": round(last_w[f"ema{EMA_FAST}"], 3),
        "week_ema34": round(last_w[f"ema{EMA_MID}"], 3),
        "reasons": reasons,
        "ema_day": {f"EMA{EMA_FAST}": round(last_d[f'ema{EMA_FAST}'],3),
                    f"EMA{EMA_MID}": round(last_d[f'ema{EMA_MID}'],3),
                    f"EMA{EMA_SLOW}": round(last_d[f'ema{EMA_SLOW}'],3)},
        "error": None,
    }
