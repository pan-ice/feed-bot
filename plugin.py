"""投喂插件 — 签到积分+商店道具+投喂Bot+定时求投喂

支持全局/群内两层商店、管理员指令和黑白名单。
"""

from __future__ import annotations

import asyncio
from typing import Any

from maibot_sdk import MaiBotPlugin

from .commands_admin import AdminCommandsMixin
from .commands_user import UserCommandsMixin
from .config import FeedBotConfig
from .db import AsyncDatabase
from .loops import LoopTasksMixin
from .utils import DATA_DIR


class FeedBotPlugin(
    MaiBotPlugin, UserCommandsMixin, AdminCommandsMixin, LoopTasksMixin
):
    """投喂插件：签到积分+商店道具+投喂Bot+定时求投喂。"""

    config_model = FeedBotConfig

    def __init__(self) -> None:
        super().__init__()
        self.db = AsyncDatabase()
        self._running: bool = False
        self._decay_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._seek_feed_task: asyncio.Task | None = None  # type: ignore[type-arg]

    # ---- 生命周期 ----

    async def on_load(self) -> None:
        """插件加载时初始化数据目录和数据库。"""
        import os
        os.makedirs(DATA_DIR, exist_ok=True)

        # 配置在 on_load 时才可用，此时传给 db
        self.db.config = self.config
        await asyncio.to_thread(self.db.open)

        self._running = True
        self._decay_task = asyncio.create_task(self._attr_decay_loop())
        self._seek_feed_task = asyncio.create_task(self._seek_feed_loop())

        self.ctx.logger.info("投喂插件已加载")

    async def _cancel_tasks(self) -> None:
        """取消并等待后台任务结束。"""
        tasks = []
        if self._decay_task:
            self._decay_task.cancel()
            tasks.append(self._decay_task)
            self._decay_task = None
        if self._seek_feed_task:
            self._seek_feed_task.cancel()
            tasks.append(self._seek_feed_task)
            self._seek_feed_task = None
        # 等待任务实际结束，避免关闭数据库时仍在操作
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def on_unload(self) -> None:
        """插件卸载时关闭数据库连接和后台任务。"""
        self._running = False
        await self._cancel_tasks()
        await asyncio.to_thread(self.db.close)
        self.ctx.logger.info("投喂插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        """配置热重载时重启后台任务以使用新配置。"""
        if scope != "self":
            return

        self.ctx.logger.info(f"投喂插件配置已更新 (v{version})，重启后台任务")

        await self._cancel_tasks()

        if self._running and self.db.is_open:
            self._decay_task = asyncio.create_task(self._attr_decay_loop())
            self._seek_feed_task = asyncio.create_task(self._seek_feed_loop())


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------


def create_plugin() -> FeedBotPlugin:
    """创建投喂插件实例。"""
    return FeedBotPlugin()
