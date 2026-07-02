"""投喂插件 — 定时任务与 LLM 回复 Mixin。"""

from __future__ import annotations

import asyncio
import random
import re
import time
from datetime import datetime, time as dt_time
from typing import Any

from .config import FeedBotConfig
from .db import AsyncDatabase

# 匹配可能干扰 LLM 指令的常见注入模式（best-effort，非安全边界）
# 覆盖中英文的「忽略/无视/不要遵守 + 之前/以上/所有 + 指令/规则/提示」组合
_PROMPT_INJECTION_RE = re.compile(
    r"(忽略|无视|不要(?:遵守|理会|管)|disregard|ignore|do\s+not\s+follow)\s*"
    r"(以上|上述|之前的|所有|一切|previous|above|prior|all|every)\s*"
    r"(指令|指示|规则|提示|约束|instructions?|rules?|prompts?|constraints?)",
    re.IGNORECASE,
)
# 移除控制字符（保留换行和 tab，它们已在 prompt 结构中使用）
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_prompt_input(text: str, max_len: int = 200) -> str:
    """清洗用户可控的 prompt 输入，防止注入和异常字符。

    注意：这是 best-effort 防御，不是安全边界。LLM 在 SDK 管道内闭环运行，
    用户可控字段对 LLM 行为的影响有限。如需更强隔离，应在 SDK 层面实现。
    """
    if not text:
        return ""
    # 移除控制字符
    text = _CONTROL_CHAR_RE.sub("", text)
    # 截断过长输入
    if len(text) > max_len:
        text = text[:max_len] + "…"
    # 将注入模式替换为占位符
    text = _PROMPT_INJECTION_RE.sub("[已过滤]", text)
    return text


