"""骰子猜大小：1 个骰子（1-6），大（4/5/6）或小（1/2/3）。"""

from __future__ import annotations

import random


def roll() -> int:
    """掷骰子。"""
    return random.randint(1, 6)


def resolve(choice: str, value: int) -> bool:
    """判断是否猜中。"""
    if choice == "大":
        return value >= 4
    return value <= 3


def net_change(bet: int, win: bool, multiplier: float) -> int:
    """计算积分变化：赢按赔率净赚，输扣下注。"""
    if win:
        return int(round(bet * (multiplier - 1)))
    return -bet
