"""投喂插件 — 签到积分+商店道具+投喂Bot+定时求投喂

支持全局/群内两层商店、管理员指令和黑白名单。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from datetime import datetime
from typing import Any

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import EventType

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "feed_bot.db")

# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用投喂插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class AdminConfig(PluginConfigBase):
    """管理员配置。"""

    __ui_label__ = "管理员"
    __ui_icon__ = "shield"
    __ui_order__ = 1

    admin_users: list[str] = Field(default_factory=list, description="Bot管理员QQ号列表")


class SignConfig(PluginConfigBase):
    """签到配置。"""

    __ui_label__ = "签到"
    __ui_icon__ = "calendar-check"
    __ui_order__ = 2

    base_points: int = Field(default=10, description="签到基础积分")
    consecutive_bonus: int = Field(default=5, description="连续签到额外奖励（每天递增）")
    max_consecutive_bonus: int = Field(default=50, description="连续签到奖励上限")


class BotAttrConfig(PluginConfigBase):
    """Bot属性配置。"""

    __ui_label__ = "Bot属性"
    __ui_icon__ = "heart"
    __ui_order__ = 3

    initial_satiety: float = Field(default=80.0, description="初始饱食度（0-100）")
    initial_affection: float = Field(default=50.0, description="初始好感度（0-100）")
    satiety_decay_rate: float = Field(default=0.5, description="饱食度每小时衰减量")
    affection_decay_rate: float = Field(default=0.1, description="好感度每小时衰减量")
    seek_feed_threshold: float = Field(default=30.0, description="饱食度低于此值触发求投喂")
    seek_feed_interval_hours: float = Field(default=4.0, description="求投喂最小间隔（小时）")


class FilterConfig(PluginConfigBase):
    """触发控制配置。"""

    __ui_label__ = "触发控制"
    __ui_icon__ = "filter"
    __ui_order__ = 4

    mode: str = Field(default="whitelist", description="名单模式：whitelist 或 blacklist")
    group_list: list[str] = Field(default_factory=list, description="群号列表（白名单/黑名单）")
    allow_private: bool = Field(default=True, description="是否允许私聊触发")
    allow_group: bool = Field(default=True, description="是否允许群聊触发")


class LLMConfig(PluginConfigBase):
    """LLM回复配置。"""

    __ui_label__ = "LLM回复"
    __ui_icon__ = "brain"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="是否使用LLM生成投喂回复")
    model: str = Field(default="", description="LLM模型名（空=默认模型）")
    temperature: float = Field(default=0.8, description="LLM温度（越高越随机）")
    max_tokens: int = Field(default=150, description="LLM最大token数")
    fallback_reply: str = Field(default="谢谢你投喂我！好开心~", description="LLM不可用时的兜底回复")


class FeedBotConfig(PluginConfigBase):
    """投喂插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    sign: SignConfig = Field(default_factory=SignConfig)
    bot_attr: BotAttrConfig = Field(default_factory=BotAttrConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)


# ---------------------------------------------------------------------------
# 插件主类
# ---------------------------------------------------------------------------


