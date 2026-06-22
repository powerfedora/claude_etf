"""
ETF 多周期均线扫描 - 主程序
============================
用法:
  1. pip install pandas numpy openpyxl requests
  2. 配置 Tushare MCP (见 tushare_mcp.json 或环境变量 TUSHARE_MCP_URL)
  3. 把ETF代码写进 etf_list.txt (一行一个6位代码, 如 562500)
  4. python main.py
  5. 打开生成的 report_YYYYMMDD.html

数据源: Tushare Pro MCP (fund_daily / index_daily)
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from engine import analyze_one, add_emas, EMA_FAST, EMA_MID, EMA_SLOW
from report import build_reports
from tushare_client import TushareMcpClient, TushareMcpError

ROOT = Path(__file__).resolve().parent
LIST_FILE = ROOT / "etf_list.txt"
START_DATE = "20240101"

# Tushare 有频次限制; 触发限流时 client 会自动等待 ~65s 重试
REQUEST_INTERVAL = 1.2
COOLDOWN_AFTER_FAIL = 3.0


def fetch_etf_daily(client: TushareMcpClient, code: str) -> pd.DataFrame:
    """抓单只 ETF 日线, 返回 date/open/high/low/close/volume。"""
    end_date = datetime.now().strftime("%Y%m%d")
    df = client.fetch_fund_daily(code, START_DATE, end_date)
    if df is None or df.empty:
        return None
    return df


def get_market_state(client: TushareMcpClient) -> str:
    """抓沪深300, 用 EMA55 判断大盘档位。"""
    try:
        end_date = datetime.now().strftime("%Y%m%d")
        idx = client.fetch_index_daily("000300.SH", START_DATE, end_date)
        if idx is None or idx.empty:
            raise ValueError("沪深300数据为空")
        idx = add_emas(idx)
        last = idx.iloc[-1]
        p = last["close"]
        ef = last[f"ema{EMA_FAST}"]
        em_ = last[f"ema{EMA_MID}"]
        es = last[f"ema{EMA_SLOW}"]
        es_rising = last[f"ema{EMA_SLOW}"] > idx.iloc[-5][f"ema{EMA_SLOW}"]
        if p > ef > em_ > es:
            return "进攻档"
        if p > es and es_rising:
            return "谨慎档"
        if p < es:
            return "防守档"
        return "谨慎档"
    except Exception as e:
        print(f"[警告] 大盘判断失败({e}), 默认谨慎档")
        return "谨慎档"


def load_list():
    try:
        with open(LIST_FILE, encoding="utf-8") as f:
            codes = []
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.replace(",", " ").split()
                code = parts[0].zfill(6)
                name = parts[1] if len(parts) > 1 else code
                codes.append((code, name))
        return codes
    except FileNotFoundError:
        print(f"找不到 {LIST_FILE}, 请创建它, 一行一个ETF代码(可选名称)。")
        sys.exit(1)


def main():
    try:
        client = TushareMcpClient()
    except TushareMcpError as e:
        print(f"配置错误: {e}")
        sys.exit(1)

    codes = load_list()
    print(f"共 {len(codes)} 只ETF, 数据源: Tushare MCP")
    print("判断大盘环境...")
    market = get_market_state(client)
    print(f"大盘环境: {market}\n")

    results = []
    failed = []
    for i, (code, name) in enumerate(codes, 1):
        try:
            df = fetch_etf_daily(client, code)
            r = analyze_one(code, name, df, market)
            results.append(r)
            tag = r.get("category", r.get("error", "?"))
            print(f"  [{i}/{len(codes)}] {code} {name}  -> {tag}")
        except Exception as e:
            failed.append((i, code, name, str(e)))
            print(f"  [{i}/{len(codes)}] {code} {name}  -> 失败: {e}")
            if "频率超限" in str(e) or "40203" in str(e):
                print(f"         等待 {COOLDOWN_AFTER_FAIL:.0f}s 后继续...")
                time.sleep(COOLDOWN_AFTER_FAIL)
        time.sleep(REQUEST_INTERVAL)

    if failed:
        print(f"\n对 {len(failed)} 只失败标的二次补抓...")
        for i, code, name, _ in failed:
            try:
                time.sleep(REQUEST_INTERVAL * 3)
                df = fetch_etf_daily(client, code)
                r = analyze_one(code, name, df, market)
                results = [x for x in results if x.get("code") != code]
                results.append(r)
                tag = r.get("category", r.get("error", "?"))
                print(f"  [{i}/{len(codes)}] {code} {name}  -> 补抓成功: {tag}")
            except Exception as e:
                results.append({"code": code, "name": name, "error": str(e)})
                print(f"  [{i}/{len(codes)}] {code} {name}  -> 补抓仍失败: {e}")

    # 保存完整结果供 Web 应用使用
    def _default(o):
        if hasattr(o, 'item'):
            return o.item()
        if hasattr(o, 'isoformat'):
            return o.isoformat()
        return str(o)
    scan_data = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "market": market, "results": results}
    (ROOT / "last_scan.json").write_text(
        json.dumps(scan_data, ensure_ascii=False, indent=2, default=_default), encoding="utf-8")

    stamp = datetime.now().strftime("%Y%m%d")
    out_html = ROOT / f"report_{stamp}.html"
    # 先记快照(让本次运行也进入报告的历史时间线), 再出报告
    snap_ts = None
    try:
        from history import record_snapshot
        snap_ts = record_snapshot(results, market)
    except Exception as e:
        print(f"[快照失败] {e}")

    n, nf, ne = build_reports(results, market, out_html)
    print(f"\n完成! 有效{n}只, 可关注{nf}只, 失败{ne}只")
    print(f"报告: {out_html}")
    if snap_ts:
        print(f"已存快照: {snap_ts}")

    try:
        from timeline import build_timeline
        tl_path = build_timeline()
        print(f"时间线: {tl_path}")
    except Exception as e:
        print(f"[时间线生成失败] {e}")

    # 自动发布到公开 GitHub Pages 仓库 (见 push.py); 未配置或失败都不影响扫描结果
    try:
        from push import publish_latest
        publish_latest()
    except SystemExit as e:
        print(f"[发布跳过] {e}")
    except Exception as e:
        print(f"[发布失败] {e}")


if __name__ == "__main__":
    main()