class LoopTasksMixin:
    """定时任务：饱食度衰减循环、求投喂循环、LLM 回复生成。"""

    # 这些属性由 FeedBotPlugin 提供，类型标注仅供静态分析
    db: AsyncDatabase
    config: FeedBotConfig
    _running: bool

    # ---- 安静时段判断 ----

    @staticmethod
    def _in_quiet_hours(quiet_hours: str, now: datetime | None = None) -> bool:
        """判断当前时间是否在安静时段内。

        quiet_hours 格式 'HH:MM-HH:MM'（24小时制），留空返回 False。
        支持跨午夜时段，如 '23:00-08:00'。
        """
        spec = (quiet_hours or "").strip()
        if not spec:
            return False

        m = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", spec)
        if not m:
            return False

        sh, sm, eh, em = (int(g) for g in m.groups())
        start = dt_time(sh, sm)
        end = dt_time(eh, em)
        current = (now or datetime.now()).time()

        if start == end:
            return False
        if start < end:
            # 同一天内，如 00:00-06:00
            return start <= current < end
        # 跨午夜，如 23:00-08:00
        return current >= start or current < end

    # ---- 定时任务 ----

    async def _attr_decay_loop(self) -> None:
        """定期应用基于时间的饱食度衰减（批量 SQL）。"""
        while self._running:
            try:
                if not self._running or not self.db.is_open:
                    break

                # 批量衰减所有群饱食度
                await self.db.apply_satiety_decay_batch(self.config.bot_attr.satiety_decay_rate)

                self.ctx.logger.debug("饱食度衰减检查完成")
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.ctx.logger.error(f"属性衰减任务异常: {e}")
                await asyncio.sleep(60)

    async def _seek_feed_loop(self) -> None:
        """定期检查饱食度并触发求投喂。"""
        while self._running:
            try:
                if not self._running or not self.db.is_open:
                    break

                threshold = self.config.bot_attr.seek_feed_threshold
                cooldown = self.config.bot_attr.seek_feed_cooldown

                try:
                    group_streams = await self.ctx.chat.get_group_streams()
                    if group_streams:
                        for stream in group_streams:
                            if not isinstance(stream, dict):
                                continue

                            stream_group_id = str(stream.get("group_id", ""))
                            stream_session_id = str(stream.get("session_id", ""))

                            if not stream_group_id or not stream_session_id:
                                continue
                            if not self._is_group_enabled(stream_group_id):
                                continue

                            satiety = await self.db.get_satiety(stream_group_id, self.config)
                            if satiety >= threshold:
                                continue

                            # 检查冷却时间
                            row = await self.db.fetchone(
                                "SELECT last_seek_feed_time FROM feed_groups WHERE group_id = ?",
                                (stream_group_id,),
                            )
                            last_seek_time = row[0] if row else 0
                            if time.time() - last_seek_time < cooldown:
                                continue

                            # 安静时段内不发送求投喂消息（不打扰用户休息）
                            if self._in_quiet_hours(self.config.bot_attr.quiet_hours):
                                continue

                            # 从自定义消息列表中随机选择一条
                            messages = self.config.bot_attr.seek_feed_messages
                            seek_msg = random.choice(messages) if messages else "好饿...有人投喂我吗？🥺"

                            # 如果开启了 LLM，尝试生成更自然的求喂消息
                            if self.config.llm.enabled:
                                try:
                                    seek_prompt_parts = [
                                        f"你是一个可爱的聊天机器人，当前饱食度{satiety:.0f}/100，很饿。",
                                        "请用1-2句简短的语气向群友们撒娇求投喂，可以包含emoji。",
                                        "不要重复之前的求喂方式，要多样化。严格遵循你的表达风格。",
                                    ]
                                    try:
                                        personality = await self.ctx.config.get("personality.personality", "")
                                        reply_style = await self.ctx.config.get("personality.reply_style", "")
                                        if personality:
                                            seek_prompt_parts.append(f"你的人设：{personality}")
                                        if reply_style:
                                            seek_prompt_parts.append(f"你的表达风格：{reply_style}")
                                    except Exception:
                                        pass
                                    llm_result = await self.ctx.llm.generate(
                                        prompt="\n".join(seek_prompt_parts),
                                        model=self.config.llm.effective_model,
                                        temperature=0.9,
                                        max_tokens=300,
                                    )
                                    if isinstance(llm_result, dict) and llm_result.get("response"):
                                        generated = llm_result["response"].strip()
                                        if generated:
                                            seek_msg = generated
                                except Exception as e:
                                    self.ctx.logger.warning(f"LLM生成求喂消息失败: {e}")

                            # 发送求喂消息
                            try:
                                await self.ctx.send.text(seek_msg, stream_session_id)
                                # 更新最后求喂时间
                                await self.db.execute_commit(
                                    "UPDATE feed_groups SET last_seek_feed_time = ? WHERE group_id = ?",
                                    (time.time(), stream_group_id),
                                )
                            except Exception as e:
                                self.ctx.logger.warning(f"群{stream_group_id}发送求喂消息失败: {e}")

                except Exception as e:
                    self.ctx.logger.error(f"获取群聊天流失败: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.ctx.logger.error(f"求投喂任务异常: {e}")
                await asyncio.sleep(60)
                continue

            # 检查完毕后再 sleep
            if self._running:
                await asyncio.sleep(1800)

    # ---- LLM 回复生成 ----

    async def _generate_feed_reply(
        self,
        user_nickname: str,
        item_name: str,
        item_emoji: str,
        feed_reply_hint: str,
        satiety_before: float,
        satiety_after: float,
        satiety_bonus: float,
        recent_feeds: list[tuple[Any, ...]],
    ) -> str:
        """根据投喂上下文生成个性化回复。"""
        if not self.config.llm.enabled:
            return self.config.llm.fallback_reply

        # 构造最近投喂历史摘要
        recent_text = ""
        if recent_feeds:
            parts: list[str] = []
            for feed_name, feed_emoji, feed_reply in recent_feeds:
                display = f"{feed_emoji}{feed_name}" if feed_emoji else feed_name
                short = feed_reply[:20] + "..." if feed_reply and len(feed_reply) > 20 else (feed_reply or "")
                parts.append(f"{display}" + (f"({short})" if short else ""))
            recent_text = "、".join(parts)

        # 构造 prompt（用户可控字段做清洗防注入）
        safe_nickname = _sanitize_prompt_input(user_nickname)
        safe_item_name = _sanitize_prompt_input(item_name)
        safe_hint = _sanitize_prompt_input(feed_reply_hint)
        safe_recent = _sanitize_prompt_input(recent_text, max_len=500)

        prompt_parts = [
            f"你是一个可爱的聊天机器人，刚刚被{safe_nickname}投喂了{item_emoji}{safe_item_name}。",
            f"投喂前饱食度{satiety_before:.0f}/100，投喂后{satiety_after:.0f}/100（增加了{satiety_bonus:.0f}）。",
            "不要在回复中重复饱食度数值，只需自然地表达感受即可。",
        ]
        # 尝试获取 bot 人设和表达风格
        try:
            personality = await self.ctx.config.get("personality.personality", "")
            reply_style = await self.ctx.config.get("personality.reply_style", "")
            if personality:
                prompt_parts.append(f"你的人设：{personality}")
            if reply_style:
                prompt_parts.append(f"你的表达风格：{reply_style}")
        except Exception:
            pass
        if safe_hint:
            prompt_parts.append(f"投喂提示：{safe_hint}")
        if safe_recent:
            prompt_parts.append(f"最近被投喂了：{safe_recent}")
        prompt_parts.append("请用简短的语气回应这次投喂，1-2句话即可，可以包含emoji。不要重复之前说过的话。严格遵循你的表达风格。")

        prompt = "\n".join(prompt_parts)

        try:
            result = await self.ctx.llm.generate(
                prompt=prompt,
                model=self.config.llm.effective_model,
                temperature=self.config.llm.temperature,
                max_tokens=self.config.llm.max_tokens,
            )
            if isinstance(result, dict) and result.get("response"):
                reply = result["response"].strip()
                if reply:
                    return reply
        except Exception as e:
            self.ctx.logger.warning(f"LLM生成投喂回复失败: {e}")

        return self.config.llm.fallback_reply