class FeedBotPlugin(MaiBotPlugin):
    """投喂插件：签到积分+商店道具+投喂Bot+定时求投喂。"""

    config_model = FeedBotConfig

    def __init__(self) -> None:
        super().__init__()
        self._db: sqlite3.Connection | None = None
        self._running: bool = False
        self._decay_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._seek_feed_task: asyncio.Task | None = None  # type: ignore[type-arg]

    # ---- 生命周期 ----

    async def on_load(self) -> None:
        """插件加载时初始化数据目录和数据库。"""
        os.makedirs(DATA_DIR, exist_ok=True)

        self._db = sqlite3.connect(DB_PATH)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_tables()
        self._init_bot_attributes()

        self._running = True
        self._decay_task = asyncio.create_task(self._attr_decay_loop())
        self._seek_feed_task = asyncio.create_task(self._seek_feed_loop())

        self.ctx.logger.info("投喂插件已加载")

    async def on_unload(self) -> None:
        """插件卸载时关闭数据库连接和后台任务。"""
        self._running = False
        if self._decay_task:
            self._decay_task.cancel()
            self._decay_task = None
        if self._seek_feed_task:
            self._seek_feed_task.cancel()
            self._seek_feed_task = None
        if self._db:
            self._db.close()
            self._db = None
        self.ctx.logger.info("投喂插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        """配置热重载时执行。"""

    # ---- 数据库初始化 ----

    def _init_tables(self) -> None:
        """创建所有数据库表。"""
        assert self._db is not None

        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL DEFAULT '',
                points INTEGER NOT NULL DEFAULT 0,
                total_sign_days INTEGER NOT NULL DEFAULT 0,
                consecutive_sign_days INTEGER NOT NULL DEFAULT 0,
                last_sign_time REAL NOT NULL DEFAULT 0,
                total_feed_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS shop_items (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                emoji TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                price INTEGER NOT NULL,
                feed_reply_hint TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                satiety_bonus REAL NOT NULL DEFAULT 5.0,
                affection_bonus REAL NOT NULL DEFAULT 2.0,
                scope TEXT NOT NULL DEFAULT 'global',
                group_id TEXT NOT NULL DEFAULT '',
                is_on_sale INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_shop_items_scope
            ON shop_items(scope, group_id)
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_inventory (
                user_id TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, item_id)
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                group_id TEXT NOT NULL DEFAULT '',
                item_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                item_emoji TEXT NOT NULL DEFAULT '',
                reply_text TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_feed_records_user
            ON feed_records(user_id)
            """
        )
        self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_feed_records_time
            ON feed_records(created_at)
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_attributes (
                attr_key TEXT PRIMARY KEY,
                attr_value REAL NOT NULL DEFAULT 0,
                last_update_time REAL NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_groups (
                group_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_seek_feed_time REAL NOT NULL DEFAULT 0,
                seek_feed_message TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_admins (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                PRIMARY KEY (group_id, user_id)
            )
            """
        )
        self._db.commit()

    def _init_bot_attributes(self) -> None:
        """初始化 bot 属性（如果不存在）。"""
        assert self._db is not None
        now = time.time()
        defaults = [
            ("satiety", self.config.bot_attr.initial_satiety, now),
            ("affection", self.config.bot_attr.initial_affection, now),
        ]
        for attr_key, attr_value, ts in defaults:
            cursor = self._db.execute(
                "SELECT 1 FROM bot_attributes WHERE attr_key = ?",
                (attr_key,),
            )
            if cursor.fetchone() is None:
                self._db.execute(
                    "INSERT INTO bot_attributes (attr_key, attr_value, last_update_time) VALUES (?, ?, ?)",
                    (attr_key, attr_value, ts),
                )
        self._db.commit()

    # ---- 权限与过滤 ----

    def _is_bot_admin(self, user_id: str) -> bool:
        """判断是否为Bot管理员。"""
        return user_id in self.config.admin.admin_users

    def _is_group_admin(self, group_id: str, user_id: str) -> bool:
        """判断是否为指定群的管理员（含Bot管理员）。"""
        if self._is_bot_admin(user_id):
            return True
        assert self._db is not None
        cursor = self._db.execute(
            "SELECT 1 FROM group_admins WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        return cursor.fetchone() is not None

    def _check_permission(self, group_id: str, user_id: str, is_group: bool) -> bool:
        """检查触发权限：黑白名单 + 群聊/私聊开关。"""
        # 私聊/群聊开关
        if not is_group and not self.config.filter.allow_private:
            return False
        if is_group and not self.config.filter.allow_group:
            return False

        # 黑白名单
        if self.config.filter.mode == "whitelist":
            if is_group and group_id not in self.config.filter.group_list:
                return False
        elif self.config.filter.mode == "blacklist":
            if is_group and group_id in self.config.filter.group_list:
                return False

        return True

    # ---- 内部方法 ----

    def _ensure_user(self, user_id: str, nickname: str) -> None:
        """确保用户存在于数据库中。"""
        assert self._db is not None
        cursor = self._db.execute(
            "SELECT 1 FROM users WHERE user_id = ?",
            (user_id,),
        )
        if cursor.fetchone() is None:
            self._db.execute(
                """
                INSERT INTO users (user_id, nickname, points, total_sign_days,
                                   consecutive_sign_days, last_sign_time,
                                   total_feed_count, created_at)
                VALUES (?, ?, 0, 0, 0, 0, 0, ?)
                """,
                (user_id, nickname, time.time()),
            )
            self._db.commit()
        elif nickname:
            # 更新昵称
            self._db.execute(
                "UPDATE users SET nickname = ? WHERE user_id = ? AND nickname != ?",
                (nickname, user_id, nickname),
            )
            self._db.commit()

    def _get_attr(self, attr_key: str) -> float:
        """获取 bot 属性值。"""
        assert self._db is not None
        cursor = self._db.execute(
            "SELECT attr_value FROM bot_attributes WHERE attr_key = ?",
            (attr_key,),
        )
        row = cursor.fetchone()
        return float(row[0]) if row else 0.0

    def _set_attr(self, attr_key: str, value: float) -> None:
        """设置 bot 属性值（钳位到 0-100）。"""
        assert self._db is not None
        value = max(0.0, min(100.0, value))
        self._db.execute(
            "UPDATE bot_attributes SET attr_value = ?, last_update_time = ? WHERE attr_key = ?",
            (value, time.time(), attr_key),
        )
        self._db.commit()

    def _add_attr(self, attr_key: str, delta: float) -> float:
        """增减 bot 属性值，返回更新后的值。"""
        current = self._get_attr(attr_key)
        new_value = max(0.0, min(100.0, current + delta))
        self._set_attr(attr_key, new_value)
        return new_value

    def _find_shop_item(self, item_name: str, group_id: str) -> dict[str, Any] | None:
        """查找道具：优先本群专属，其次全局。"""
        assert self._db is not None

        # 优先查找群内道具
        if group_id:
            cursor = self._db.execute(
                """
                SELECT item_id, name, emoji, description, price, feed_reply_hint,
                       category, satiety_bonus, affection_bonus, scope, group_id
                FROM shop_items
                WHERE name = ? AND scope = 'group' AND group_id = ? AND is_on_sale = 1
                """,
                (item_name, group_id),
            )
            row = cursor.fetchone()
            if row:
                return self._row_to_item_dict(row)

        # 其次查找全局道具
        cursor = self._db.execute(
            """
            SELECT item_id, name, emoji, description, price, feed_reply_hint,
                   category, satiety_bonus, affection_bonus, scope, group_id
            FROM shop_items
            WHERE name = ? AND scope = 'global' AND is_on_sale = 1
            """,
            (item_name,),
        )
        row = cursor.fetchone()
        if row:
            return self._row_to_item_dict(row)

        return None

    @staticmethod
    def _row_to_item_dict(row: tuple[Any, ...]) -> dict[str, Any]:
        """将数据库行转为道具字典。"""
        return {
            "item_id": row[0],
            "name": row[1],
            "emoji": row[2],
            "description": row[3],
            "price": row[4],
            "feed_reply_hint": row[5],
            "category": row[6],
            "satiety_bonus": row[7],
            "affection_bonus": row[8],
            "scope": row[9],
            "group_id": row[10],
        }

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

        is_group = bool(group_id)
        if not self._check_permission(group_id, user_id, is_group):
            return False, "无权限", True

        # 提取昵称
        nickname = _extract_nickname(message)
        self._ensure_user(user_id, nickname)

        assert self._db is not None

        # 检查今天是否已签到
        now = time.time()
        cursor = self._db.execute(
            "SELECT last_sign_time, consecutive_sign_days, total_sign_days FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        if not row:
            return False, "用户不存在", True

        last_sign_time, consecutive_days, total_days = row

        # 判断今天是否已签到（以本地日期判断）
        today = datetime.fromtimestamp(now).date()
        last_sign_date = datetime.fromtimestamp(last_sign_time).date() if last_sign_time > 0 else None

        if last_sign_date == today:
            await self.ctx.send.text("你今天已经签到过了～明天再来吧！", stream_id)
            return True, "已签到", True

        # 判断是否连续签到
        from datetime import timedelta

        yesterday = today - timedelta(days=1)
        if last_sign_date == yesterday:
            consecutive_days += 1
        else:
            consecutive_days = 1

        total_days += 1

        # 计算积分
        base = self.config.sign.base_points
        bonus = min(
            (consecutive_days - 1) * self.config.sign.consecutive_bonus,
            self.config.sign.max_consecutive_bonus,
        )
        earned = base + bonus

        # 更新数据库
        self._db.execute(
            """
            UPDATE users SET points = points + ?, total_sign_days = ?,
                             consecutive_sign_days = ?, last_sign_time = ?
            WHERE user_id = ?
            """,
            (earned, total_days, consecutive_days, now, user_id),
        )
        self._db.commit()

        # 构建回复
        lines = [
            f"✅ 签到成功！",
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

        is_group = bool(group_id)
        if not self._check_permission(group_id, user_id, is_group):
            return False, "无权限", True

        nickname = _extract_nickname(message)
        self._ensure_user(user_id, nickname)

        assert self._db is not None
        cursor = self._db.execute(
            "SELECT points, total_sign_days, consecutive_sign_days FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
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

        if not self._check_permission(group_id, user_id, True):
            return False, "无权限", True

        assert self._db is not None
        cursor = self._db.execute(
            """
            SELECT nickname, user_id, points
            FROM users
            WHERE points > 0
            ORDER BY points DESC LIMIT 10
            """
        )
        rows = cursor.fetchall()

        if not rows:
            await self.ctx.send.text("暂无积分记录，快来 /签到 吧！", stream_id)
            return True, "无积分记录", True

        lines = ["🏆 积分排行榜"]
        for i, (nickname, uid, pts) in enumerate(rows, 1):
            display_name = nickname or uid
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

        is_group = bool(group_id)
        if not self._check_permission(group_id, user_id, is_group):
            return False, "无权限", True

        assert self._db is not None

        # 查询全局道具
        global_cursor = self._db.execute(
            """
            SELECT name, emoji, price, description, category
            FROM shop_items
            WHERE scope = 'global' AND is_on_sale = 1
            ORDER BY price ASC
            """
        )
        global_items = global_cursor.fetchall()

        # 查询群内道具
        group_items = []
        if group_id:
            group_cursor = self._db.execute(
                """
                SELECT name, emoji, price, description, category
                FROM shop_items
                WHERE scope = 'group' AND group_id = ? AND is_on_sale = 1
                ORDER BY price ASC
                """,
                (group_id,),
            )
            group_items = group_cursor.fetchall()

        if not global_items and not group_items:
            await self.ctx.send.text("商店空空如也～等管理员上架道具吧！", stream_id)
            return True, "商店为空", True

        lines: list[str] = []

        if global_items:
            lines.append("🌐 全局道具")
            for name, emoji, price, desc, _cat in global_items:
                display = f"{emoji}{name}" if emoji else name
                desc_part = f" — {desc}" if desc else ""
                lines.append(f"  {display} {price}积分{desc_part}")

        if group_items:
            lines.append("")
            lines.append("🏠 本群专属")
            for name, emoji, price, desc, _cat in group_items:
                display = f"{emoji}{name}" if emoji else name
                desc_part = f" — {desc}" if desc else ""
                lines.append(f"  {display} {price}积分{desc_part}")

        lines.append("")
        lines.append("💡 使用 /购买 <道具名> 购买道具")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "查看商店", True

    @Command("buy_item", description="购买道具", pattern=r"^/购买\s+(?P<item_name>.+)$")
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

        is_group = bool(group_id)
        if not self._check_permission(group_id, user_id, is_group):
            return False, "无权限", True

        # 获取道具名
        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/购买 <道具名>", stream_id)
            return False, "缺少道具名", True

        nickname = _extract_nickname(message)
        self._ensure_user(user_id, nickname)

        assert self._db is not None

        # 查找道具
        item = self._find_shop_item(item_name, group_id)
        if not item:
            await self.ctx.send.text(f"没有找到道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        # 检查积分
        cursor = self._db.execute(
            "SELECT points FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        if not row:
            return False, "用户不存在", True

        current_points = row[0]
        if current_points < item["price"]:
            await self.ctx.send.text(
                f"积分不足！{item['emoji']}{item['name']}需要{item['price']}积分，你只有{current_points}积分",
                stream_id,
            )
            return False, "积分不足", True

        # 扣减积分，增加背包
        self._db.execute(
            "UPDATE users SET points = points - ? WHERE user_id = ?",
            (item["price"], user_id),
        )
        self._db.execute(
            """
            INSERT INTO user_inventory (user_id, item_id, quantity)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, item_id) DO UPDATE SET quantity = quantity + 1
            """,
            (user_id, item["item_id"]),
        )
        self._db.commit()

        await self.ctx.send.text(
            f"🛒 购买成功！获得 {item['emoji']}{item['name']} x1\n"
            f"💰 剩余积分：{current_points - item['price']}",
            stream_id,
        )
        return True, f"购买{item_name}", True

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

        is_group = bool(group_id)
        if not self._check_permission(group_id, user_id, is_group):
            return False, "无权限", True

        assert self._db is not None
        cursor = self._db.execute(
            """
            SELECT si.name, si.emoji, inv.quantity
            FROM user_inventory inv
            JOIN shop_items si ON inv.item_id = si.item_id
            WHERE inv.user_id = ? AND inv.quantity > 0
            ORDER BY si.name ASC
            """,
            (user_id,),
        )
        rows = cursor.fetchall()

        if not rows:
            await self.ctx.send.text("背包空空如也～去 /商店 买点东西吧！", stream_id)
            return True, "背包为空", True

        lines = ["🎒 你的背包"]
        for name, emoji, qty in rows:
            display = f"{emoji}{name}" if emoji else name
            lines.append(f"  {display} x{qty}")
        lines.append("")
        lines.append("💡 使用 /投喂 <道具名> 投喂我吧～")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "查看背包", True

    # ---- 投喂命令 ----

    @Command("feed_bot", description="投喂Bot", pattern=r"^/投喂\s+(?P<item_name>.+)$")
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

        is_group = bool(group_id)
        if not self._check_permission(group_id, user_id, is_group):
            return False, "无权限", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/投喂 <道具名>", stream_id)
            return False, "缺少道具名", True

        nickname = _extract_nickname(message)
        self._ensure_user(user_id, nickname)

        assert self._db is not None

        # 查找背包中的道具
        cursor = self._db.execute(
            """
            SELECT inv.item_id, inv.quantity, si.name, si.emoji, si.feed_reply_hint,
                   si.satiety_bonus, si.affection_bonus
            FROM user_inventory inv
            JOIN shop_items si ON inv.item_id = si.item_id
            WHERE inv.user_id = ? AND si.name = ? AND inv.quantity > 0
            """,
            (user_id, item_name),
        )
        row = cursor.fetchone()

        if not row:
            await self.ctx.send.text(
                f"你没有「{item_name}」哦～去 /商店 购买或检查 /背包", stream_id
            )
            return False, "背包无此道具", True

        item_id, _qty, name, emoji, reply_hint, satiety_bonus, affection_bonus = row

        # 扣减背包
        self._db.execute(
            "UPDATE user_inventory SET quantity = quantity - 1 WHERE user_id = ? AND item_id = ?",
            (user_id, item_id),
        )
        # 清理数量为0的记录
        self._db.execute(
            "DELETE FROM user_inventory WHERE user_id = ? AND item_id = ? AND quantity <= 0",
            (user_id, item_id),
        )

        # 增加 bot 属性
        new_satiety = self._add_attr("satiety", satiety_bonus)
        new_affection = self._add_attr("affection", affection_bonus)

        # 更新用户投喂计数
        self._db.execute(
            "UPDATE users SET total_feed_count = total_feed_count + 1 WHERE user_id = ?",
            (user_id,),
        )

        # 获取最近投喂记录（用于 LLM 生成）
        recent_cursor = self._db.execute(
            """
            SELECT item_name, item_emoji, reply_text
            FROM feed_records
            ORDER BY created_at DESC LIMIT 3
            """
        )
        recent_feeds = recent_cursor.fetchall()

        # 生成 LLM 回复
        reply = await self._generate_feed_reply(
            user_nickname=nickname or user_id,
            item_name=name,
            item_emoji=emoji,
            feed_reply_hint=reply_hint,
            satiety=new_satiety,
            affection=new_affection,
            recent_feeds=recent_feeds,
        )

        # 记录投喂历史
        now = time.time()
        self._db.execute(
            """
            INSERT INTO feed_records (user_id, nickname, group_id, item_id,
                                       item_name, item_emoji, reply_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, nickname, group_id, item_id, name, emoji, reply, now),
        )
        self._db.commit()

        # 发送回复
        display_item = f"{emoji}{name}" if emoji else name
        await self.ctx.send.text(
            f"{nickname} 投喂了 {display_item} 给我～\n{reply}",
            stream_id,
        )
        return True, f"投喂{name}", True

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

        is_group = bool(group_id)
        if not self._check_permission(group_id, user_id, is_group):
            return False, "无权限", True

        assert self._db is not None
        cursor = self._db.execute(
            """
            SELECT item_name, item_emoji, reply_text, created_at
            FROM feed_records
            WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 10
            """,
            (user_id,),
        )
        rows = cursor.fetchall()

        if not rows:
            await self.ctx.send.text("还没有投喂记录哦～", stream_id)
            return True, "无投喂记录", True

        lines = ["📜 投喂记录"]
        for item_name, item_emoji, reply, created_at in rows:
            display = f"{item_emoji}{item_name}" if item_emoji else item_name
            dt = datetime.fromtimestamp(created_at).strftime("%m-%d %H:%M")
            lines.append(f"  [{dt}] {display}")
            if reply:
                # 截取回复前30字
                short_reply = reply[:30] + "..." if len(reply) > 30 else reply
                lines.append(f"    ↳ {short_reply}")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "投喂记录", True

    @Command("bot_status", description="查看Bot状态", pattern=r"^/bot状态$")
    async def handle_bot_status(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /bot状态 命令。"""
        del kwargs

        if not user_id:
            return False, "无法获取用户信息", True

        is_group = bool(group_id)
        if not self._check_permission(group_id, user_id, is_group):
            return False, "无权限", True

        satiety = self._get_attr("satiety")
        affection = self._get_attr("affection")

        # 根据饱食度生成状态描述
        if satiety >= 80:
            satiety_desc = "饱饱的～好满足"
        elif satiety >= 50:
            satiety_desc = "还行，不太饿"
        elif satiety >= 30:
            satiety_desc = "有点饿了..."
        else:
            satiety_desc = "好饿好饿！快投喂我！"

        # 根据好感度生成状态描述
        if affection >= 80:
            affection_desc = "超喜欢你！"
        elif affection >= 50:
            affection_desc = "我们关系不错呢"
        elif affection >= 30:
            affection_desc = "还在熟悉中..."
        else:
            affection_desc = "有点陌生..."

        msg = (
            f"📊 我的状态\n"
            f"  🍖 饱食度：{satiety:.0f}/100 — {satiety_desc}\n"
            f"  💕 好感度：{affection:.0f}/100 — {affection_desc}"
        )
        await self.ctx.send.text(msg, stream_id)
        return True, "Bot状态", True

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

        if not self._check_permission(group_id, user_id, True):
            return False, "无权限", True

        assert self._db is not None
        cursor = self._db.execute(
            """
            SELECT u.nickname, u.user_id, u.total_feed_count
            FROM users u
            WHERE u.total_feed_count > 0
            ORDER BY u.total_feed_count DESC LIMIT 10
            """
        )
        rows = cursor.fetchall()

        if not rows:
            await self.ctx.send.text("还没有人投喂过我呢～", stream_id)
            return True, "无投喂记录", True

        lines = ["🏆 投喂排行榜"]
        for i, (nickname, uid, count) in enumerate(rows, 1):
            display_name = nickname or uid
            lines.append(f"  {i}. {display_name} — 投喂{count}次")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "投喂排行", True

    # ---- Bot管理员命令 ----

    @Command(
        "admin_global_add_item",
        description="上架全局道具（Bot管理员）",
        pattern=r"^/投喂管理\s+全局上架\s+(?P<name>\S+)\s+(?P<price>\d+)(?:\s+(?P<emoji>\S+))?(?:\s+(?P<desc>.+))?$",
    )
    async def handle_admin_global_add_item(
        self,
        stream_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 全局上架 命令。"""
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        name = str(matched_groups.get("name") or "").strip()
        price_str = str(matched_groups.get("price") or "").strip()
        emoji = str(matched_groups.get("emoji") or "").strip()
        desc = str(matched_groups.get("desc") or "").strip()

        if not name or not price_str:
            await self.ctx.send.text("用法：/投喂管理 全局上架 <名称> <价格> [emoji] [描述]", stream_id)
            return False, "参数缺失", True

        try:
            price = int(price_str)
        except ValueError:
            await self.ctx.send.text("价格必须是整数", stream_id)
            return False, "价格格式错误", True

        assert self._db is not None

        # 检查是否重名
        cursor = self._db.execute(
            "SELECT 1 FROM shop_items WHERE name = ? AND scope = 'global' AND is_on_sale = 1",
            (name,),
        )
        if cursor.fetchone():
            await self.ctx.send.text(f"全局道具「{name}」已存在", stream_id)
            return False, "道具已存在", True

        self._db.execute(
            """
            INSERT INTO shop_items (name, emoji, description, price, scope, group_id,
                                     is_on_sale, created_by, created_at)
            VALUES (?, ?, ?, ?, 'global', '', 1, ?, ?)
            """,
            (name, emoji, desc, price, user_id, time.time()),
        )
        self._db.commit()

        display = f"{emoji}{name}" if emoji else name
        await self.ctx.send.text(
            f"✅ 全局道具「{display}」已上架，价格：{price}积分", stream_id
        )
        return True, f"上架全局道具{name}", True

    @Command(
        "admin_global_remove_item",
        description="下架全局道具（Bot管理员）",
        pattern=r"^/投喂管理\s+全局下架\s+(?P<item_name>.+)$",
    )
    async def handle_admin_global_remove_item(
        self,
        stream_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 全局下架 命令。"""
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/投喂管理 全局下架 <道具名>", stream_id)
            return False, "缺少道具名", True

        assert self._db is not None
        cursor = self._db.execute(
            "UPDATE shop_items SET is_on_sale = 0 WHERE name = ? AND scope = 'global'",
            (item_name,),
        )
        if cursor.rowcount == 0:
            await self.ctx.send.text(f"未找到全局道具「{item_name}」", stream_id)
            return False, "道具不存在", True
        self._db.commit()

        await self.ctx.send.text(f"✅ 全局道具「{item_name}」已下架", stream_id)
        return True, f"下架全局道具{item_name}", True

    @Command(
        "admin_points",
        description="调整用户积分（Bot管理员）",
        pattern=r"^/投喂管理\s+积分\s+(?P<target_user>\S+)\s+(?P<amount>-?\d+)$",
    )
    async def handle_admin_points(
        self,
        stream_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 积分 命令。"""
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        target_user = str(matched_groups.get("target_user") or "").strip()
        amount_str = str(matched_groups.get("amount") or "").strip()

        if not target_user or not amount_str:
            await self.ctx.send.text("用法：/投喂管理 积分 <QQ号> <数量>", stream_id)
            return False, "参数缺失", True

        try:
            amount = int(amount_str)
        except ValueError:
            await self.ctx.send.text("数量必须是整数", stream_id)
            return False, "数量格式错误", True

        assert self._db is not None

        # 确保目标用户存在
        self._ensure_user(target_user, "")

        self._db.execute(
            "UPDATE users SET points = MAX(0, points + ?) WHERE user_id = ?",
            (amount, target_user),
        )
        self._db.commit()

        action = "增加" if amount >= 0 else "减少"
        await self.ctx.send.text(
            f"✅ 已为 {target_user} {action} {abs(amount)} 积分", stream_id
        )
        return True, f"调整积分{amount}", True

    @Command(
        "admin_attr",
        description="设置Bot属性（Bot管理员）",
        pattern=r"^/投喂管理\s+属性\s+(?P<attr_key>\S+)\s+(?P<attr_value>\d+\.?\d*)$",
    )
    async def handle_admin_attr(
        self,
        stream_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 属性 命令。"""
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        attr_key = str(matched_groups.get("attr_key") or "").strip()
        attr_value_str = str(matched_groups.get("attr_value") or "").strip()

        if not attr_key or not attr_value_str:
            await self.ctx.send.text("用法：/投喂管理 属性 <属性名> <值>（属性名：satiety/affection）", stream_id)
            return False, "参数缺失", True

        valid_keys = {"satiety", "affection"}
        if attr_key not in valid_keys:
            await self.ctx.send.text(f"无效属性名，可选：{', '.join(valid_keys)}", stream_id)
            return False, "无效属性名", True

        try:
            attr_value = float(attr_value_str)
        except ValueError:
            await self.ctx.send.text("属性值必须是数字", stream_id)
            return False, "属性值格式错误", True

        self._set_attr(attr_key, attr_value)

        key_names = {"satiety": "饱食度", "affection": "好感度"}
        await self.ctx.send.text(
            f"✅ {key_names.get(attr_key, attr_key)}已设置为 {attr_value:.0f}", stream_id
        )
        return True, f"设置属性{attr_key}", True

    @Command(
        "admin_seek_feed",
        description="触发求投喂（Bot管理员）",
        pattern=r"^/投喂管理\s+求喂\s+(?P<group_id>\S+)\s+(?P<message>.+)$",
    )
    async def handle_admin_seek_feed(
        self,
        stream_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 求喂 命令。"""
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        target_group_id = str(matched_groups.get("group_id") or "").strip()
        seek_msg = str(matched_groups.get("message") or "").strip()

        if not target_group_id or not seek_msg:
            await self.ctx.send.text("用法：/投喂管理 求喂 <群号> <消息>", stream_id)
            return False, "参数缺失", True

        # 查找群的聊天流
        chat_stream = await self.ctx.chat.get_stream_by_group_id(target_group_id)
        if not chat_stream:
            await self.ctx.send.text(f"未找到群 {target_group_id} 的聊天流", stream_id)
            return False, "聊天流不存在", True

        # 获取 session_id
        target_stream_id = ""
        if isinstance(chat_stream, dict):
            target_stream_id = str(chat_stream.get("session_id", ""))
        elif hasattr(chat_stream, "session_id"):
            target_stream_id = str(chat_stream.session_id)

        if not target_stream_id:
            await self.ctx.send.text(f"无法获取群 {target_group_id} 的聊天流ID", stream_id)
            return False, "聊天流ID为空", True

        await self.ctx.send.text(seek_msg, target_stream_id)
        await self.ctx.send.text(f"✅ 已在群 {target_group_id} 发送求投喂消息", stream_id)
        return True, "求投喂", True

    @Command(
        "admin_reset_sign",
        description="重置用户签到（Bot管理员）",
        pattern=r"^/投喂管理\s+重置签到\s+(?P<target_user>\S+)$",
    )
    async def handle_admin_reset_sign(
        self,
        stream_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 重置签到 命令。"""
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        target_user = str(matched_groups.get("target_user") or "").strip()

        if not target_user:
            await self.ctx.send.text("用法：/投喂管理 重置签到 <QQ号>", stream_id)
            return False, "参数缺失", True

        assert self._db is not None
        self._db.execute(
            """
            UPDATE users SET total_sign_days = 0, consecutive_sign_days = 0, last_sign_time = 0
            WHERE user_id = ?
            """,
            (target_user,),
        )
        self._db.commit()

        await self.ctx.send.text(f"✅ 已重置 {target_user} 的签到记录", stream_id)
        return True, f"重置签到{target_user}", True

    @Command(
        "admin_grant",
        description="授权群管理员（Bot管理员）",
        pattern=r"^/投喂管理\s+授权\s+(?P<group_id>\S+)\s+(?P<target_user>\S+)$",
    )
    async def handle_admin_grant(
        self,
        stream_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 授权 命令。"""
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        target_group_id = str(matched_groups.get("group_id") or "").strip()
        target_user = str(matched_groups.get("target_user") or "").strip()

        if not target_group_id or not target_user:
            await self.ctx.send.text("用法：/投喂管理 授权 <群号> <QQ号>", stream_id)
            return False, "参数缺失", True

        assert self._db is not None
        try:
            self._db.execute(
                "INSERT OR IGNORE INTO group_admins (group_id, user_id) VALUES (?, ?)",
                (target_group_id, target_user),
            )
            self._db.commit()
        except sqlite3.Error as e:
            await self.ctx.send.text(f"授权失败：{e}", stream_id)
            return False, "授权失败", True

        await self.ctx.send.text(
            f"✅ 已授权 {target_user} 为群 {target_group_id} 的管理员", stream_id
        )
        return True, f"授权群管理员{target_user}", True

    @Command(
        "admin_revoke",
        description="取消群管理员授权（Bot管理员）",
        pattern=r"^/投喂管理\s+取消授权\s+(?P<group_id>\S+)\s+(?P<target_user>\S+)$",
    )
    async def handle_admin_revoke(
        self,
        stream_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 取消授权 命令。"""
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        target_group_id = str(matched_groups.get("group_id") or "").strip()
        target_user = str(matched_groups.get("target_user") or "").strip()

        if not target_group_id or not target_user:
            await self.ctx.send.text("用法：/投喂管理 取消授权 <群号> <QQ号>", stream_id)
            return False, "参数缺失", True

        assert self._db is not None
        self._db.execute(
            "DELETE FROM group_admins WHERE group_id = ? AND user_id = ?",
            (target_group_id, target_user),
        )
        self._db.commit()

        await self.ctx.send.text(
            f"✅ 已取消 {target_user} 在群 {target_group_id} 的管理员权限", stream_id
        )
        return True, f"取消授权{target_user}", True

    # ---- 群管理员命令 ----

    @Command(
        "admin_group_add_item",
        description="上架群内道具（群管理员）",
        pattern=r"^/投喂管理\s+群上架\s+(?P<name>\S+)\s+(?P<price>\d+)(?:\s+(?P<emoji>\S+))?(?:\s+(?P<desc>.+))?$",
    )
    async def handle_admin_group_add_item(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 群上架 命令。"""
        if not group_id:
            await self.ctx.send.text("群上架仅支持在群聊中使用", stream_id)
            return False, "非群聊", True

        if not self._is_group_admin(group_id, user_id):
            await self.ctx.send.text("只有群管理员或Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        name = str(matched_groups.get("name") or "").strip()
        price_str = str(matched_groups.get("price") or "").strip()
        emoji = str(matched_groups.get("emoji") or "").strip()
        desc = str(matched_groups.get("desc") or "").strip()

        if not name or not price_str:
            await self.ctx.send.text("用法：/投喂管理 群上架 <名称> <价格> [emoji] [描述]", stream_id)
            return False, "参数缺失", True

        try:
            price = int(price_str)
        except ValueError:
            await self.ctx.send.text("价格必须是整数", stream_id)
            return False, "价格格式错误", True

        assert self._db is not None

        # 检查群内是否重名
        cursor = self._db.execute(
            "SELECT 1 FROM shop_items WHERE name = ? AND scope = 'group' AND group_id = ? AND is_on_sale = 1",
            (name, group_id),
        )
        if cursor.fetchone():
            await self.ctx.send.text(f"本群道具「{name}」已存在", stream_id)
            return False, "道具已存在", True

        self._db.execute(
            """
            INSERT INTO shop_items (name, emoji, description, price, scope, group_id,
                                     is_on_sale, created_by, created_at)
            VALUES (?, ?, ?, ?, 'group', ?, 1, ?, ?)
            """,
            (name, emoji, desc, price, group_id, user_id, time.time()),
        )
        self._db.commit()

        display = f"{emoji}{name}" if emoji else name
        await self.ctx.send.text(
            f"✅ 本群道具「{display}」已上架，价格：{price}积分", stream_id
        )
        return True, f"群上架{name}", True

    @Command(
        "admin_group_remove_item",
        description="下架群内道具（群管理员）",
        pattern=r"^/投喂管理\s+群下架\s+(?P<item_name>.+)$",
    )
    async def handle_admin_group_remove_item(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 群下架 命令。"""
        if not group_id:
            await self.ctx.send.text("群下架仅支持在群聊中使用", stream_id)
            return False, "非群聊", True

        if not self._is_group_admin(group_id, user_id):
            await self.ctx.send.text("只有群管理员或Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/投喂管理 群下架 <道具名>", stream_id)
            return False, "缺少道具名", True

        assert self._db is not None
        cursor = self._db.execute(
            "UPDATE shop_items SET is_on_sale = 0 WHERE name = ? AND scope = 'group' AND group_id = ?",
            (item_name, group_id),
        )
        if cursor.rowcount == 0:
            await self.ctx.send.text(f"未找到本群道具「{item_name}」", stream_id)
            return False, "道具不存在", True
        self._db.commit()

        await self.ctx.send.text(f"✅ 本群道具「{item_name}」已下架", stream_id)
        return True, f"群下架{item_name}", True

    # ---- 定时任务 ----

    async def _attr_decay_loop(self) -> None:
        """每小时衰减 bot 属性。"""
        while self._running:
            try:
                await asyncio.sleep(3600)
                if not self._running or not self._db:
                    break

                # 衰减饱食度
                satiety_decay = self.config.bot_attr.satiety_decay_rate
                self._add_attr("satiety", -satiety_decay)

                # 衰减好感度
                affection_decay = self.config.bot_attr.affection_decay_rate
                self._add_attr("affection", -affection_decay)

                self.ctx.logger.debug(
                    f"属性衰减：饱食度-{satiety_decay}，好感度-{affection_decay}"
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.ctx.logger.error(f"属性衰减任务异常: {e}")
                await asyncio.sleep(60)

    async def _seek_feed_loop(self) -> None:
        """定期检查饱食度并触发求投喂。"""
        while self._running:
            try:
                await asyncio.sleep(1800)
                if not self._running or not self._db:
                    break

                satiety = self._get_attr("satiety")
                threshold = self.config.bot_attr.seek_feed_threshold

                if satiety >= threshold:
                    continue

                # 根据饱食度生成求喂消息
                if satiety < 10:
                    seek_msg = "呜呜...好饿好饿...有没有人投喂我呀？🥺"
                elif satiety < 20:
                    seek_msg = "肚子咕咕叫了...能投喂我一些吃的吗？😢"
                elif satiety < 30:
                    seek_msg = "有点想吃东西了...有人愿意投喂我吗？🥺"
                else:
                    seek_msg = "虽然还不算太饿，但如果有人投喂我就好了~"

                # 如果开启了 LLM，尝试生成更自然的求喂消息
                if self.config.llm.enabled:
                    try:
                        llm_result = await self.ctx.llm.generate(
                            prompt=f"你是一个可爱的聊天机器人，当前饱食度{satiety:.0f}/100，很饿。"
                            f"请用1-2句简短可爱的语气向群友们撒娇求投喂，可以包含emoji。"
                            f"不要重复之前的求喂方式，要多样化。",
                            model=self.config.llm.model,
                            temperature=0.9,
                            max_tokens=80,
                        )
                        if isinstance(llm_result, dict) and llm_result.get("response"):
                            generated = llm_result["response"].strip()
                            if generated:
                                seek_msg = generated
                    except Exception as e:
                        self.ctx.logger.warning(f"LLM生成求喂消息失败: {e}")

                # 获取所有群聊天流并发送求喂消息
                try:
                    group_streams = await self.ctx.chat.get_group_streams()
                    if not group_streams:
                        continue

                    min_interval = self.config.bot_attr.seek_feed_interval_hours * 3600
                    now = time.time()

                    for stream in group_streams:
                        if not isinstance(stream, dict):
                            continue

                        stream_group_id = str(stream.get("group_id", ""))
                        stream_session_id = str(stream.get("session_id", ""))

                        if not stream_group_id or not stream_session_id:
                            continue

                        # 检查黑白名单
                        if not self._check_permission(stream_group_id, "", True):
                            continue

                        # 检查该群的求喂间隔
                        assert self._db is not None
                        cursor = self._db.execute(
                            "SELECT last_seek_feed_time FROM feed_groups WHERE group_id = ?",
                            (stream_group_id,),
                        )
                        row = cursor.fetchone()

                        if row and row[0] > 0 and (now - row[0]) < min_interval:
                            continue

                        # 发送求喂消息
                        try:
                            await self.ctx.send.text(seek_msg, stream_session_id)
                            # 更新最后求喂时间
                            self._db.execute(
                                """
                                INSERT INTO feed_groups (group_id, enabled, last_seek_feed_time, created_at)
                                VALUES (?, 1, ?, ?)
                                ON CONFLICT(group_id) DO UPDATE SET last_seek_feed_time = ?
                                """,
                                (stream_group_id, now, now, now),
                            )
                            self._db.commit()
                        except Exception as e:
                            self.ctx.logger.warning(f"群{stream_group_id}发送求喂消息失败: {e}")

                except Exception as e:
                    self.ctx.logger.error(f"获取群聊天流失败: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.ctx.logger.error(f"求投喂任务异常: {e}")
                await asyncio.sleep(60)

    # ---- LLM 回复生成 ----

    async def _generate_feed_reply(
        self,
        user_nickname: str,
        item_name: str,
        item_emoji: str,
        feed_reply_hint: str,
        satiety: float,
        affection: float,
        recent_feeds: list[tuple[Any, ...]],
    ) -> str:
        """根据投喂上下文生成个性化回复。"""
        if not self.config.llm.enabled:
            return self.config.llm.fallback_reply

        # 构造最近投喂历史摘要
        recent_text = ""
        if recent_feeds:
            parts = []
            for feed_name, feed_emoji, feed_reply in recent_feeds:
                display = f"{feed_emoji}{feed_name}" if feed_emoji else feed_name
                short = feed_reply[:20] + "..." if feed_reply and len(feed_reply) > 20 else (feed_reply or "")
                parts.append(f"{display}" + (f"({short})" if short else ""))
            recent_text = "、".join(parts)

        # 构造 prompt
        prompt_parts = [
            f"你是一个可爱的聊天机器人，刚刚被{user_nickname}投喂了{item_emoji}{item_name}。",
            f"当前你的状态：饱食度{satiety:.0f}/100，好感度{affection:.0f}/100。",
        ]
        if feed_reply_hint:
            prompt_parts.append(f"投喂提示：{feed_reply_hint}")
        if recent_text:
            prompt_parts.append(f"最近被投喂了：{recent_text}")
        prompt_parts.append("请用简短可爱的语气回应这次投喂，1-2句话即可，可以包含emoji。不要重复之前说过的话。")

        prompt = "\n".join(prompt_parts)

        try:
            result = await self.ctx.llm.generate(
                prompt=prompt,
                model=self.config.llm.model,
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


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _extract_nickname(message: dict[str, Any] | None) -> str:
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


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------


def create_plugin() -> FeedBotPlugin:
    """创建投喂插件实例。"""
    return FeedBotPlugin()
