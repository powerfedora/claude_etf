"""组合持仓视角下的信号展示: 区分「扫描开仓信号」与「已有仓位操作建议」。"""


def is_exit_signal(scan_category: str, stop_breach: bool = False) -> bool:
    if stop_breach:
        return True
    if not scan_category:
        return False
    return scan_category.startswith("回避")


def adjust_signal_for_holding(scan_category, scan_verdict, filled, target_amt, stop_breach=False):
    """
    有仓位时把扫描信号转为持仓建议。
    返回 (display_category, display_verdict, scan_category)。
    """
    scan_category = scan_category or "—"
    scan_verdict = scan_verdict or ""

    if filled <= 0:
        return scan_category, scan_verdict, scan_category

    if is_exit_signal(scan_category, stop_breach):
        if stop_breach and not scan_category.startswith("回避"):
            cat = "持有/止损警戒"
            verdict = f"⚠已跌破EMA34, 考虑减仓"
            if scan_verdict:
                verdict += f"; 扫描: {scan_verdict}"
            return cat, verdict, scan_category
        return scan_category, scan_verdict, scan_category

    target = target_amt or 0
    remaining = max(0, target - filled) if target else 0

    if scan_category.startswith("可关注") and remaining > 0:
        display_cat = "持有/可加仓"
        display_verdict = f"已建仓, 扫描仍「{scan_category}」, 余 {remaining / 10000:g} 万可补"
    elif scan_category.startswith("可关注"):
        display_cat = "持有/已满仓"
        display_verdict = f"已达标, 扫描「{scan_category}」→ 持有不追"
    else:
        display_cat = "持有/已建仓"
        display_verdict = f"已建仓, 扫描「{scan_category}」→ 持有不加仓"

    if scan_verdict:
        display_verdict += f" ({scan_verdict})"

    return display_cat, display_verdict, scan_category
