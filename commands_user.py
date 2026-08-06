"""投喂插件 — 用户命令 Mixin。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import math
import re
import time

from maibot_sdk import Command

from .config import FeedBotConfig, MAX_SIGN_BASE_POINTS
from .db import AsyncDatabase, _Rollback
from .utils import extract_nickname

MAX_BATCH_QUANTITY = 99


def parse_item_request(raw: str) -> tuple[str, int, str | None]:
    """解析道具名和可选的数量后缀（如 x5 或 5）。"""
    value = raw.strip()
    if not value:
        return "", 0, "缺少道具名"

    match = re.match(
        r"^(?P<item_name>.+?)(?:\s+(?:[xX*×](?P<quantity>\S+)|(?P<plain_quantity>[+-]?(?:\d+(?:\.\d*)?|\.\d+))))?$",
        value,
    )
    if match is None:
        return "", 0, "参数格式错误"

    item_name = match.group("item_name").strip()
    quantity_text = match.group("quantity") or match.group("plain_quantity")
    if quantity_text is None:
        return item_name, 1, None
    if not quantity_text.isdigit() or quantity_text == "0":
        return item_name, 0, "数量必须是正整数"

    normalized_quantity = quantity_text.lstrip("0") or "0"
    if normalized_quantity == "0":
        return item_name, 0, "数量必须是正整数"

    max_quantity_text = str(MAX_BATCH_QUANTITY)
    if len(normalized_quantity) > len(max_quantity_text) or (
        len(normalized_quantity) == len(max_quantity_text)
        and normalized_quantity > max_quantity_text
    ):
        return item_name, 0, f"单次最多操作{MAX_BATCH_QUANTITY}个道具"

    quantity = int(normalized_quantity)
    return item_name, quantity, None


class UserCommandsMixin:
    """用户命令：签到、积分、商店、购买、背包、投喂、投喂记录、饱食度、投喂排行、投喂规则。"""

    # 这些属性由 FeedBotPlugin 提供，类型标注仅供静态分析
    db: AsyncDatabase
    config: FeedBotConfig

    # ---- 签到命令 ----

    @Command("sign_in", description="每日签到获取积分", pattern=r"^/签到$")
    async def handle_sign_in(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        message: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /签到 命令。"""
        del kwargs

        if not user_id:
            return False, "无法获取用户信息", True
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        nickname = extract_nickname(message)
        await self.db.ensure_user(user_id, nickname, group_id)

        uid = self.db.user_key(user_id, group_id)

        # 签到基础积分：优先使用管理员通过 /投喂管理 签到积分 设置的值
        sign_base_override = await self.db.get_setting("sign_base_points")
        if sign_base_override is None:
            sign_base = self.config.sign.base_points
        else:
            try:
                sign_base = int(sign_base_override)
            except ValueError as error:
                raise ValueError(
                    "数据库设置 sign_base_points 不是整数："
                    f"{sign_base_override!r}"
                ) from error
        if sign_base_override is not None and not 0 <= sign_base <= MAX_SIGN_BASE_POINTS:
            raise ValueError(
                "数据库设置 sign_base_points 超出允许范围："
                f"{sign_base}，应在 0-{MAX_SIGN_BASE_POINTS} 之间"
            )

        # 在事务中原子完成：读取签到状态 + 判断 + 更新，防止 TOCTOU
        def _sign_tx(cursor: Any) -> tuple[bool, int, int, int, int] | None:
            now_ts = time.time()
            cursor.execute(
                "SELECT last_sign_time, consecutive_sign_days, total_sign_days FROM users WHERE user_id = ?",
                (uid,),
            )
            row = cursor.fetchone()
            if not row:
                return None  # 用户不存在

            last_sign_time, consecutive_days, total_days = row

            today = datetime.fromtimestamp(now_ts).date()
            last_sign_date = datetime.fromtimestamp(last_sign_time).date() if last_sign_time > 0 else None

            if last_sign_date == today:
                return (False, 0, consecutive_days, total_days, 0)  # 已签到

            yesterday = today - timedelta(days=1)
            if last_sign_date == yesterday:
                consecutive_days += 1
            else:
                consecutive_days = 1

            total_days += 1

            base = sign_base
            bonus = min(
                (consecutive_days - 1) * self.config.sign.consecutive_bonus,
                self.config.sign.max_consecutive_bonus,
            )
            earned = base + bonus

            cursor.execute(
                """
                UPDATE users SET points = points + ?, total_sign_days = ?,
                                 consecutive_sign_days = ?, last_sign_time = ?
                WHERE user_id = ?
                """,
                (earned, total_days, consecutive_days, now_ts, uid),
            )
            return (True, earned, consecutive_days, total_days, bonus)

        result = await self.db.run_in_transaction(_sign_tx)

        if result is None:
            return False, "用户不存在", True

        signed, earned, consecutive_days, total_days, bonus = result
        if not signed:
            await self.ctx.send.text("你今天已经签到过了～明天再来吧！", stream_id)
            return True, "已签到", True

        base = sign_base
        lines = [
            "✅ 签到成功！",
            f"💰 获得积分：{earned}（基础{base} + 连续奖励{bonus}）",
            f"📅 连续签到：{consecutive_days}天  累计签到：{total_days}天",
        ]
        if consecutive_days > 1:
            next_bonus = min(
                consecutive_days * self.config.sign.consecutive_bonus,
                self.config.sign.max_consecutive_bonus,
            )
            lines.append(f"🔥 明天签到可获得额外奖励：{next_bonus}积分")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, f"签到获得{earned}积分", True

    # ---- 积分命令 ----

    @Command("check_points", description="查看积分余额", pattern=r"^/积分$")
    async def handle_check_points(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        message: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /积分 命令。"""
        del kwargs

        if not user_id:
            return False, "无法获取用户信息", True
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        nickname = extract_nickname(message)
        await self.db.ensure_user(user_id, nickname, group_id)

        uid = self.db.user_key(user_id, group_id)
        row = await self.db.fetchone(
            "SELECT points, total_sign_days, consecutive_sign_days FROM users WHERE user_id = ?",
            (uid,),
        )
        if not row:
            return False, "用户不存在", True

        points, total_days, consecutive_days = row
        display_name = nickname or user_id

        msg = (
            f"💰 {display_name} 的积分信息\n"
            f"  当前积分：{points}\n"
            f"  累计签到：{total_days}天\n"
            f"  连续签到：{consecutive_days}天"
        )
        await self.ctx.send.text(msg, stream_id)
        return True, "查询积分", True

    @Command("points_ranking", description="查看本群积分排行", pattern=r"^/积分排行$")
    async def handle_points_ranking(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /积分排行 命令。"""
        del kwargs

        if not group_id:
            await self.ctx.send.text("积分排行仅支持群聊使用", stream_id)
            return False, "非群聊", True
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        prefix = f"{group_id}:"
        rows = await self.db.execute(
            """
            SELECT nickname, user_id, points
            FROM users
            WHERE user_id LIKE ? AND points > 0
            ORDER BY points DESC LIMIT 10
            """,
            (prefix + "%",),
        )

        if not rows:
            await self.ctx.send.text("暂无积分记录，快来 /签到 吧！", stream_id)
            return True, "无积分记录", True

        lines = ["🏆 积分排行榜"]
        for i, (nickname, uid, pts) in enumerate(rows, 1):
            display_uid = uid.split(":", 1)[-1] if ":" in uid else uid
            display_name = nickname or display_uid
            lines.append(f"  {i}. {display_name} — {pts}积分")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "积分排行", True

    # ---- 商店命令 ----

    @Command("shop", description="查看商店道具", pattern=r"^/商店$")
    async def handle_shop(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /商店 命令。"""
        del kwargs

        if not user_id:
            return False, "无法获取用户信息", True
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        global_items = await self.db.execute(
            """
            SELECT name, emoji, price, description, category, satiety_bonus
            FROM shop_items
            WHERE scope = 'global' AND is_on_sale = 1
            ORDER BY price ASC
            """
        )

        group_items: list[tuple[Any, ...]] = []
        if group_id:
            group_items = await self.db.execute(
                """
                SELECT name, emoji, price, description, category, satiety_bonus
                FROM shop_items
                WHERE scope = 'group' AND group_id = ? AND is_on_sale = 1
                ORDER BY price ASC
                """,
                (group_id,),
            )

        if not global_items and not group_items:
            await self.ctx.send.text("商店空空如也～等管理员上架道具吧！", stream_id)
            return True, "商店为空", True

        def _item_line(
            name: str, emoji: str, price: int, desc: str, satiety_bonus: float
        ) -> str:
            display = f"{emoji}{name}" if emoji else name
            desc_part = f" — {desc}" if desc else ""
            return f"  {display} {price}积分 饱食度{satiety_bonus:+}{desc_part}"

        lines: list[str] = []

        if global_items:
            lines.append("🌐 全局道具")
            lines.append("")
            for index, (name, emoji, price, desc, _cat, satiety_bonus) in enumerate(
                global_items
            ):
                if index > 0:
                    lines.append("")  # 每个商品之间隔一行，便于查看
                lines.append(_item_line(name, emoji, price, desc, satiety_bonus))

        if group_items:
            if lines:
                lines.append("")
            lines.append("🏠 本群专属")
            lines.append("")
            for index, (name, emoji, price, desc, _cat, satiety_bonus) in enumerate(
                group_items
            ):
                if index > 0:
                    lines.append("")
                lines.append(_item_line(name, emoji, price, desc, satiety_bonus))

        if lines:
            lines.append("")
        lines.append(
            f"💡 使用 /购买 <道具名> [x数量] 购买道具，单次最多{MAX_BATCH_QUANTITY}个"
        )

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "查看商店", True

    @Command("buy_item", description="购买道具", pattern=r"^/购买\s+(?P<item_request>.+)$")
    async def handle_buy(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        message: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /购买 命令。"""
        if not user_id:
            return False, "无法获取用户信息", True
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_request = str(matched_groups.get("item_request") or "")
        item_name, quantity, error = parse_item_request(item_request)
        if error:
            await self.ctx.send.text(
                f"{error}。用法：/购买 <道具名> [x数量]，"
                f"单次最多{MAX_BATCH_QUANTITY}个",
                stream_id,
            )
            return False, error, True

        nickname = extract_nickname(message)
        await self.db.ensure_user(user_id, nickname, group_id)

        uid = self.db.user_key(user_id, group_id)

        item = await self.db.find_shop_item(item_name, group_id)
        if not item:
            await self.ctx.send.text(f"没有找到道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        total_price = item["price"] * quantity

        # 在事务中原子完成：扣积分 + 加背包
        def _buy_tx(cursor: Any) -> int:
            # 扣积分（WHERE points >= ? 防止并发导致积分变负）
            cursor.execute(
                "UPDATE users SET points = points - ? WHERE user_id = ? AND points >= ?",
                (total_price, uid, total_price),
            )
            if cursor.rowcount == 0:
                return -1  # 积分不足
            # 加背包
            cursor.execute(
                """
                INSERT INTO user_inventory (user_id, item_id, quantity)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, item_id) DO UPDATE SET quantity = quantity + excluded.quantity
                """,
                (uid, item["item_id"], quantity),
            )
            # 查询扣除后的实际积分
            cursor.execute(
                "SELECT points FROM users WHERE user_id = ?",
                (uid,),
            )
            row = cursor.fetchone()
            return row[0] if row else 0

        result = await self.db.run_in_transaction(_buy_tx)

        if result < 0:
            # 查询当前积分用于提示
            row = await self.db.fetchone(
                "SELECT points FROM users WHERE user_id = ?",
                (uid,),
            )
            current_points = row[0] if row else 0
            await self.ctx.send.text(
                f"积分不足！{item['emoji']}{item['name']} x{quantity}需要{total_price}积分，"
                f"你只有{current_points}积分",
                stream_id,
            )
            return False, "积分不足", True

        remaining = result
        await self.ctx.send.text(
            f"🛒 购买成功！获得 {item['emoji']}{item['name']} x{quantity}\n"
            f"💰 花费积分：{total_price}  剩余积分：{remaining}",
            stream_id,
        )
        return True, f"购买{item_name} x{quantity}", True

    # ---- 背包命令 ----

    @Command("inventory", description="查看背包", pattern=r"^/背包$")
    async def handle_inventory(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /背包 命令。"""
        del kwargs

        if not user_id:
            return False, "无法获取用户信息", True
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        uid = self.db.user_key(user_id, group_id)
        rows = await self.db.execute(
            """
            SELECT si.name, si.emoji, si.satiety_bonus, inv.quantity
            FROM user_inventory inv
            JOIN shop_items si ON inv.item_id = si.item_id
            WHERE inv.user_id = ? AND inv.quantity > 0
            ORDER BY si.name ASC
            """,
            (uid,),
        )

        if not rows:
            await self.ctx.send.text("背包空空如也～去 /商店 买点东西吧！", stream_id)
            return True, "背包为空", True

        lines = ["🎒 你的背包"]
        for name, emoji, satiety_bonus, qty in rows:
            display = f"{emoji}{name}" if emoji else name
            lines.append(f"  {display} x{qty} 饱食度{satiety_bonus:+}")
        lines.append("")
        lines.append("💡 使用 /投喂 <道具名> 投喂我吧～")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "查看背包", True

    # ---- 投喂命令 ----

    @Command("feed_bot", description="投喂Bot", pattern=r"^/投喂\s+(?P<item_request>.+)$")
    async def handle_feed(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        message: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂 命令。"""
        if not user_id:
            return False, "无法获取用户信息", True
        if not group_id:
            await self.ctx.send.text("投喂仅支持群聊使用", stream_id)
            return False, "非群聊", True
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_request = str(matched_groups.get("item_request") or "")
        item_name, requested_quantity, error = parse_item_request(item_request)
        if error:
            await self.ctx.send.text(
                f"{error}。用法：/投喂 <道具名> [x数量]，"
                f"单次最多{MAX_BATCH_QUANTITY}个",
                stream_id,
            )
            return False, error, True

        nickname = extract_nickname(message)
        await self.db.ensure_user(user_id, nickname, group_id)

        uid = self.db.user_key(user_id, group_id)

        # 在事务中原子完成：校验库存 + 扣背包 + 更新饱食度和投喂记录
        def _feed_tx(
            cursor: Any,
        ) -> tuple[str, int, float, float, int, str, str, str, float] | None:
            now_ts = time.time()

            # 1. 在事务内读取库存和道具信息，避免并发投喂产生超扣
            cursor.execute(
                """
                SELECT inv.item_id, inv.quantity, si.name, si.emoji,
                       si.feed_reply_hint, si.satiety_bonus
                FROM user_inventory inv
                JOIN shop_items si ON inv.item_id = si.item_id
                WHERE inv.user_id = ? AND si.name = ? AND inv.quantity > 0
                """,
                (uid, item_name),
            )
            item_row = cursor.fetchone()
            if item_row is None:
                return ("missing", 0, 0.0, 0.0, 0, "", "", "", 0.0)

            item_id, inventory_quantity, name, emoji, reply_hint, satiety_bonus = item_row
            if inventory_quantity < requested_quantity:
                return (
                    "insufficient",
                    inventory_quantity,
                    0.0,
                    0.0,
                    0,
                    name,
                    emoji,
                    reply_hint,
                    satiety_bonus,
                )

            # 2. 获取当前饱食度（含衰减补偿）
            cursor.execute(
                "SELECT satiety, last_decay_time FROM feed_groups WHERE group_id = ?",
                (group_id,),
            )
            fg_row = cursor.fetchone()
            if fg_row:
                current_satiety, last_decay_time = fg_row[0], fg_row[1]
                if current_satiety < 0:
                    current_satiety = self.config.bot_attr.initial_satiety
                if last_decay_time > 0:
                    hours_elapsed = (now_ts - last_decay_time) / 3600.0
                    current_satiety = max(
                        0.0,
                        current_satiety
                        - hours_elapsed * self.config.bot_attr.satiety_decay_rate,
                    )
            else:
                current_satiety = self.config.bot_attr.initial_satiety

            # 3. 正饱食度道具只消耗当前还能吃下的数量
            if satiety_bonus > 0:
                if current_satiety >= 100:
                    raise _Rollback()
                capacity_quantity = math.ceil((100.0 - current_satiety) / satiety_bonus)
                consumed_quantity = min(requested_quantity, capacity_quantity)
            else:
                consumed_quantity = requested_quantity

            # 4. 条件扣减库存，失败则回滚整笔投喂
            cursor.execute(
                """
                UPDATE user_inventory
                SET quantity = quantity - ?
                WHERE user_id = ? AND item_id = ? AND quantity >= ?
                """,
                (consumed_quantity, uid, item_id, consumed_quantity),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("投喂时库存发生变化，请重试")
            cursor.execute(
                "DELETE FROM user_inventory WHERE user_id = ? AND item_id = ? AND quantity <= 0",
                (uid, item_id),
            )

            # 5. 更新饱食度和实际投喂件数
            new_satiety = max(
                0.0,
                min(100.0, current_satiety + satiety_bonus * consumed_quantity),
            )
            cursor.execute(
                """
                INSERT INTO feed_groups (group_id, enabled, satiety, last_seek_feed_time, last_decay_time, created_at)
                VALUES (?, 1, ?, 0, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET satiety = ?, last_decay_time = ?
                """,
                (group_id, new_satiety, now_ts, now_ts, new_satiety, now_ts),
            )
            cursor.execute(
                "UPDATE users SET total_feed_count = total_feed_count + ? WHERE user_id = ?",
                (consumed_quantity, uid),
            )

            # 6. 每次批量投喂写一条汇总记录
            cursor.execute(
                """
                INSERT INTO feed_records (user_id, nickname, group_id, item_id,
                                           item_name, item_emoji, quantity,
                                           reply_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    nickname,
                    group_id,
                    item_id,
                    name,
                    emoji,
                    consumed_quantity,
                    "",
                    now_ts,
                ),
            )
            record_id = cursor.lastrowid

            return (
                "success",
                consumed_quantity,
                current_satiety,
                new_satiety,
                record_id,
                name,
                emoji,
                reply_hint,
                satiety_bonus,
            )

        result = await self.db.run_in_transaction(_feed_tx)

        if result is None:
            await self.ctx.send.text("我已经吃饱了，吃不下啦～等饿一点再喂我吧！", stream_id)
            return False, "饱食度已满", True

        status, value, current_satiety, new_satiety, record_id, name, emoji, reply_hint, satiety_bonus = result
        if status == "missing":
            await self.ctx.send.text(
                f"你没有「{item_name}」哦～去 /商店 购买或检查 /背包", stream_id
            )
            return False, "背包无此道具", True
        if status == "insufficient":
            await self.ctx.send.text(
                f"库存不足！你想投喂 {item_name} x{requested_quantity}，背包里只有 x{value}",
                stream_id,
            )
            return False, "库存不足", True

        consumed_quantity = value
        satiety_change = new_satiety - current_satiety

        # 生成回复（在事务外，不影响数据一致性）
        recent_feeds = await self.db.execute(
            """
            SELECT item_name, item_emoji, quantity, reply_text
            FROM feed_records
            WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 3
            """,
            (uid,),
        )

        reply = await self._generate_feed_reply(
            user_nickname=nickname or user_id,
            item_name=name,
            item_emoji=emoji,
            quantity=consumed_quantity,
            feed_reply_hint=reply_hint,
            satiety_before=current_satiety,
            satiety_after=new_satiety,
            satiety_bonus=satiety_change,
            recent_feeds=recent_feeds,
        )

        # 按记录 ID 精确更新本次投喂回复
        await self.db.execute_commit(
            "UPDATE feed_records SET reply_text = ? WHERE id = ?",
            (reply, record_id),
        )

        display_item = f"{emoji}{name}" if emoji else name
        truncated = consumed_quantity < requested_quantity
        quantity_note = (
            f"（请求 x{requested_quantity}，吃下 x{consumed_quantity}）"
            if truncated
            else f"x{consumed_quantity}"
        )
        await self.ctx.send.text(
            f"{nickname or user_id} 投喂了 {display_item} {quantity_note} 给我～"
            f"饱食度 {satiety_change:+.0f}（{new_satiety:.0f}/100）\n{reply}",
            stream_id,
        )
        return True, f"投喂{name} x{consumed_quantity}", True

    @Command("feed_history", description="查看投喂记录", pattern=r"^/投喂记录$")
    async def handle_feed_history(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂记录 命令。"""
        del kwargs

        if not user_id:
            return False, "无法获取用户信息", True
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        uid = self.db.user_key(user_id, group_id)
        rows = await self.db.execute(
            """
            SELECT item_name, item_emoji, quantity, reply_text, created_at
            FROM feed_records
            WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 10
            """,
            (uid,),
        )

        if not rows:
            await self.ctx.send.text("还没有投喂记录哦～", stream_id)
            return True, "无投喂记录", True

        lines = ["📜 投喂记录"]
        for item_name, item_emoji, quantity, reply, created_at in rows:
            display = f"{item_emoji}{item_name}" if item_emoji else item_name
            dt = datetime.fromtimestamp(created_at).strftime("%m-%d %H:%M")
            lines.append(f"  [{dt}] {display} x{quantity}")
            if reply:
                short_reply = reply[:30] + "..." if len(reply) > 30 else reply
                lines.append(f"    ↳ {short_reply}")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "投喂记录", True

    @Command("satiety_status", description="查看当前饱食度", pattern=r"^/饱食度$")
    async def handle_satiety_status(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /饱食度 命令。"""
        del kwargs

        if not user_id:
            return False, "无法获取用户信息", True
        if not group_id:
            await self.ctx.send.text("饱食度仅支持群聊使用", stream_id)
            return False, "非群聊", True
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        satiety = await self.db.get_satiety(group_id, self.config)

        if satiety >= 80:
            desc = "饱饱的～好满足"
        elif satiety >= 50:
            desc = "还行，不太饿"
        elif satiety >= 30:
            desc = "有点饿了..."
        else:
            desc = "好饿好饿！快投喂我！"

        msg = f"🍖 当前饱食度（本群）：{satiety:.0f}/100 — {desc}"
        await self.ctx.send.text(msg, stream_id)
        return True, "饱食度", True

    @Command("feed_ranking", description="查看本群投喂排行", pattern=r"^/投喂排行$")
    async def handle_feed_ranking(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂排行 命令。"""
        del kwargs

        if not group_id:
            await self.ctx.send.text("投喂排行仅支持群聊使用", stream_id)
            return False, "非群聊", True
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        prefix = f"{group_id}:"
        rows = await self.db.execute(
            """
            SELECT u.nickname, u.user_id, u.total_feed_count
            FROM users u
            WHERE u.user_id LIKE ? AND u.total_feed_count > 0
            ORDER BY u.total_feed_count DESC LIMIT 10
            """,
            (prefix + "%",),
        )

        if not rows:
            await self.ctx.send.text("还没有人投喂过我呢～", stream_id)
            return True, "无投喂记录", True

        lines = ["🏆 投喂排行榜"]
        for i, (nickname, uid, count) in enumerate(rows, 1):
            display_uid = uid.split(":", 1)[-1] if ":" in uid else uid
            display_name = nickname or display_uid
            lines.append(f"  {i}. {display_name} — 投喂{count}次")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "投喂排行", True

    # ---- 规则命令 ----

    @Command("feed_rules", description="查看普通用户可用指令", pattern=r"^/投喂规则$")
    async def handle_feed_rules(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂规则 命令，列出非管理员可使用的所有指令。"""
        del kwargs

        if not user_id:
            return False, "无法获取用户信息", True
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        lines = [
            "📖 投喂规则",
            "以下指令所有成员均可使用：",
            "",
            "通用指令：",
            "/签到 — 每日签到获取积分",
            "/积分 — 查看积分余额",
            "/商店 — 查看可购买道具",
            f"/购买 <道具名> [x数量] — 购买道具，单次最多{MAX_BATCH_QUANTITY}个",
            "/背包 — 查看背包道具",
            "/投喂记录 — 查看投喂历史",
            "/投喂规则 — 查看本规则",
            "",
            "群聊指令：",
            "/积分排行 — 查看本群积分排行",
            f"/投喂 <道具名> [x数量] — 投喂Bot，单次最多{MAX_BATCH_QUANTITY}个",
            "/饱食度 — 查看当前饱食度",
            "/投喂排行 — 查看本群投喂排行",
            "",
            "管理员另有 /投喂管理 系列指令，仅管理员可执行",
        ]

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "投喂规则", True
