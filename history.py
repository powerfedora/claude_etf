"""
history.py - 历史记录: 运行快照 + 操作流水, 可追溯每个时间点的状态
==================================================================
存储(均为 JSONL, 一行一条, 追加写, 不进 Git):
  history/snapshots.jsonl  每次跑 main.py 自动追加一条运行快照
  history/trades.jsonl     你的每笔买入/卖出操作

命令行查询/记录:
  python history.py runs                      # 列出最近运行快照(时间线)
  python history.py code 159713               # 某只ETF在各时间点的价格/分类变化
  python history.py trades                     # 列出所有操作流水
  python history.py buy  159713 60000 1.558 --note 首笔   # 记一笔买入(金额/成交价)
  python history.py sell 159713 30000 1.62              # 记一笔卖出
说明: buy/sell 会同时更新 portfolio.json 的 filled(已建仓)与 cost(成交均价)。
"""
import json
import sys
import argparse
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIST_DIR = ROOT / "history"
SNAP_FILE = HIST_DIR / "snapshots.jsonl"
TRADES_FILE = HIST_DIR / "trades.jsonl"
PORTFOLIO_FILE = ROOT / "portfolio.json"


def _append(path: Path, rec: dict):
    HIST_DIR.mkdir(exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _load(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def load_snapshots():
    return _load(SNAP_FILE)


def load_trades():
    return _load(TRADES_FILE)


def record_snapshot(results, market_state):
    """每次扫描后调用: 把全部ETF关键字段 + 组合当时状态存成一条快照。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    etfs = []
    for r in results:
        if r.get("error"):
            continue
        etfs.append({
            "code": r["code"], "name": r["name"], "price": r["price"],
            "cat": r["category"], "score": r["score"], "pos": r["pos_pct"],
            "ema34": (r.get("ema_day") or {}).get("EMA34"),
            "verdict": r.get("verdict", ""),
            "reasons": r.get("reasons", []),
            "month_state": r.get("month_state", ""),
            "week_state": r.get("week_state", ""),
            "day_state": r.get("day_state", ""),
            "day_cross": r.get("day_cross", ""),
        })
    pf_state = None
    if PORTFOLIO_FILE.exists():
        try:
            pf = json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
            filled = sum(p.get("filled", 0) for p in pf.get("positions", []))
            cap = pf.get("capital", 0)
            pf_state = {
                "capital": cap, "filled": filled, "cash": cap - filled,
                "positions": [{"code": p["code"], "filled": p.get("filled", 0),
                               "cost": p.get("cost", 0)} for p in pf.get("positions", [])],
            }
        except Exception:
            pass
    _append(SNAP_FILE, {"ts": ts, "market": market_state, "etfs": etfs, "portfolio": pf_state})
    return ts


def log_trade(code, action, amount, price, name=None, note=""):
    """记一笔操作并同步更新 portfolio.json。action: buy/sell。amount=金额(元), price=成交价。"""
    amount = float(amount); price = float(price)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    pf = json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8")) if PORTFOLIO_FILE.exists() \
        else {"capital": 0, "positions": []}
    pos = next((p for p in pf.get("positions", []) if p["code"] == code), None)
    if pos is None:
        pos = {"code": code, "name": name or code, "target_pct": 0, "target_amt": 0,
               "first_buy": 0, "filled": 0, "cost": 0, "status": ""}
        pf.setdefault("positions", []).append(pos)

    old_filled = pos.get("filled", 0) or 0
    old_cost = pos.get("cost", 0) or 0
    if action == "buy":
        old_shares = (old_filled / old_cost) if old_cost else 0
        new_shares = old_shares + amount / price
        new_filled = old_filled + amount
        pos["cost"] = round(new_filled / new_shares, 4) if new_shares else price
        pos["filled"] = round(new_filled)
    elif action == "sell":
        new_filled = max(0, old_filled - amount)
        pos["filled"] = round(new_filled)
        if new_filled == 0:
            pos["cost"] = 0
    else:
        sys.exit(f"未知操作: {action} (应为 buy 或 sell)")

    pf["as_of"] = ts[:10]
    PORTFOLIO_FILE.write_text(json.dumps(pf, ensure_ascii=False, indent=2), encoding="utf-8")

    rec = {"ts": ts, "code": code, "name": pos.get("name"), "action": action,
           "amount": amount, "price": price, "note": note,
           "after_filled": pos["filled"], "after_cost": pos.get("cost", 0)}
    _append(TRADES_FILE, rec)
    return rec


# ---------------- 命令行 ----------------
def _cli():
    ap = argparse.ArgumentParser(description="ETF 历史记录查询/记录")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("runs", help="最近运行快照时间线")
    pc = sub.add_parser("code", help="某ETF各时间点变化"); pc.add_argument("code")
    sub.add_parser("trades", help="所有操作流水")
    for act in ("buy", "sell"):
        pa = sub.add_parser(act, help=f"记一笔{act}")
        pa.add_argument("code"); pa.add_argument("amount", type=float)
        pa.add_argument("price", type=float)
        pa.add_argument("--name", default=None); pa.add_argument("--note", default="")

    a = ap.parse_args()
    if a.cmd == "runs":
        for s in load_snapshots()[-20:]:
            pf = s.get("portfolio") or {}
            focus = sum(1 for e in s["etfs"] if e["cat"].startswith("可关注"))
            print(f"{s['ts']} | {s['market']} | 可关注{focus}只 | "
                  f"已投{pf.get('filled',0)/10000:g}万 现金{pf.get('cash',0)/10000:g}万")
    elif a.cmd == "code":
        for s in load_snapshots():
            e = next((x for x in s["etfs"] if x["code"] == a.code), None)
            if e:
                print(f"{s['ts']} | {e['name']} {e['price']} | {e['cat']} | "
                      f"打分{e['score']} 位置{e['pos']}% EMA34={e['ema34']}")
    elif a.cmd == "trades":
        for t in load_trades():
            print(f"{t['ts']} | {t['action']:<4} {t.get('name','')}({t['code']}) | "
                  f"{t['amount']/10000:g}万 @ {t['price']} | {t.get('note','')}")
    elif a.cmd in ("buy", "sell"):
        r = log_trade(a.code, a.cmd, a.amount, a.price, a.name, a.note)
        print(f"已记录: {r['ts']} {a.cmd} {a.code} {a.amount/10000:g}万 @ {a.price} "
              f"-> 已建仓{r['after_filled']/10000:g}万 均价{r['after_cost']}")
    else:
        ap.print_help()


if __name__ == "__main__":
    _cli()
