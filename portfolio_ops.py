"""
portfolio_ops.py - 成交导入后更新 portfolio.json 与 trades 流水
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

from history import PORTFOLIO_FILE, TRADES_FILE, _append, log_trade, load_trades

ROOT = Path(__file__).resolve().parent
IMPORT_DIR = ROOT / "history" / "imports"
IMPORTS_FILE = ROOT / "history" / "trade_imports.jsonl"


def _load_portfolio():
    if not PORTFOLIO_FILE.exists():
        return {"capital": 0, "positions": []}
    return json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))


def _save_portfolio(pf):
    PORTFOLIO_FILE.write_text(json.dumps(pf, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_position(pf, code):
    for p in pf.get("positions", []):
        if p["code"] == code.zfill(6):
            return p
    return None


def _fmt_wan(n):
    return f"{n / 10000:g}万"


def _auto_status_after_buy(pos):
    filled = pos.get("filled", 0) or 0
    target = pos.get("target_amt", 0) or 0
    remaining = max(0, target - filled)
    if remaining > 0:
        return f"已持有·已建仓{_fmt_wan(filled)},余{_fmt_wan(remaining)}回踩加仓"
    if target:
        return f"已持有·已达目标{_fmt_wan(target)}"
    return f"已持有·已建仓{_fmt_wan(filled)}"


def _auto_status_after_sell(pos):
    filled = pos.get("filled", 0) or 0
    if filled <= 0:
        return "已清仓·观望"
    target = pos.get("target_amt", 0) or 0
    remaining = max(0, target - filled)
    if remaining > 0:
        return f"已持有·已减仓至{_fmt_wan(filled)},余{_fmt_wan(remaining)}可补"
    return f"已持有·剩余{_fmt_wan(filled)}"


def _trade_fingerprint(t):
    return "|".join([
        t.get("trade_time", ""),
        t.get("code", ""),
        str(t.get("amount", "")),
        str(t.get("price", "")),
        str(t.get("qty", "")),
    ])


def _existing_fingerprints():
    fps = set()
    for t in load_trades():
        fps.add("|".join([
            t.get("ts", ""),
            t.get("code", ""),
            str(t.get("amount", "")),
            str(t.get("price", "")),
            str(t.get("qty", "")),
        ]))
    return fps


def save_import_image(file_bytes, suffix=".png"):
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    import_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    path = IMPORT_DIR / f"{import_id}{suffix}"
    path.write_bytes(file_bytes)
    return import_id, path


def _normalize_trade(t):
    price = float(t.get("price") or 0)
    amount = float(t.get("amount") or 0)
    qty = int(t.get("qty") or 0)
    if price <= 0 and qty > 0 and amount > 0:
        price = round(amount / qty, 4)
        t["price"] = price
    if amount <= 0 and price > 0 and qty > 0:
        amount = round(price * qty, 2)
        t["amount"] = amount
    if price <= 0:
        raise ValueError(f"无法确定成交价 (金额={amount}, 数量={qty})")
    return t


def apply_ocr_trades(trades, import_id=None, image_name=None):
    """将 OCR 成交写入 trades 流水并更新 portfolio 仓位。"""
    pf = _load_portfolio()
    existing = _existing_fingerprints()
    applied = []
    skipped = []
    errors = []

    for t in trades:
        try:
            t = _normalize_trade(dict(t))
        except Exception as e:
            errors.append(f"{t.get('code', '?')} {t.get('trade_time', '')}: {e}")
            continue
        fp = _trade_fingerprint(t)
        if fp in existing:
            skipped.append(t)
            continue
        code = t["code"].zfill(6)
        try:
            pos = _find_position(pf, code)
            pf_name = pos.get("name") if pos else t.get("name", code)
            display_name = pf_name or t.get("name", code)
            t["name"] = display_name
            rec = log_trade(
                code,
                t["action"],
                t["amount"],
                t["price"],
                name=pf_name or t.get("name"),
                note=f"截图导入 {image_name or ''}".strip(),
                trade_time=t.get("trade_time"),
                qty=t.get("qty", 0),
                direction=t.get("direction", ""),
                import_id=import_id,
            )
            existing.add(fp)
            pf = _load_portfolio()
            pos = _find_position(pf, code)
            if pos:
                if t["action"] == "buy":
                    fb = pos.get("first_buy", 0) or 0
                    if fb > 0 and t["amount"] >= fb * 0.9:
                        pos["first_buy"] = 0
                    pos["status"] = _auto_status_after_buy(pos)
                else:
                    pos["status"] = _auto_status_after_sell(pos)
                if t.get("name") and not pos.get("name"):
                    pos["name"] = t["name"]
                pf["as_of"] = t.get("date") or datetime.now().strftime("%Y-%m-%d")
                _save_portfolio(pf)
            applied.append({**t, **rec})
        except Exception as e:
            errors.append(f"{code} {t.get('trade_time')}: {e}")

    if import_id and (applied or trades):
        _append(IMPORTS_FILE, {
            "import_id": import_id,
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "image": image_name,
            "trade_count": len(trades),
            "applied_count": len(applied),
            "skipped_count": len(skipped),
            "trades": trades,
        })

    pf = _load_portfolio()
    filled = sum(p.get("filled", 0) or 0 for p in pf.get("positions", []))
    cap = pf.get("capital", 0) or 0
    return {
        "import_id": import_id,
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "parsed_count": len(trades),
        "summary": {
            "capital": cap,
            "filled": filled,
            "cash": cap - filled,
            "as_of": pf.get("as_of", ""),
        },
    }


def list_all_trades(limit=200):
    rows = load_trades()
    out = []
    for t in reversed(rows[-limit:]):
        action = t.get("action", "buy")
        out.append({
            "ts": t.get("ts", ""),
            "date": (t.get("ts") or "")[:10],
            "time": (t.get("ts") or "")[11:19] if len(t.get("ts") or "") > 10 else "",
            "name": t.get("name", ""),
            "code": t.get("code", ""),
            "price": t.get("price", 0),
            "qty": t.get("qty", 0),
            "direction": t.get("direction") or ("证券买入" if action == "buy" else "证券卖出"),
            "action": action,
            "amount": t.get("amount", 0),
            "after_filled": t.get("after_filled", 0),
            "after_cost": t.get("after_cost", 0),
            "import_id": t.get("import_id", ""),
            "note": t.get("note", ""),
        })
    return out
