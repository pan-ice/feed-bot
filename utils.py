"""投喂插件 — 工具函数与常量。"""

from __future__ import annotations

import os
import unicodedata
from contextlib import contextmanager
from typing import Any

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "feed_bot.db")

# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


@contextmanager
def atomic_write(path: str, mode: str = "w", encoding: str | None = None):
    """原子写入：先写临时文件，成功后 os.replace 替换。"""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, mode, encoding=encoding) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def is_likely_emoji(text: str) -> bool:
    """判断文本是否可能是单个 emoji 或 emoji 序列。"""
    if not text or text.isascii():
        return False
    # 去除变体选择符和零宽连接符后检查
    stripped = text
    for ch in ("️", "‍", "︎"):  # VS-16, ZWJ, VS-15
        stripped = stripped.replace(ch, "")
    if not stripped:
        return True  # 纯修饰符序列，视为 emoji
    # 所有剩余字符都是 Symbol/Other 或 Modifier Symbol
    return all(unicodedata.category(c) in ("So", "Sk") for c in stripped)


def extract_nickname(message: dict[str, Any] | None) -> str:
    """从消息字典中提取发送者昵称。"""
    if not message or not isinstance(message, dict):
        return ""
    message_info = message.get("message_info", {})
    if isinstance(message_info, dict):
        user_info = message_info.get("user_info", {})
        if isinstance(user_info, dict):
            cardname = str(user_info.get("user_cardname") or "").strip()
            if cardname:
                return cardname
            nickname = str(user_info.get("user_nickname") or "").strip()
            if nickname:
                return nickname
    return ""
