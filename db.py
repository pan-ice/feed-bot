"""投喂插件 — 异步数据库层。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from typing import Any

from .config import FeedBotConfig
from .utils import DB_PATH


class AsyncDatabase:
    """线程安全的异步 SQLite 数据库封装。"""

    def __init__(self, config: FeedBotConfig) -> None:
        self._config = config
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ---- 连接管理 ----

    def open(self) -> None:
        """打开数据库连接并初始化表结构（同步，由 asyncio.to_thread 调用）。"""
        self._db = sqlite3.connect(DB_PATH)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_tables()
        self._init_bot_attributes()
        self._migrate_per_group_data()

    def close(self) -> None:
        """关闭数据库连接（同步，由 asyncio.to_thread 调用）。"""
        if self._db:
            self._db.close()
            self._db = None

    @property
    def is_open(self) -> bool:
        return self._db is not None

    # ---- 异步查询方法 ----

    async def execute(
        self, sql: str, params: tuple[Any, ...] | list[Any] = ()
    ) -> list[tuple[Any, ...]]:
        """线程安全地执行 SQL 并返回 fetchall 结果。"""

        def _do() -> list[tuple[Any, ...]]:
            if self._db is None:
                raise RuntimeError("数据库未初始化")
            with self._lock:
                cursor = self._db.execute(sql, params)
                return cursor.fetchall()

        return await asyncio.to_thread(_do)

    async def fetchone(
        self, sql: str, params: tuple[Any, ...] | list[Any] = ()
    ) -> tuple[Any, ...] | None:
        """线程安全地执行 SQL 并返回 fetchone 结果。"""

        def _do() -> tuple[Any, ...] | None:
            if self._db is None:
                raise RuntimeError("数据库未初始化")
            with self._lock:
                cursor = self._db.execute(sql, params)
                return cursor.fetchone()

        return await asyncio.to_thread(_do)

    async def execute_commit(
        self, sql: str, params: tuple[Any, ...] | list[Any] = ()
    ) -> None:
        """线程安全地执行 SQL 并提交。"""

        def _do() -> None:
            if self._db is None:
                raise RuntimeError("数据库未初始化")
            with self._lock:
                self._db.execute(sql, params)
                self._db.commit()

        await asyncio.to_thread(_do)

    async def execute_rowcount(
        self, sql: str, params: tuple[Any, ...] | list[Any] = ()
    ) -> int:
        """线程安全地执行 UPDATE/DELETE 并返回影响行数。"""

        def _do() -> int:
            if self._db is None:
                raise RuntimeError("数据库未初始化")
            with self._lock:
                cursor = self._db.execute(sql, params)
                self._db.commit()
                return cursor.rowcount

        return await asyncio.to_thread(_do)

    async def commit(self) -> None:
        """线程安全地提交事务。"""

        def _do() -> None:
            if self._db is None:
                raise RuntimeError("数据库未初始化")
            with self._lock:
                self._db.commit()

        await asyncio.to_thread(_do)

    async def execute_many(
        self, sql: str, params_seq: list[tuple[Any, ...]]
    ) -> None:
        """线程安全地执行多条 SQL 并提交。"""

        def _do() -> None:
            if self._db is None:
                raise RuntimeError("数据库未初始化")
            with self._lock:
                for params in params_seq:
                    self._db.execute(sql, params)
                self._db.commit()

        await asyncio.to_thread(_do)

    # ---- 数据库初始化（同步，仅在 open() 中调用） ----

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
                created_at REAL NOT NULL
            )
            """
        )

        # 迁移：添加 last_decay_time 列（基于时间戳衰减）
        try:
            self._db.execute(
                "ALTER TABLE feed_groups ADD COLUMN last_decay_time REAL NOT NULL DEFAULT 0"
            )
            # 将现有记录的 last_decay_time 设为当前时间，避免首次加载大量衰减
            self._db.execute(
                "UPDATE feed_groups SET last_decay_time = ? WHERE last_decay_time = 0 AND satiety >= 0",
                (time.time(),),
            )
        except sqlite3.OperationalError:
            pass  # 列已存在

        self._db.commit()

    def _init_bot_attributes(self) -> None:
        """初始化 bot 属性（如果不存在）。"""
        assert self._db is not None
        now = time.time()
        defaults = [
            ("satiety", self._config.bot_attr.initial_satiety, now),
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
        group_ids: set[str] = set()
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
            old_users: set[str] = set()
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

    # ---- 数据访问方法 ----

    @staticmethod
    def user_key(user_id: str, group_id: str = "") -> str:
        """返回群隔离的用户键 '群号:QQ号'。"""
        if group_id:
            return f"{group_id}:{user_id}"
        return user_id

    async def ensure_user(self, user_id: str, nickname: str, group_id: str = "") -> None:
        """确保用户存在于数据库中。per_group 模式下按群隔离。"""
        uid = self.user_key(user_id, group_id)
        row = await self.fetchone(
            "SELECT 1 FROM users WHERE user_id = ?",
            (uid,),
        )
        if row is None:
            await self.execute_commit(
                """
                INSERT INTO users (user_id, nickname, points, total_sign_days,
                                   consecutive_sign_days, last_sign_time,
                                   total_feed_count, created_at)
                VALUES (?, ?, 0, 0, 0, 0, 0, ?)
                """,
                (uid, nickname, time.time()),
            )
        elif nickname:
            # 更新昵称
            await self.execute_commit(
                "UPDATE users SET nickname = ? WHERE user_id = ? AND nickname != ?",
                (nickname, uid, nickname),
            )

    async def apply_satiety_decay(self, group_id: str, decay_rate: float) -> float:
        """基于时间差计算并应用饱食度衰减，返回衰减后的值。"""
        row = await self.fetchone(
            "SELECT satiety, last_decay_time FROM feed_groups WHERE group_id = ?",
            (group_id,),
        )
        if row is None:
            return -1.0  # 群不存在

        satiety, last_decay_time = row[0], row[1]

        now = time.time()
        if last_decay_time > 0:
            hours_elapsed = (now - last_decay_time) / 3600.0
            decay = hours_elapsed * decay_rate
            satiety = max(0.0, satiety - decay)

        await self.execute_commit(
            "UPDATE feed_groups SET satiety = ?, last_decay_time = ? WHERE group_id = ?",
            (satiety, now, group_id),
        )
        return satiety

    async def get_satiety(self, group_id: str, config: FeedBotConfig) -> float:
        """获取群饱食度（基于时间差补偿衰减）。如果群未初始化则自动创建记录。"""
        if group_id:
            # 先应用基于时间的衰减
            satiety = await self.apply_satiety_decay(group_id, config.bot_attr.satiety_decay_rate)
            if satiety >= 0:
                return satiety
            # 群未初始化饱食度，自动创建记录并写入初始值
            initial = config.bot_attr.initial_satiety
            now = time.time()
            await self.execute_commit(
                """
                INSERT INTO feed_groups (group_id, enabled, satiety, last_seek_feed_time, last_decay_time, created_at)
                VALUES (?, 1, ?, 0, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET satiety = ?, last_decay_time = ?
                """,
                (group_id, initial, now, time.time(), initial, now),
            )
            return initial
        # 私聊无群号时，从全局属性读取
        row = await self.fetchone(
            "SELECT attr_value, last_update_time FROM bot_attributes WHERE attr_key = 'satiety'",
        )
        if not row:
            return 0.0
        # 基于时间差补偿全局饱食度衰减
        satiety, last_update = row[0], row[1]
        now = time.time()
        if last_update > 0:
            hours_elapsed = (now - last_update) / 3600.0
            decay = hours_elapsed * config.bot_attr.satiety_decay_rate
            satiety = max(0.0, satiety - decay)
            await self.execute_commit(
                "UPDATE bot_attributes SET attr_value = ?, last_update_time = ? WHERE attr_key = 'satiety'",
                (satiety, now),
            )
        return satiety

    async def set_satiety(self, value: float, group_id: str = "") -> None:
        """设置饱食度（钳位到 0-100）。"""
        value = max(0.0, min(100.0, value))
        now = time.time()
        if group_id:
            await self.execute_commit(
                """
                INSERT INTO feed_groups (group_id, enabled, satiety, last_seek_feed_time, last_decay_time, created_at)
                VALUES (?, 1, ?, 0, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET satiety = ?, last_decay_time = ?
                """,
                (group_id, value, now, time.time(), value, now),
            )
        else:
            await self.execute_commit(
                "UPDATE bot_attributes SET attr_value = ?, last_update_time = ? WHERE attr_key = 'satiety'",
                (value, now),
            )

    async def find_shop_item(self, item_name: str, group_id: str) -> dict[str, Any] | None:
        """查找道具：优先本群专属，其次全局。"""

        # 优先查找群内道具
        if group_id:
            row = await self.fetchone(
                """
                SELECT item_id, name, emoji, description, price, feed_reply_hint,
                       category, satiety_bonus, scope, group_id
                FROM shop_items
                WHERE name = ? AND scope = 'group' AND group_id = ? AND is_on_sale = 1
                """,
                (item_name, group_id),
            )
            if row:
                return self._row_to_item_dict(row)

        # 其次查找全局道具
        row = await self.fetchone(
            """
            SELECT item_id, name, emoji, description, price, feed_reply_hint,
                   category, satiety_bonus, scope, group_id
            FROM shop_items
            WHERE name = ? AND scope = 'global' AND is_on_sale = 1
            """,
            (item_name,),
        )
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
