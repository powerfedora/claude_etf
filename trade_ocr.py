"""
trade_ocr.py - 从券商「当日成交」截图 OCR 识别并解析成交记录
"""
import re
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMPORT_DIR = ROOT / "history" / "imports"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
CODE_RE = re.compile(r"^\d{6}$")
PRICE_RE = re.compile(r"^\d+\.\d{2,4}$")
QTY_RE = re.compile(r"^\d{1,7}$")
AMOUNT_RE = re.compile(r"^\d+\.\d{2,4}$")
BUY_KW = re.compile(r"买")
SELL_KW = re.compile(r"卖")


def _ocr_image(image_path):
    """OCR 返回 [(top, left, text, conf), ...]"""
    from PIL import Image
    import pytesseract

    img = Image.open(image_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    if w < 800:
        scale = 800 / w
        img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

    langs = ["chi_sim+eng", "chi_sim", "eng"]
    data = None
    last_err = None
    for lang in langs:
        try:
            data = pytesseract.image_to_data(
                img, lang=lang, output_type=pytesseract.Output.DICT,
                config="--psm 6",
            )
            break
        except Exception as e:
            last_err = e
    if data is None:
        raise RuntimeError(
            f"OCR 失败: {last_err}. 如缺中文包请运行: brew install tesseract-lang"
        )

    items = []
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        if not text:
            continue
        conf = int(data["conf"][i]) if str(data["conf"][i]).isdigit() else -1
        if conf != -1 and conf < 25:
            continue
        items.append((data["top"][i], data["left"][i], text, conf))
    return items


def _merge_name_parts(parts):
    name = "".join(t for _, t in sorted(parts, key=lambda x: x[0])).strip()
    name = re.sub(r"\s+", "", name)
    return name


def parse_trades_from_ocr_items(items):
    """按截图列布局解析: 时间 | 名称代码 | 价格数量 | 方向金额"""
    if not items:
        return []

    anchors = sorted(
        [(y, x, t) for y, x, t, _ in items if DATE_RE.match(t)],
        key=lambda z: z[0],
    )
    if not anchors:
        return parse_trades_from_lines([t for _, _, t, _ in sorted(items, key=lambda z: (z[0], z[1]))])

    trades = []
    for i, (dy, _, date) in enumerate(anchors):
        y_end = anchors[i + 1][0] - 5 if i + 1 < len(anchors) else dy + 120
        band = [(y, x, t) for y, x, t, _ in items if dy - 5 <= y <= y_end]

        time = next((t for y, x, t in band if TIME_RE.match(t)), "00:00:00")
        code = next((t for y, x, t in band if CODE_RE.match(t)), "")
        if not code:
            continue

        name_row = [(x, t) for y, x, t in band if 300 < x < 580 and y < dy + 35
                    and not DATE_RE.match(t) and not TIME_RE.match(t)]
        code_row_name = [(x, t) for y, x, t in band if 300 < x < 580 and y >= dy + 35
                         and not CODE_RE.match(t)]
        name = _merge_name_parts(name_row) or _merge_name_parts(code_row_name)

        prices = [(y, x, t) for y, x, t in band if PRICE_RE.match(t) and 580 < x < 760]
        qtys = [(y, x, t) for y, x, t in band if QTY_RE.match(t) and 580 < x < 760 and t != code]
        price = float(prices[0][2]) if prices else 0.0
        qty = int(qtys[0][2]) if qtys else 0

        amounts = [(y, x, t) for y, x, t in band if AMOUNT_RE.match(t) and x >= 760]
        amount = float(amounts[-1][2]) if amounts else round(price * qty, 2)

        dir_texts = [(y, x, t) for y, x, t in band if x >= 760 and y < dy + 40
                     and not AMOUNT_RE.match(t) and len(t) >= 2]
        direction = dir_texts[0][2] if dir_texts else ""
        if SELL_KW.search(direction):
            action = "sell"
            direction = direction or "证券卖出"
        else:
            action = "buy"
            direction = direction if BUY_KW.search(direction) else "证券买入"

        if amount <= 0 and price and qty:
            amount = round(price * qty, 2)

        if price <= 0 and qty > 0 and amount > 0:
            price = round(amount / qty, 4)

        if price <= 0 or qty <= 0 or amount <= 0:
            continue

        trades.append({
            "trade_time": f"{date} {time}",
            "date": date,
            "time": time,
            "name": name or code,
            "code": code,
            "price": price,
            "qty": qty,
            "amount": round(amount, 2),
            "direction": direction,
            "action": action,
        })
    return trades


def parse_trades_from_lines(lines):
    """纯文本行兜底解析 (OCR 按列输出时)。"""
    trades = []
    i = 0
    while i < len(lines):
        if DATE_RE.match(lines[i]):
            try:
                date, time = lines[i], lines[i + 1]
                name, code = lines[i + 2], lines[i + 3]
                price, qty = float(lines[i + 4]), int(float(lines[i + 5]))
                direction = lines[i + 6]
                amount = float(lines[i + 7])
                action = "sell" if SELL_KW.search(direction) else "buy"
                trades.append({
                    "trade_time": f"{date} {time}",
                    "date": date, "time": time,
                    "name": name, "code": code,
                    "price": price, "qty": qty,
                    "amount": round(amount, 2),
                    "direction": direction,
                    "action": action,
                })
                i += 8
                continue
            except (IndexError, ValueError):
                pass
        i += 1
    return trades


def ocr_and_parse(image_path):
    items = _ocr_image(image_path)
    trades = parse_trades_from_ocr_items(items)
    raw_lines = [t for _, _, t, _ in sorted(items, key=lambda z: (z[0], z[1]))]
    return trades, raw_lines
