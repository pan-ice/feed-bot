"""猜数字：Bot 随机 1-100，用户限次猜测。"""

from __future__ import annotations

import random
import time


def new_game(max_tries: int) -> dict:
    """创建新一局猜数字。"""
    return {
        "number": random.randint(1, 100),
        "tries_left": max_tries,
        "started_at": time.time(),
    }


def is_expired(state: dict, timeout_seconds: int = 300) -> bool:
    """会话是否超时。"""
    return time.time() - state.get("started_at", 0) > timeout_seconds


def guess(state: dict, num: int) -> tuple[str, str]:
    """处理一次猜测，返回 (状态, 提示文本)。

    状态：win / over / low / high
    """
    state["tries_left"] -= 1
    if num == state["number"]:
        return "win", f"猜中了！答案是 {state['number']}"
    if state["tries_left"] <= 0:
        return "over", f"次数用完了，答案是 {state['number']}"
    if num < state["number"]:
        return "low", f"小了，还剩 {state['tries_left']} 次"
    return "high", f"大了，还剩 {state['tries_left']} 次"
