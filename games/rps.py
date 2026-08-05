"""石头剪刀布：下注与 Bot 猜拳。"""

from __future__ import annotations

import random

CHOICES = {"石头": 0, "剪刀": 1, "布": 2}


def bot_choice() -> str:
    """Bot 随机出拳。"""
    return random.choice(list(CHOICES))


def resolve(player: str, bot: str) -> str:
    """返回 win / draw / lose。"""
    p, b = CHOICES[player], CHOICES[bot]
    if p == b:
        return "draw"
    if (p - b) % 3 == 2:
        return "win"
    return "lose"


def net_change(bet: int, result: str, multiplier: float) -> int:
    """计算积分变化：赢净赚、平局返还、输扣下注。"""
    if result == "win":
        return int(round(bet * (multiplier - 1)))
    if result == "lose":
        return -bet
    return 0
