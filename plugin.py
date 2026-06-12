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
    satiety_decay_rate: float = Field(default=0.5, description="饱食度每小时衰减量")
    seek_feed_threshold: float = Field(default=30.0, description="饱食度低于此值触发求投喂")


class FilterConfig(PluginConfigBase):
    """触发控制配置。"""

    __ui_label__ = "触发控制"
    __ui_icon__ = "filter"
    __ui_order__ = 4

    group_admins: list[dict[str, str]] = Field(
        default_factory=list,
        description="群管理员配置，每项包含 group_id（群号）和 admin_users（管理员QQ，空格/逗号/|分隔）",
    )


class LLMConfig(PluginConfigBase):
    """LLM回复配置。"""

    __ui_label__ = "LLM回复"
    __ui_icon__ = "brain"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="是否使用LLM生成投喂回复")
    model: str = Field(default="replyer", description="LLM模型任务名（默认使用MaiBot的replyer模型）")
    temperature: float = Field(default=0.8, description="LLM温度（越高越随机）")
    max_tokens: int = Field(default=300, description="LLM最大token数（含reasoning token）")
    fallback_reply: str = Field(default="谢谢你投喂我！好开心~", description="LLM不可用时的兜底回复")

    @property
    def effective_model(self) -> str:
        """获取实际使用的模型任务名，空字符串时默认使用 replyer。"""
        return self.model.strip() or "replyer"


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
        self._migrate_per_group_data()

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
                satiety REAL NOT NULL DEFAULT -1,
                last_seek_feed_time REAL NOT NULL DEFAULT 0,
                seek_feed_message TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
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

    def _migrate_per_group_data(self) -> None:
        """将旧的无群号前缀的用户数据迁移为带群号前缀的格式。"""
        assert self._db is not None

        # 收集所有群号
        group_ids = set()
        cursor = self._db.execute("SELECT group_id FROM feed_groups")
        for row in cursor.fetchall():
            if row[0]:
                group_ids.add(row[0])
        cursor = self._db.execute("SELECT DISTINCT group_id FROM feed_records WHERE group_id != ''")
        for row in cursor.fetchall():
            if row[0]:
                group_ids.add(row[0])

        if not group_ids:
            return

        migrated = False
        for gid in group_ids:
            prefix = f"{gid}:"
            # 跳过已有前缀记录的群
            cursor = self._db.execute(
                "SELECT 1 FROM users WHERE user_id LIKE ? LIMIT 1",
                (prefix + "%",),
            )
            if cursor.fetchone():
                continue

            # 从 feed_records 获取该群投喂过的旧用户（纯QQ号，无冒号前缀）
            old_users = set()
            cursor = self._db.execute(
                "SELECT DISTINCT user_id FROM feed_records WHERE group_id = ? AND user_id NOT LIKE ?",
                (gid, prefix + "%"),
            )
            for row in cursor.fetchall():
                old_users.add(row[0])

            for old_uid in old_users:
                new_uid = f"{gid}:{old_uid}"
                # 检查新记录是否已存在
                cursor = self._db.execute(
                    "SELECT 1 FROM users WHERE user_id = ?",
                    (new_uid,),
                )
                if cursor.fetchone():
                    continue

                # 复制 users 记录（不改原记录，因为可能属于其他群）
                cursor = self._db.execute(
                    "SELECT nickname, points, total_sign_days, consecutive_sign_days, last_sign_time, total_feed_count, created_at FROM users WHERE user_id = ?",
                    (old_uid,),
                )
                src = cursor.fetchone()
                if src:
                    self._db.execute(
                        """
                        INSERT OR IGNORE INTO users (user_id, nickname, points, total_sign_days,
                                                      consecutive_sign_days, last_sign_time,
                                                      total_feed_count, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (new_uid, src[0], src[1], src[2], src[3], src[4], src[5], src[6]),
                    )

                # 复制 user_inventory（只复制该群可见的道具）
                cursor = self._db.execute(
                    "SELECT item_id, quantity FROM user_inventory WHERE user_id = ?",
                    (old_uid,),
                )
                for inv_row in cursor.fetchall():
                    self._db.execute(
                        """
                        INSERT OR IGNORE INTO user_inventory (user_id, item_id, quantity)
                        VALUES (?, ?, ?)
                        """,
                        (new_uid, inv_row[0], inv_row[1]),
                    )

                # 迁移 feed_records（只迁移该群的）
                self._db.execute(
                    "UPDATE feed_records SET user_id = ? WHERE user_id = ? AND group_id = ?",
                    (new_uid, old_uid, gid),
                )

                migrated = True

        if migrated:
            self._db.commit()
            self.ctx.logger.info("per_group 数据迁移完成")

    # ---- 权限与过滤 ----

    def _is_bot_admin(self, user_id: str) -> bool:
        """判断是否为Bot管理员。"""
        return user_id in self.config.admin.admin_users

    def _is_group_admin(self, group_id: str, user_id: str) -> bool:
        """判断是否为指定群的管理员（含Bot管理员）。"""
        if self._is_bot_admin(user_id):
            return True
        # 检查配置中的群管理员
        for item in self.config.filter.group_admins:
            if not isinstance(item, dict):
                continue
            if str(item.get("group_id", "")) == group_id:
                raw = str(item.get("admin_users", "") or "")
                if user_id in self._parse_admin_list(raw):
                    return True
        return False

    def _parse_admin_list(self, raw: str) -> list[str]:
        """解析管理员列表字符串，支持空格、逗号、|分隔。"""
        return [s.strip() for s in raw.replace(",", " ").replace("|", " ").split() if s.strip()]

    # ---- 内部方法 ----

    def _enabled_group_ids(self) -> set[str]:
        """返回配置中授权的所有群号。"""
        result = set()
        for item in self.config.filter.group_admins:
            if isinstance(item, dict):
                gid = str(item.get("group_id", "") or "").strip()
                if gid:
                    result.add(gid)
        return result

    def _is_group_enabled(self, group_id: str) -> bool:
        """判断群是否在配置中授权。"""
        return group_id in self._enabled_group_ids()

    def _check_group_enabled(self, group_id: str) -> bool:
        """群聊时检查群是否授权，私聊时放行。未授权群静默忽略。"""
        if group_id and not self._is_group_enabled(group_id):
            return False
        return True

    def _save_config_to_file(self) -> None:
        """将当前配置写入 config.toml 文件。"""
        try:
            config_path = os.path.join(PLUGIN_DIR, "config.toml")
            lines = []
            # [plugin]
            lines.append("[plugin]")
            lines.append(f"enabled = {'true' if self.config.plugin.enabled else 'false'}")
            lines.append(f'config_version = "{self.config.plugin.config_version}"')
            lines.append("")
            # [admin]
            lines.append("[admin]")
            admin_users = self.config.admin.admin_users
            lines.append(f"admin_users = {self._toml_list(admin_users)}")
            lines.append("")
            # [sign]
            lines.append("[sign]")
            lines.append(f"base_points = {self.config.sign.base_points}")
            lines.append(f"consecutive_bonus = {self.config.sign.consecutive_bonus}")
            lines.append(f"max_consecutive_bonus = {self.config.sign.max_consecutive_bonus}")
            lines.append("")
            # [bot_attr]
            lines.append("[bot_attr]")
            lines.append(f"initial_satiety = {self.config.bot_attr.initial_satiety}")
            lines.append(f"satiety_decay_rate = {self.config.bot_attr.satiety_decay_rate}")
            lines.append(f"seek_feed_threshold = {self.config.bot_attr.seek_feed_threshold}")
            lines.append("")
            # [filter]
            lines.append("[filter]")
            if self.config.filter.group_admins:
                for item in self.config.filter.group_admins:
                    if isinstance(item, dict):
                        gid = str(item.get("group_id", "") or "")
                        raw = str(item.get("admin_users", "") or "")
                        lines.append(f"[[filter.group_admins]]")
                        lines.append(f'group_id = "{gid}"')
                        lines.append(f'admin_users = "{raw}"')
            else:
                lines.append("group_admins = []")
            lines.append("")
            # [llm]
            lines.append("[llm]")
            lines.append(f"enabled = {'true' if self.config.llm.enabled else 'false'}")
            lines.append(f'model = "{self.config.llm.model}"')
            lines.append(f"temperature = {self.config.llm.temperature}")
            lines.append(f"max_tokens = {self.config.llm.max_tokens}")
            lines.append(f'fallback_reply = "{self.config.llm.fallback_reply}"')
            lines.append("")

            with open(config_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:
            self.ctx.logger.warning(f"保存配置文件失败: {e}")

    @staticmethod
    def _toml_list(items: list[str]) -> str:
        """将字符串列表格式化为 TOML 数组。"""
        if not items:
            return "[]"
        return '[' + ", ".join(f'"{i}"' for i in items) + ']'

    def _add_group_admin_to_config(self, target_group_id: str, target_user: str) -> None:
        """在配置中添加群管理员，若群不存在则创建条目。"""
        admins = self.config.filter.group_admins
        for item in admins:
            if isinstance(item, dict) and str(item.get("group_id", "")) == target_group_id:
                # 群已存在，追加管理员
                raw = str(item.get("admin_users", "") or "")
                existing = self._parse_admin_list(raw)
                if target_user not in existing:
                    existing.append(target_user)
                    item["admin_users"] = ", ".join(existing)
                self._save_config_to_file()
                return
        # 群不存在，创建新条目
        admins.append({"group_id": target_group_id, "admin_users": target_user})
        self._save_config_to_file()

    def _remove_group_admin_from_config(self, target_group_id: str, target_user: str) -> None:
        """在配置中移除群管理员。"""
        admins = self.config.filter.group_admins
        for item in admins:
            if isinstance(item, dict) and str(item.get("group_id", "")) == target_group_id:
                raw = str(item.get("admin_users", "") or "")
                existing = self._parse_admin_list(raw)
                if target_user in existing:
                    existing.remove(target_user)
                    item["admin_users"] = ", ".join(existing)
                self._save_config_to_file()
                return

    def _user_key(self, user_id: str, group_id: str = "") -> str:
        """返回群隔离的用户键 '群号:QQ号'。"""
        if group_id:
            return f"{group_id}:{user_id}"
        return user_id

    def _ensure_user(self, user_id: str, nickname: str, group_id: str = "") -> None:
        """确保用户存在于数据库中。per_group 模式下按群隔离。"""
        uid = self._user_key(user_id, group_id)
        assert self._db is not None
        cursor = self._db.execute(
            "SELECT 1 FROM users WHERE user_id = ?",
            (uid,),
        )
        if cursor.fetchone() is None:
            self._db.execute(
                """
                INSERT INTO users (user_id, nickname, points, total_sign_days,
                                   consecutive_sign_days, last_sign_time,
                                   total_feed_count, created_at)
                VALUES (?, ?, 0, 0, 0, 0, 0, ?)
                """,
                (uid, nickname, time.time()),
            )
            self._db.commit()
        elif nickname:
            # 更新昵称
            self._db.execute(
                "UPDATE users SET nickname = ? WHERE user_id = ? AND nickname != ?",
                (nickname, uid, nickname),
            )
            self._db.commit()

    def _get_satiety(self, group_id: str = "") -> float:
        """获取群饱食度。如果群未初始化则自动创建记录。"""
        assert self._db is not None
        if group_id:
            cursor = self._db.execute(
                "SELECT satiety FROM feed_groups WHERE group_id = ?",
                (group_id,),
            )
            row = cursor.fetchone()
            if row is not None and row[0] >= 0:
                return float(row[0])
            # 群未初始化饱食度，自动创建记录并写入初始值
            initial = self.config.bot_attr.initial_satiety
            self._db.execute(
                """
                INSERT INTO feed_groups (group_id, enabled, satiety, last_seek_feed_time, created_at)
                VALUES (?, 1, ?, 0, ?)
                ON CONFLICT(group_id) DO UPDATE SET satiety = ?
                """,
                (group_id, initial, time.time(), initial),
            )
            self._db.commit()
            return initial
        # 私聊无群号时，从全局属性读取
        cursor = self._db.execute(
            "SELECT attr_value FROM bot_attributes WHERE attr_key = 'satiety'",
        )
        row = cursor.fetchone()
        return float(row[0]) if row else 0.0

    def _set_satiety(self, value: float, group_id: str = "") -> None:
        """设置饱食度（钳位到 0-100）。"""
        assert self._db is not None
        value = max(0.0, min(100.0, value))
        if group_id:
            self._db.execute(
                """
                INSERT INTO feed_groups (group_id, enabled, satiety, last_seek_feed_time, created_at)
                VALUES (?, 1, ?, 0, ?)
                ON CONFLICT(group_id) DO UPDATE SET satiety = ?
                """,
                (group_id, value, time.time(), value),
            )
            self._db.commit()
        else:
            self._db.execute(
                "UPDATE bot_attributes SET attr_value = ?, last_update_time = ? WHERE attr_key = 'satiety'",
                (value, time.time()),
            )
            self._db.commit()

    def _find_shop_item(self, item_name: str, group_id: str) -> dict[str, Any] | None:
        """查找道具：优先本群专属，其次全局。"""
        assert self._db is not None

        # 优先查找群内道具
        if group_id:
            cursor = self._db.execute(
                """
                SELECT item_id, name, emoji, description, price, feed_reply_hint,
                       category, satiety_bonus, scope, group_id
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
                   category, satiety_bonus, scope, group_id
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
            "scope": row[8],
            "group_id": row[9],
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
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        # 提取昵称
        nickname = _extract_nickname(message)
        self._ensure_user(user_id, nickname, group_id)

        assert self._db is not None
        uid = self._user_key(user_id, group_id)

        # 检查今天是否已签到
        now = time.time()
        cursor = self._db.execute(
            "SELECT last_sign_time, consecutive_sign_days, total_sign_days FROM users WHERE user_id = ?",
            (uid,),
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
            (earned, total_days, consecutive_days, now, uid),
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
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        nickname = _extract_nickname(message)
        self._ensure_user(user_id, nickname, group_id)

        assert self._db is not None
        uid = self._user_key(user_id, group_id)
        cursor = self._db.execute(
            "SELECT points, total_sign_days, consecutive_sign_days FROM users WHERE user_id = ?",
            (uid,),
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
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        assert self._db is not None
        prefix = f"{group_id}:"
        cursor = self._db.execute(
            """
            SELECT nickname, user_id, points
            FROM users
            WHERE user_id LIKE ? AND points > 0
            ORDER BY points DESC LIMIT 10
            """,
            (prefix + "%",),
        )
        rows = cursor.fetchall()

        if not rows:
            await self.ctx.send.text("暂无积分记录，快来 /签到 吧！", stream_id)
            return True, "无积分记录", True

        lines = ["🏆 积分排行榜"]
        for i, (nickname, uid, pts) in enumerate(rows, 1):
            # per_group 模式下去掉群号前缀
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

        assert self._db is not None

        # 查询全局道具
        global_cursor = self._db.execute(
            """
            SELECT name, emoji, price, description, category, satiety_bonus
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
                SELECT name, emoji, price, description, category, satiety_bonus
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
            for name, emoji, price, desc, _cat, satiety_bonus in global_items:
                display = f"{emoji}{name}" if emoji else name
                desc_part = f" — {desc}" if desc else ""
                lines.append(f"  {display} {price}积分 饱食度{satiety_bonus:+}{desc_part}")

        if group_items:
            lines.append("")
            lines.append("🏠 本群专属")
            for name, emoji, price, desc, _cat, satiety_bonus in group_items:
                display = f"{emoji}{name}" if emoji else name
                desc_part = f" — {desc}" if desc else ""
                lines.append(f"  {display} {price}积分 饱食度{satiety_bonus:+}{desc_part}")

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
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        # 获取道具名
        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/购买 <道具名>", stream_id)
            return False, "缺少道具名", True

        nickname = _extract_nickname(message)
        self._ensure_user(user_id, nickname, group_id)

        assert self._db is not None
        uid = self._user_key(user_id, group_id)

        # 查找道具
        item = self._find_shop_item(item_name, group_id)
        if not item:
            await self.ctx.send.text(f"没有找到道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        # 检查积分
        cursor = self._db.execute(
            "SELECT points FROM users WHERE user_id = ?",
            (uid,),
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
            (item["price"], uid),
        )
        self._db.execute(
            """
            INSERT INTO user_inventory (user_id, item_id, quantity)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, item_id) DO UPDATE SET quantity = quantity + 1
            """,
            (uid, item["item_id"]),
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
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        assert self._db is not None
        uid = self._user_key(user_id, group_id)
        cursor = self._db.execute(
            """
            SELECT si.name, si.emoji, si.satiety_bonus, inv.quantity
            FROM user_inventory inv
            JOIN shop_items si ON inv.item_id = si.item_id
            WHERE inv.user_id = ? AND inv.quantity > 0
            ORDER BY si.name ASC
            """,
            (uid,),
        )
        rows = cursor.fetchall()

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
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/投喂 <道具名>", stream_id)
            return False, "缺少道具名", True

        nickname = _extract_nickname(message)
        self._ensure_user(user_id, nickname, group_id)

        assert self._db is not None
        uid = self._user_key(user_id, group_id)

        # 查找背包中的道具
        cursor = self._db.execute(
            """
            SELECT inv.item_id, inv.quantity, si.name, si.emoji, si.feed_reply_hint,
                   si.satiety_bonus
            FROM user_inventory inv
            JOIN shop_items si ON inv.item_id = si.item_id
            WHERE inv.user_id = ? AND si.name = ? AND inv.quantity > 0
            """,
            (uid, item_name),
        )
        row = cursor.fetchone()

        if not row:
            await self.ctx.send.text(
                f"你没有「{item_name}」哦～去 /商店 购买或检查 /背包", stream_id
            )
            return False, "背包无此道具", True

        item_id, _qty, name, emoji, reply_hint, satiety_bonus = row

        # 检查饱食度是否已满
        current_satiety = self._get_satiety(group_id)
        if current_satiety >= 100:
            await self.ctx.send.text("我已经吃饱了，吃不下啦～等饿一点再喂我吧！", stream_id)
            return False, "饱食度已满", True

        # 扣减背包
        self._db.execute(
            "UPDATE user_inventory SET quantity = quantity - 1 WHERE user_id = ? AND item_id = ?",
            (uid, item_id),
        )
        # 清理数量为0的记录
        self._db.execute(
            "DELETE FROM user_inventory WHERE user_id = ? AND item_id = ? AND quantity <= 0",
            (uid, item_id),
        )

        # 增加 bot 饱食度（不超过100）
        new_satiety = min(100.0, current_satiety + satiety_bonus)
        self._set_satiety(new_satiety, group_id)

        # 更新用户投喂计数
        self._db.execute(
            "UPDATE users SET total_feed_count = total_feed_count + 1 WHERE user_id = ?",
            (uid,),
        )

        # 获取最近投喂记录（用于 LLM 生成）
        recent_cursor = self._db.execute(
            """
            SELECT item_name, item_emoji, reply_text
            FROM feed_records
            WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 3
            """,
            (uid,),
        )
        recent_feeds = recent_cursor.fetchall()

        # 生成 LLM 回复
        reply = await self._generate_feed_reply(
            user_nickname=nickname or user_id,
            item_name=name,
            item_emoji=emoji,
            feed_reply_hint=reply_hint,
            satiety=new_satiety,
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
            (uid, nickname, group_id, item_id, name, emoji, reply, now),
        )
        self._db.commit()

        # 发送回复
        display_item = f"{emoji}{name}" if emoji else name
        satiety_change = new_satiety - current_satiety
        await self.ctx.send.text(
            f"{nickname} 投喂了 {display_item} 给我～饱食度 +{satiety_change:.0f}（{new_satiety:.0f}/100）\n{reply}",
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
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        assert self._db is not None
        uid = self._user_key(user_id, group_id)
        cursor = self._db.execute(
            """
            SELECT item_name, item_emoji, reply_text, created_at
            FROM feed_records
            WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 10
            """,
            (uid,),
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
        if not self._check_group_enabled(group_id):
            return False, "群未授权", True

        satiety = self._get_satiety(group_id)

        # 根据饱食度生成状态描述
        if satiety >= 80:
            desc = "饱饱的～好满足"
        elif satiety >= 50:
            desc = "还行，不太饿"
        elif satiety >= 30:
            desc = "有点饿了..."
        else:
            desc = "好饿好饿！快投喂我！"

        scope_hint = "（本群）" if group_id else ""
        msg = f"🍖 当前饱食度{scope_hint}：{satiety:.0f}/100 — {desc}"
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

        assert self._db is not None
        prefix = f"{group_id}:"
        cursor = self._db.execute(
            """
            SELECT u.nickname, u.user_id, u.total_feed_count
            FROM users u
            WHERE u.user_id LIKE ? AND u.total_feed_count > 0
            ORDER BY u.total_feed_count DESC LIMIT 10
            """,
            (prefix + "%",),
        )
        rows = cursor.fetchall()

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

    # ---- Bot管理员命令 ----

    @Command(
        "admin_global_add_item",
        description="上架全局道具（Bot管理员）",
        pattern=r"^/投喂管理\s+全局上架\s+(?P<name>\S+)\s+(?P<price>\d+)(?:\s+(?P<rest>.+))?$",
    )
    async def handle_admin_global_add_item(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 全局上架 命令。"""
        if group_id:
            await self.ctx.send.text("Bot管理员命令请私聊Bot使用", stream_id)
            return False, "非私聊", True
        if not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        name = str(matched_groups.get("name") or "").strip()
        price_str = str(matched_groups.get("price") or "").strip()
        rest = str(matched_groups.get("rest") or "").strip()

        if not name or not price_str:
            await self.ctx.send.text("用法：/投喂管理 全局上架 <名称> <价格> [emoji] [描述] [饱食度N]", stream_id)
            return False, "参数缺失", True

        try:
            price = int(price_str)
        except ValueError:
            await self.ctx.send.text("价格必须是整数", stream_id)
            return False, "价格格式错误", True

        # 从剩余文本中提取饱食度和描述
        satiety_bonus = 5.0
        emoji = ""
        desc = ""
        if rest:
            import re as _re
            satiety_match = _re.search(r"饱食度(-?\d+(?:\.\d+)?)", rest)
            if satiety_match:
                satiety_bonus = float(satiety_match.group(1))
                rest = rest[:satiety_match.start()] + rest[satiety_match.end():]
                rest = rest.strip()
            if rest:
                tokens = rest.split(None, 1)
                # 判断第一个token是否为emoji（非ASCII字符且长度短）
                first = tokens[0]
                if len(first) <= 2 and not first.isascii():
                    emoji = first
                    desc = tokens[1].strip() if len(tokens) > 1 else ""
                else:
                    desc = rest

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
                                     is_on_sale, satiety_bonus, created_by, created_at)
            VALUES (?, ?, ?, ?, 'global', '', 1, ?, ?, ?)
            """,
            (name, emoji, desc, price, satiety_bonus, user_id, time.time()),
        )
        self._db.commit()

        display = f"{emoji}{name}" if emoji else name
        await self.ctx.send.text(
            f"✅ 全局道具「{display}」已上架，价格：{price}积分，饱食度{satiety_bonus:+}", stream_id
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
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 全局下架 命令。"""
        if group_id:
            await self.ctx.send.text("Bot管理员命令请私聊Bot使用", stream_id)
            return False, "非私聊", True
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
        "admin_modify_satiety",
        description="修改道具饱食度（管理员）",
        pattern=r"^/投喂管理\s+修改饱食度\s+(?P<item_name>.+?)\s+(?P<satiety>-?\d+(?:\.\d+)?)$",
    )
    async def handle_admin_modify_satiety(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 修改饱食度 命令。"""
        if group_id:
            if not self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        item_name = str(matched_groups.get("item_name") or "").strip()
        satiety_str = str(matched_groups.get("satiety") or "").strip()

        if not item_name or not satiety_str:
            await self.ctx.send.text("用法：/投喂管理 修改饱食度 <道具名> <饱食度>", stream_id)
            return False, "参数缺失", True

        try:
            satiety_bonus = float(satiety_str)
        except ValueError:
            await self.ctx.send.text("饱食度必须是数字", stream_id)
            return False, "饱食度格式错误", True

        assert self._db is not None
        cursor = self._db.execute(
            "UPDATE shop_items SET satiety_bonus = ? WHERE name = ? AND is_on_sale = 1",
            (satiety_bonus, item_name),
        )
        if cursor.rowcount == 0:
            await self.ctx.send.text(f"未找到在售道具「{item_name}」", stream_id)
            return False, "道具不存在", True
        self._db.commit()

        await self.ctx.send.text(f"✅ 道具「{item_name}」饱食度已修改为{satiety_bonus:+}", stream_id)
        return True, f"修改道具饱食度{item_name}", True

    @Command(
        "admin_points",
        description="调整用户积分（管理员）",
        pattern=r"^/投喂管理\s+积分\s+(?P<target_user>\S+)\s+(?P<amount>-?\d+)(?:\s+(?P<target_group>\S+))?$",
    )
    async def handle_admin_points(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 积分 命令。"""
        if group_id:
            if not self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        target_user = str(matched_groups.get("target_user") or "").strip()
        amount_str = str(matched_groups.get("amount") or "").strip()
        target_group = str(matched_groups.get("target_group") or "").strip()

        if not target_user or not amount_str:
            await self.ctx.send.text("用法：/投喂管理 积分 <QQ号> <数量> [群号]", stream_id)
            return False, "参数缺失", True

        # 确定目标群号
        effective_group = target_group or group_id
        if not effective_group:
            await self.ctx.send.text("需要指定群号：/投喂管理 积分 <QQ号> <数量> <群号>", stream_id)
            return False, "缺少群号", True

        try:
            amount = int(amount_str)
        except ValueError:
            await self.ctx.send.text("数量必须是整数", stream_id)
            return False, "数量格式错误", True

        assert self._db is not None

        # 确保目标用户存在
        uid = self._user_key(target_user, effective_group)
        self._ensure_user(target_user, "", effective_group)

        self._db.execute(
            "UPDATE users SET points = MAX(0, points + ?) WHERE user_id = ?",
            (amount, uid),
        )
        self._db.commit()

        action = "增加" if amount >= 0 else "减少"
        await self.ctx.send.text(
            f"✅ 已为 {target_user} {action} {abs(amount)} 积分", stream_id
        )
        return True, f"调整积分{amount}", True

    @Command(
        "admin_attr",
        description="设置Bot属性（管理员）",
        pattern=r"^/投喂管理\s+属性\s+(?P<attr_key>\S+)\s+(?P<attr_value>\d+\.?\d*)$",
    )
    async def handle_admin_attr(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 属性 命令。"""
        if group_id:
            if not self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        attr_key = str(matched_groups.get("attr_key") or "").strip()
        attr_value_str = str(matched_groups.get("attr_value") or "").strip()

        if not attr_key or not attr_value_str:
            await self.ctx.send.text("用法：/投喂管理 属性 satiety <值>", stream_id)
            return False, "参数缺失", True

        if attr_key != "satiety":
            await self.ctx.send.text("无效属性名，当前仅支持：satiety", stream_id)
            return False, "无效属性名", True

        try:
            attr_value = float(attr_value_str)
        except ValueError:
            await self.ctx.send.text("属性值必须是数字", stream_id)
            return False, "属性值格式错误", True

        self._set_satiety(attr_value, group_id)

        await self.ctx.send.text(
            f"✅ 饱食度已设置为 {attr_value:.0f}", stream_id
        )
        return True, f"设置属性{attr_key}", True

    @Command(
        "admin_reset_sign",
        description="重置用户签到（管理员）",
        pattern=r"^/投喂管理\s+重置签到\s+(?P<target_user>\S+)(?:\s+(?P<target_group>\S+))?$",
    )
    async def handle_admin_reset_sign(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 重置签到 命令。"""
        if group_id:
            if not self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        target_user = str(matched_groups.get("target_user") or "").strip()
        target_group = str(matched_groups.get("target_group") or "").strip()

        if not target_user:
            await self.ctx.send.text("用法：/投喂管理 重置签到 <QQ号> [群号]", stream_id)
            return False, "参数缺失", True

        # 确定目标群号
        effective_group = target_group or group_id
        if not effective_group:
            await self.ctx.send.text("需要指定群号：/投喂管理 重置签到 <QQ号> <群号>", stream_id)
            return False, "缺少群号", True

        assert self._db is not None
        uid = self._user_key(target_user, effective_group)
        self._db.execute(
            """
            UPDATE users SET total_sign_days = 0, consecutive_sign_days = 0, last_sign_time = 0
            WHERE user_id = ?
            """,
            (uid,),
        )
        self._db.commit()

        await self.ctx.send.text(f"✅ 已重置 {target_user} 的签到记录", stream_id)
        return True, f"重置签到{target_user}", True

    @Command(
        "admin_grant",
        description="授权群管理员（管理员）",
        pattern=r"^/投喂管理\s+授权\s+(?P<group_id>\S+)\s+(?P<target_user>\S+)$",
    )
    async def handle_admin_grant(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 授权 命令。"""
        if group_id:
            if not self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
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

        # 群聊中只能授权当前群
        if group_id and target_group_id != group_id:
            await self.ctx.send.text("群聊中只能授权当前群的管理员", stream_id)
            return False, "跨群授权", True

        self._add_group_admin_to_config(target_group_id, target_user)

        await self.ctx.send.text(
            f"✅ 已授权 {target_user} 为群 {target_group_id} 的管理员", stream_id
        )
        return True, f"授权群管理员{target_user}", True

    @Command(
        "admin_revoke",
        description="取消群管理员授权（管理员）",
        pattern=r"^/投喂管理\s+取消授权\s+(?P<group_id>\S+)\s+(?P<target_user>\S+)$",
    )
    async def handle_admin_revoke(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 取消授权 命令。"""
        if group_id:
            if not self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
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

        # 群聊中只能取消授权当前群
        if group_id and target_group_id != group_id:
            await self.ctx.send.text("群聊中只能取消授权当前群的管理员", stream_id)
            return False, "跨群取消授权", True

        self._remove_group_admin_from_config(target_group_id, target_user)

        await self.ctx.send.text(
            f"✅ 已取消 {target_user} 在群 {target_group_id} 的管理员权限", stream_id
        )
        return True, f"取消授权{target_user}", True

    # ---- 群管理员命令 ----

    @Command(
        "admin_group_add_item",
        description="上架群内道具（群管理员）",
        pattern=r"^/投喂管理\s+群上架\s+(?P<name>\S+)\s+(?P<price>\d+)(?:\s+(?P<rest>.+))?$",
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
        rest = str(matched_groups.get("rest") or "").strip()

        if not name or not price_str:
            await self.ctx.send.text("用法：/投喂管理 群上架 <名称> <价格> [emoji] [描述] [饱食度N]", stream_id)
            return False, "参数缺失", True

        try:
            price = int(price_str)
        except ValueError:
            await self.ctx.send.text("价格必须是整数", stream_id)
            return False, "价格格式错误", True

        # 从剩余文本中提取饱食度和描述
        satiety_bonus = 5.0
        emoji = ""
        desc = ""
        if rest:
            import re as _re
            satiety_match = _re.search(r"饱食度(-?\d+(?:\.\d+)?)", rest)
            if satiety_match:
                satiety_bonus = float(satiety_match.group(1))
                rest = rest[:satiety_match.start()] + rest[satiety_match.end():]
                rest = rest.strip()
            if rest:
                tokens = rest.split(None, 1)
                # 判断第一个token是否为emoji（非ASCII字符且长度短）
                first = tokens[0]
                if len(first) <= 2 and not first.isascii():
                    emoji = first
                    desc = tokens[1].strip() if len(tokens) > 1 else ""
                else:
                    desc = rest

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
                                     is_on_sale, satiety_bonus, created_by, created_at)
            VALUES (?, ?, ?, ?, 'group', ?, 1, ?, ?, ?)
            """,
            (name, emoji, desc, price, group_id, satiety_bonus, user_id, time.time()),
        )
        self._db.commit()

        display = f"{emoji}{name}" if emoji else name
        await self.ctx.send.text(
            f"✅ 本群道具「{display}」已上架，价格：{price}积分，饱食度{satiety_bonus:+}", stream_id
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
        """每小时衰减 bot 饱食度。"""
        while self._running:
            try:
                if not self._running or not self._db:
                    break

                satiety_decay = self.config.bot_attr.satiety_decay_rate

                # 衰减每个群的饱食度
                cursor = self._db.execute(
                    "SELECT group_id, satiety FROM feed_groups WHERE satiety > 0"
                )
                for row in cursor.fetchall():
                    gid, current = row[0], row[1]
                    new_val = max(0.0, current - satiety_decay)
                    self._db.execute(
                        "UPDATE feed_groups SET satiety = ? WHERE group_id = ?",
                        (new_val, gid),
                    )
                self._db.commit()

                self.ctx.logger.debug(f"饱食度衰减：-{satiety_decay}")
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
                await asyncio.sleep(1800)
                if not self._running or not self._db:
                    break

                threshold = self.config.bot_attr.seek_feed_threshold

                # 获取所有群聊天流
                try:
                    group_streams = await self.ctx.chat.get_group_streams()
                    if not group_streams:
                        continue

                    for stream in group_streams:
                        if not isinstance(stream, dict):
                            continue

                        stream_group_id = str(stream.get("group_id", ""))
                        stream_session_id = str(stream.get("session_id", ""))

                        if not stream_group_id or not stream_session_id:
                            continue
                        if not self._is_group_enabled(stream_group_id):
                            continue

                        # 获取该群的饱食度
                        satiety = self._get_satiety(stream_group_id)
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
            f"当前你的状态：饱食度{satiety:.0f}/100。",
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
        if feed_reply_hint:
            prompt_parts.append(f"投喂提示：{feed_reply_hint}")
        if recent_text:
            prompt_parts.append(f"最近被投喂了：{recent_text}")
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
