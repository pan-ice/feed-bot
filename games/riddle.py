"""猜谜语：AI 生成谜语，用户限次猜测。"""

from __future__ import annotations

import random
import re
import time

# LLM 不可用时的兜底谜语池
FALLBACK_RIDDLES: list[tuple[str, str]] = [
    ("千条线，万条线，掉到水里看不见。", "雨"),
    ("麻屋子，红帐子，里面住个白胖子。", "花生"),
    ("弯弯的月儿小小的船，小小的船儿两头尖。", "月亮"),
    ("身穿绿衣裳，肚里水汪汪，生的子儿多，个个黑脸膛。", "西瓜"),
    ("五个兄弟，住在一起，名字不同，高矮不齐。", "手指"),
    ("上不怕水，下不怕火，家家厨房，都有一个。", "锅"),
    ("像糖不是糖，有圆也有方，帮你改错字，自己不怕脏。", "橡皮"),
    ("有头没有颈，身上冷冰冰，有翅不能飞，无脚也能行。", "鱼"),
    ("兄弟七八个，围着柱子坐，大家一分家，衣服就扯破。", "蒜"),
    ("白娃娃，爬黑墙，越爬个儿越变小，再也没法往上长。", "粉笔"),
]

_PUNCT_RE = re.compile(r"[\s，。！？、,.!?；;：:·\-\—]+")


def normalize(text: str) -> str:
    """去除空白与标点，统一用于答案比对。"""
    return _PUNCT_RE.sub("", str(text or "")).strip()


def check_answer(answer: str, guess: str) -> bool:
    """判断猜测是否命中答案（完全一致或互为包含）。"""
    answer_norm = normalize(answer)
    guess_norm = normalize(guess)
    if not answer_norm or not guess_norm:
        return False
    return answer_norm == guess_norm or answer_norm in guess_norm or guess_norm in answer_norm


def new_session(riddle_text: str, answer: str, max_tries: int) -> dict:
    """创建新一局猜谜语。"""
    return {
        "riddle_text": riddle_text,
        "answer": answer,
        "tries_left": max_tries,
        "started_at": time.time(),
    }


def is_riddle_session(state: dict) -> bool:
    """判断会话是否为猜谜语会话（防止与其他游戏会话混淆）。"""
    return isinstance(state, dict) and "answer" in state and "tries_left" in state

def is_expired(state: dict, timeout_seconds: int = 300) -> bool:
    """会话是否超时。"""
    return time.time() - state.get("started_at", 0) > timeout_seconds


def process_guess(state: dict, guess: str) -> tuple[str, str]:
    """处理一次猜测，返回 (状态, 提示文本)。状态：win / over / wrong。"""
    state["tries_left"] -= 1
    if check_answer(state["answer"], guess):
        return "win", f"答对了！答案是「{state['answer']}」"
    if state["tries_left"] <= 0:
        return "over", f"机会用完了，答案是「{state['answer']}」"
    return "wrong", f"不对哦，还剩 {state['tries_left']} 次机会"


def parse_llm_result(text: str) -> tuple[str, str]:
    """从 LLM 输出中解析谜面与答案，解析失败返回空串。"""
    riddle_text = ""
    answer = ""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line.startswith("谜面") and ("：" in line or ":" in line):
            riddle_text = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        if line.startswith("答案") and ("：" in line or ":" in line):
            answer = line.split("：", 1)[-1].split(":", 1)[-1].strip()
    return riddle_text, answer


def random_fallback() -> tuple[str, str]:
    """从兜底谜语池随机取一条。"""
    return random.choice(FALLBACK_RIDDLES)
