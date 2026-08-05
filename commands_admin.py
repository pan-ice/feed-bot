"""投喂插件 — 管理员命令 Mixin。"""

from __future__ import annotations

import re
import time
from typing import Any

from maibot_sdk import Command

from .config import FeedBotConfig
from .db import AsyncDatabase
from .utils import extract_nickname, is_likely_emoji


class AdminCommandsMixin:
    """管理员命令：全局商店、群商店、积分调整、属性设置、授权管理。"""

    # 这些属性由 FeedBotPlugin 提供，类型标注仅供静态分析
    db: AsyncDatabase
    config: FeedBotConfig

    # ---- 权限与过滤 ----

    def _is_bot_admin(self, user_id: str) -> bool:
        """判断是否为Bot管理员。"""
        return user_id in self.config.admin.admin_users

    async def _is_group_admin(self, group_id: str, user_id: str) -> bool:
        """判断是否为指定群的管理员（含Bot管理员）。从数据库读取运行时授权。"""
        if self._is_bot_admin(user_id):
            return True
        admins = await self.db.get_group_admins(group_id)
        return user_id in admins

    def _enabled_group_ids(self) -> set[str]:
        """返回配置中授权的所有群号。"""
        result: set[str] = set()
        for item in self.config.filter.group_admins:
            gid = item.gid()
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

    # ---- 内存配置同步（运行时授权不写 config.toml，参照 bilibili 插件模式） ----

    def _add_group_admin_to_memory(self, target_group_id: str, target_user: str) -> None:
        """在内存配置中添加群管理员（供 _enabled_group_ids 使用，不写文件）。

        运行时授权数据持久化到数据库，config.toml 仅作为初始配置源。
        WebUI 用户修改 config.toml 后通过 on_config_update 同步到数据库。
        命令用户通过 /投喂管理 群列表 查看运行时授权。
        """
        from .config import GroupAdminEntry
        admins = self.config.filter.group_admins
        for item in admins:
            if item.gid() == target_group_id:
                existing = item.admin_list()
                if target_user not in existing:
                    existing.append(target_user)
                    item.admin_users = AsyncDatabase._serialize_admin_list(existing)
                return
        admins.append(GroupAdminEntry(group_id=target_group_id, admin_users=target_user))

    def _remove_group_admin_from_memory(self, target_group_id: str, target_user: str) -> None:
        """在内存配置中移除群管理员（不写文件）。"""
        admins = self.config.filter.group_admins
        for item in admins:
            if item.gid() == target_group_id:
                existing = item.admin_list()
                if target_user in existing:
                    existing.remove(target_user)
                    item.admin_users = AsyncDatabase._serialize_admin_list(existing)
                return

    # ---- 道具参数解析 ----

    @staticmethod
    def _parse_item_rest(rest: str) -> tuple[float, str, str]:
        """从上架命令的剩余文本中提取饱食度、emoji 和描述。"""
        satiety_bonus = 5.0
        emoji = ""
        desc = ""
        if rest:
            satiety_match = re.search(r"饱食度(-?\d+(?:\.\d+)?)", rest)
            if satiety_match:
                satiety_bonus = float(satiety_match.group(1))
                rest = rest[:satiety_match.start()] + rest[satiety_match.end():]
                rest = rest.strip()
            if rest:
                tokens = rest.split(None, 1)
                first = tokens[0]
                if is_likely_emoji(first):
                    emoji = first
                    desc = tokens[1].strip() if len(tokens) > 1 else ""
                else:
                    desc = rest
        return satiety_bonus, emoji, desc

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

        satiety_bonus, emoji, desc = self._parse_item_rest(rest)

        row = await self.db.fetchone(
            "SELECT 1 FROM shop_items WHERE name = ? AND scope = 'global' AND is_on_sale = 1",
            (name,),
        )
        if row:
            await self.ctx.send.text(f"全局道具「{name}」已存在", stream_id)
            return False, "道具已存在", True

        await self.db.execute_commit(
            """
            INSERT INTO shop_items (name, emoji, description, price, scope, group_id,
                                     is_on_sale, satiety_bonus, created_by, created_at)
            VALUES (?, ?, ?, ?, 'global', '', 1, ?, ?, ?)
            """,
            (name, emoji, desc, price, satiety_bonus, user_id, time.time()),
        )

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

        rowcount = await self.db.execute_rowcount(
            "UPDATE shop_items SET is_on_sale = 0 WHERE name = ? AND scope = 'global'",
            (item_name,),
        )
        if rowcount == 0:
            await self.ctx.send.text(f"未找到全局道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        await self.ctx.send.text(f"✅ 全局道具「{item_name}」已下架", stream_id)
        return True, f"下架全局道具{item_name}", True

    @Command(
        "admin_modify_satiety",
        description="修改道具饱食度（管理员）",
        pattern=r"^/投喂管理\s+修改饱食度\s+(?P<scope>全局|群)\s+(?P<item_name>.+?)\s+(?P<satiety>-?\d+(?:\.\d+)?)$",
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
            if not await self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        scope = str(matched_groups.get("scope") or "").strip()
        item_name = str(matched_groups.get("item_name") or "").strip()
        satiety_str = str(matched_groups.get("satiety") or "").strip()

        if not scope or not item_name or not satiety_str:
            await self.ctx.send.text("用法：/投喂管理 修改饱食度 <全局|群> <道具名> <饱食度>", stream_id)
            return False, "参数缺失", True

        try:
            satiety_bonus = float(satiety_str)
        except ValueError:
            await self.ctx.send.text("饱食度必须是数字", stream_id)
            return False, "饱食度格式错误", True

        if scope == "全局":
            rowcount = await self.db.execute_rowcount(
                "UPDATE shop_items SET satiety_bonus = ? WHERE name = ? AND scope = 'global' AND is_on_sale = 1",
                (satiety_bonus, item_name),
            )
        else:
            # 群道具：需要群号
            effective_group = group_id
            if not effective_group:
                await self.ctx.send.text("修改群道具饱食度需要在群聊中使用", stream_id)
                return False, "缺少群号", True
            rowcount = await self.db.execute_rowcount(
                "UPDATE shop_items SET satiety_bonus = ? WHERE name = ? AND scope = 'group' AND group_id = ? AND is_on_sale = 1",
                (satiety_bonus, item_name, effective_group),
            )

        if rowcount == 0:
            scope_label = "全局" if scope == "全局" else "群内"
            await self.ctx.send.text(f"未找到{scope_label}在售道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        scope_label = "全局" if scope == "全局" else "群内"
        await self.ctx.send.text(f"✅ {scope_label}道具「{item_name}」饱食度已修改为{satiety_bonus:+}", stream_id)
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
            if not await self._is_group_admin(group_id, user_id):
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

        effective_group = target_group or group_id
        if not effective_group:
            await self.ctx.send.text("需要指定群号：/投喂管理 积分 <QQ号> <数量> <群号>", stream_id)
            return False, "缺少群号", True

        try:
            amount = int(amount_str)
        except ValueError:
            await self.ctx.send.text("数量必须是整数", stream_id)
            return False, "数量格式错误", True

        uid = self.db.user_key(target_user, effective_group)
        await self.db.ensure_user(target_user, "", effective_group)

        await self.db.execute_commit(
            "UPDATE users SET points = MAX(0, points + ?) WHERE user_id = ?",
            (amount, uid),
        )

        action = "增加" if amount >= 0 else "减少"
        await self.ctx.send.text(
            f"✅ 已为 {target_user} {action} {abs(amount)} 积分", stream_id
        )
        return True, f"调整积分{amount}", True

    @Command(
        "admin_attr",
        description="设置Bot属性（管理员）",
        pattern=r"^/投喂管理\s+属性\s+(?P<attr_key>\S+)\s+(?P<attr_value>\d+\.?\d*)(?:\s+(?P<target_group>\S+))?$",
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
            if not await self._is_group_admin(group_id, user_id):
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
        target_group = str(matched_groups.get("target_group") or "").strip()

        if not attr_key or not attr_value_str:
            await self.ctx.send.text(
                "用法：/投喂管理 属性 <属性名> <值> [群号]（属性名支持 satiety / 饱食度）",
                stream_id,
            )
            return False, "参数缺失", True

        if attr_key not in ("satiety", "饱食度"):
            await self.ctx.send.text(
                "无效属性名，当前仅支持：satiety / 饱食度", stream_id
            )
            return False, "无效属性名", True

        effective_group = target_group or group_id
        if not effective_group:
            await self.ctx.send.text("需要指定群号：/投喂管理 属性 satiety <值> <群号>", stream_id)
            return False, "缺少群号", True

        try:
            attr_value = float(attr_value_str)
        except ValueError:
            await self.ctx.send.text("属性值必须是数字", stream_id)
            return False, "属性值格式错误", True

        await self.db.set_satiety(attr_value, effective_group)

        await self.ctx.send.text(
            f"✅ 群 {effective_group} 饱食度已设置为 {attr_value:.0f}", stream_id
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
            if not await self._is_group_admin(group_id, user_id):
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

        effective_group = target_group or group_id
        if not effective_group:
            await self.ctx.send.text("需要指定群号：/投喂管理 重置签到 <QQ号> <群号>", stream_id)
            return False, "缺少群号", True

        uid = self.db.user_key(target_user, effective_group)
        await self.db.execute_commit(
            """
            UPDATE users SET total_sign_days = 0, consecutive_sign_days = 0, last_sign_time = 0
            WHERE user_id = ?
            """,
            (uid,),
        )

        await self.ctx.send.text(f"✅ 已重置 {target_user} 的签到记录", stream_id)
        return True, f"重置签到{target_user}", True

    @Command(
        "admin_set_sign_points",
        description="设置签到基础积分（管理员）",
        pattern=r"^/投喂管理\s+签到积分\s+(?P<value>\d+)$",
    )
    async def handle_admin_set_sign_points(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 签到积分 命令，设置每次签到获得的基础积分。"""
        if group_id:
            if not await self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        value_str = str(matched_groups.get("value") or "").strip()
        if not value_str:
            await self.ctx.send.text("用法：/投喂管理 签到积分 <数值>", stream_id)
            return False, "参数缺失", True

        try:
            value = int(value_str)
        except ValueError:
            await self.ctx.send.text("数值必须是整数", stream_id)
            return False, "数值格式错误", True

        # 持久化到数据库，并同步内存配置
        await self.db.set_setting("sign_base_points", str(value))
        self.config.sign.base_points = value

        await self.ctx.send.text(f"✅ 签到基础积分已设置为 {value}", stream_id)
        return True, f"设置签到积分{value}", True

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
            if not await self._is_group_admin(group_id, user_id):
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

        if group_id and target_group_id != group_id:
            await self.ctx.send.text("群聊中只能授权当前群的管理员", stream_id)
            return False, "跨群授权", True

        # 写入数据库（运行时授权）
        await self.db.add_group_admin(target_group_id, target_user)
        # 同步更新内存配置（供 _enabled_group_ids 使用）
        self._add_group_admin_to_memory(target_group_id, target_user)

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
            if not await self._is_group_admin(group_id, user_id):
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

        if group_id and target_group_id != group_id:
            await self.ctx.send.text("群聊中只能取消授权当前群的管理员", stream_id)
            return False, "跨群取消授权", True

        # 不允许取消 Bot 管理员的授权
        if self._is_bot_admin(target_user):
            await self.ctx.send.text("无法取消Bot管理员的授权", stream_id)
            return False, "无法取消Bot管理员", True

        # 从数据库移除（运行时授权）
        await self.db.remove_group_admin(target_group_id, target_user)
        # 同步更新内存配置
        self._remove_group_admin_from_memory(target_group_id, target_user)

        await self.ctx.send.text(
            f"✅ 已取消 {target_user} 在群 {target_group_id} 的管理员权限", stream_id
        )
        return True, f"取消授权{target_user}", True

    @Command(
        "admin_list_groups",
        description="查看已授权的群及管理员（管理员）",
        pattern=r"^/投喂管理\s+群列表$",
    )
    async def handle_admin_list_groups(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /投喂管理 群列表 命令，显示所有已配置群及其管理员。"""
        del kwargs

        if group_id:
            if not await self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False, "非管理员", True
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        # 从配置获取已启用的群列表
        enabled_groups = self._enabled_group_ids()
        if not enabled_groups:
            await self.ctx.send.text("暂无已授权的群", stream_id)
            return True, "无授权群", True

        lines = ["📋 已授权群列表"]
        for gid in sorted(enabled_groups):
            admins = await self.db.get_group_admins(gid)
            if admins:
                admin_str = ", ".join(admins)
            else:
                admin_str = "（无管理员）"
            lines.append(f"  群 {gid}：{admin_str}")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "群列表", True

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

        if not await self._is_group_admin(group_id, user_id):
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

        satiety_bonus, emoji, desc = self._parse_item_rest(rest)

        row = await self.db.fetchone(
            "SELECT 1 FROM shop_items WHERE name = ? AND scope = 'group' AND group_id = ? AND is_on_sale = 1",
            (name, group_id),
        )
        if row:
            await self.ctx.send.text(f"本群道具「{name}」已存在", stream_id)
            return False, "道具已存在", True

        await self.db.execute_commit(
            """
            INSERT INTO shop_items (name, emoji, description, price, scope, group_id,
                                     is_on_sale, satiety_bonus, created_by, created_at)
            VALUES (?, ?, ?, ?, 'group', ?, 1, ?, ?, ?)
            """,
            (name, emoji, desc, price, group_id, satiety_bonus, user_id, time.time()),
        )

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

        if not await self._is_group_admin(group_id, user_id):
            await self.ctx.send.text("只有群管理员或Bot管理员才能执行此命令", stream_id)
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        item_name = str(matched_groups.get("item_name") or "").strip()
        if not item_name:
            await self.ctx.send.text("用法：/投喂管理 群下架 <道具名>", stream_id)
            return False, "缺少道具名", True

        rowcount = await self.db.execute_rowcount(
            "UPDATE shop_items SET is_on_sale = 0 WHERE name = ? AND scope = 'group' AND group_id = ?",
            (item_name, group_id),
        )
        if rowcount == 0:
            await self.ctx.send.text(f"未找到本群道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        await self.ctx.send.text(f"✅ 本群道具「{item_name}」已下架", stream_id)
        return True, f"群下架{item_name}", True

    # ---- 小游戏：配置覆盖与参数命令 ----

    async def _apply_game_settings_overrides(self) -> None:
        """从数据库读取小游戏管理员设置并同步到内存配置。"""
        mapping: dict[str, tuple[str, Any]] = {
            "game:daily_earn_limit": ("daily_earn_limit", int),
            "game:guess_number_reward": ("guess_number_reward", int),
            "game:min_bet": ("min_bet", int),
            "game:max_bet": ("max_bet", int),
            "game:guess_number_enabled": ("guess_number_enabled", lambda v: v == "1"),
            "game:dice_enabled": ("dice_enabled", lambda v: v == "1"),
            "game:rps_enabled": ("rps_enabled", lambda v: v == "1"),
        }
        for key, (attr, conv) in mapping.items():
            raw = await self.db.get_setting(key)
            if raw is None:
                continue
            try:
                setattr(self.config.game, attr, conv(raw))
            except (TypeError, ValueError):
                continue

    async def _check_game_admin(
        self, stream_id: str, group_id: str, user_id: str
    ) -> bool:
        """小游戏管理命令的权限校验，失败时发送提示并返回 False。"""
        if group_id:
            if not await self._is_group_admin(group_id, user_id):
                await self.ctx.send.text("只有管理员才能执行此命令", stream_id)
                return False
        elif not self._is_bot_admin(user_id):
            await self.ctx.send.text("只有Bot管理员才能执行此命令", stream_id)
            return False
        return True

    async def _set_game_param(
        self,
        stream_id: str,
        user_id: str,
        group_id: str,
        matched_groups: dict[str, Any],
        *,
        key: str,
        attr: str,
        value_text: str,
        display: str,
        validate: Any,
    ) -> tuple[bool, str, bool]:
        """小游戏参数的通用设置流程。"""
        if not await self._check_game_admin(stream_id, group_id, user_id):
            return False, "非管理员", True
        if not value_text:
            await self.ctx.send.text(f"用法：/游戏管理 {display} <数值>", stream_id)
            return False, "参数缺失", True
        try:
            value = int(value_text)
        except ValueError:
            await self.ctx.send.text("数值必须是整数", stream_id)
            return False, "数值格式错误", True
        if error := validate(value):
            await self.ctx.send.text(error, stream_id)
            return False, "数值超范围", True
        setattr(self.config.game, attr, value)
        await self.db.set_setting(key, str(value))
        await self.ctx.send.text(f"✅ {display}已设置为 {value}", stream_id)
        return True, f"设置{display}{value}", True

    @Command(
        "game_admin_daily_limit",
        description="设置每日积分获取上限（管理员）",
        pattern=r"^/游戏管理\s+每日上限\s+(?P<value>\d+)$",
    )
    async def handle_game_admin_daily_limit(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /游戏管理 每日上限 命令。"""
        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        return await self._set_game_param(
            stream_id,
            user_id,
            group_id,
            matched_groups,
            key="game:daily_earn_limit",
            attr="daily_earn_limit",
            value_text=str(matched_groups.get("value") or ""),
            display="每日积分获取上限",
            validate=lambda v: None if v >= 1 else "数值必须大于等于 1",
        )

    @Command(
        "game_admin_guess_reward",
        description="设置猜数字奖励（管理员）",
        pattern=r"^/游戏管理\s+猜数字\s+奖励\s+(?P<value>\d+)$",
    )
    async def handle_game_admin_guess_reward(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /游戏管理 猜数字 奖励 命令。"""
        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        return await self._set_game_param(
            stream_id,
            user_id,
            group_id,
            matched_groups,
            key="game:guess_number_reward",
            attr="guess_number_reward",
            value_text=str(matched_groups.get("value") or ""),
            display="猜数字奖励",
            validate=lambda v: None if v >= 1 else "数值必须大于等于 1",
        )

    @Command(
        "game_admin_max_bet",
        description="设置下注上限（管理员）",
        pattern=r"^/游戏管理\s+下注上限\s+(?P<value>\d+)$",
    )
    async def handle_game_admin_max_bet(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /游戏管理 下注上限 命令。"""
        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        return await self._set_game_param(
            stream_id,
            user_id,
            group_id,
            matched_groups,
            key="game:max_bet",
            attr="max_bet",
            value_text=str(matched_groups.get("value") or ""),
            display="下注上限",
            validate=lambda v: (
                None
                if v >= self.config.game.min_bet
                else "下注上限不能低于下注下限"
            ),
        )

    @Command(
        "game_admin_min_bet",
        description="设置下注下限（管理员）",
        pattern=r"^/游戏管理\s+下注下限\s+(?P<value>\d+)$",
    )
    async def handle_game_admin_min_bet(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /游戏管理 下注下限 命令。"""
        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        return await self._set_game_param(
            stream_id,
            user_id,
            group_id,
            matched_groups,
            key="game:min_bet",
            attr="min_bet",
            value_text=str(matched_groups.get("value") or ""),
            display="下注下限",
            validate=lambda v: (
                None
                if v >= 1 and v <= self.config.game.max_bet
                else "下注下限必须大于等于1且不高于下注上限"
            ),
        )

    @Command(
        "game_admin_switch",
        description="启停游戏（管理员）",
        pattern=r"^/游戏管理\s+开关\s+(?P<game>猜数字|猜大小|石头剪刀布)\s+(?P<state>开|关)$",
    )
    async def handle_game_admin_switch(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        """处理 /游戏管理 开关 命令。"""
        if not await self._check_game_admin(stream_id, group_id, user_id):
            return False, "非管理员", True

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}
        game = str(matched_groups.get("game") or "")
        state = str(matched_groups.get("state") or "")
        enabled = state == "开"

        attr_map = {
            "猜数字": ("guess_number_enabled", "game:guess_number_enabled"),
            "猜大小": ("dice_enabled", "game:dice_enabled"),
            "石头剪刀布": ("rps_enabled", "game:rps_enabled"),
        }
        attr, key = attr_map[game]
        setattr(self.config.game, attr, enabled)
        await self.db.set_setting(key, "1" if enabled else "0")
        await self.ctx.send.text(
            f"✅ {game}已{'开启' if enabled else '关闭'}", stream_id
        )
        return True, f"开关{game}", True
