"""投喂插件 — 管理员命令 Mixin。"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from maibot_sdk import Command

from .config import FeedBotConfig
from .db import AsyncDatabase
from .utils import PLUGIN_DIR, atomic_write, extract_nickname, is_likely_emoji


class AdminCommandsMixin:
    """管理员命令：全局商店、群商店、积分调整、属性设置、授权管理。"""

    # 这些属性由 FeedBotPlugin 提供，类型标注仅供静态分析
    db: AsyncDatabase
    config: FeedBotConfig

    # ---- 权限与过滤 ----

    def _is_bot_admin(self, user_id: str) -> bool:
        """判断是否为Bot管理员。"""
        return user_id in self.config.admin.admin_users

    def _is_group_admin(self, group_id: str, user_id: str) -> bool:
        """判断是否为指定群的管理员（含Bot管理员）。"""
        if self._is_bot_admin(user_id):
            return True
        for item in self.config.filter.group_admins:
            if not isinstance(item, dict):
                continue
            if str(item.get("group_id", "")) == group_id:
                raw = str(item.get("admin_users", "") or "")
                if user_id in self._parse_admin_list(raw):
                    return True
        return False

    @staticmethod
    def _parse_admin_list(raw: str) -> list[str]:
        """解析管理员列表字符串，支持空格、逗号、|分隔。"""
        return [s.strip() for s in raw.replace(",", " ").replace("|", " ").split() if s.strip()]

    def _enabled_group_ids(self) -> set[str]:
        """返回配置中授权的所有群号。"""
        result: set[str] = set()
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

    # ---- 配置持久化 ----

    @staticmethod
    def _toml_escape(s: str) -> str:
        """转义 TOML 基本字符串中的特殊字符。"""
        return (
            s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\r", "\\r")
             .replace("\t", "\\t")
        )

    @staticmethod
    def _toml_list(items: list[str]) -> str:
        """将字符串列表格式化为 TOML 数组。"""
        if not items:
            return "[]"
        escaped = [f'"{AdminCommandsMixin._toml_escape(i)}"' for i in items]
        return '[' + ", ".join(escaped) + ']'

    def _save_config_to_file(self) -> None:
        """将当前配置原子写入 config.toml 文件。"""
        try:
            config_path = os.path.join(PLUGIN_DIR, "config.toml")
            lines: list[str] = []
            lines.append("[plugin]")
            lines.append(f"enabled = {'true' if self.config.plugin.enabled else 'false'}")
            lines.append(f'config_version = "{self._toml_escape(self.config.plugin.config_version)}"')
            lines.append("")
            lines.append("[admin]")
            lines.append(f"admin_users = {self._toml_list(self.config.admin.admin_users)}")
            lines.append("")
            lines.append("[sign]")
            lines.append(f"base_points = {self.config.sign.base_points}")
            lines.append(f"consecutive_bonus = {self.config.sign.consecutive_bonus}")
            lines.append(f"max_consecutive_bonus = {self.config.sign.max_consecutive_bonus}")
            lines.append("")
            lines.append("[bot_attr]")
            lines.append(f"initial_satiety = {self.config.bot_attr.initial_satiety}")
            lines.append(f"satiety_decay_rate = {self.config.bot_attr.satiety_decay_rate}")
            lines.append(f"seek_feed_threshold = {self.config.bot_attr.seek_feed_threshold}")
            lines.append(f"seek_feed_cooldown = {self.config.bot_attr.seek_feed_cooldown}")
            lines.append("")
            lines.append("[filter]")
            if self.config.filter.group_admins:
                for item in self.config.filter.group_admins:
                    if isinstance(item, dict):
                        gid = str(item.get("group_id", "") or "")
                        raw = str(item.get("admin_users", "") or "")
                        lines.append("[[filter.group_admins]]")
                        lines.append(f'group_id = "{self._toml_escape(gid)}"')
                        lines.append(f'admin_users = "{self._toml_escape(raw)}"')
            else:
                lines.append("group_admins = []")
            lines.append("")
            lines.append("[llm]")
            lines.append(f"enabled = {'true' if self.config.llm.enabled else 'false'}")
            lines.append(f'model = "{self._toml_escape(self.config.llm.model)}"')
            lines.append(f"temperature = {self.config.llm.temperature}")
            lines.append(f"max_tokens = {self.config.llm.max_tokens}")
            lines.append(f'fallback_reply = "{self._toml_escape(self.config.llm.fallback_reply)}"')
            lines.append("")

            with atomic_write(config_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:
            self.ctx.logger.warning(f"保存配置文件失败: {e}")

    def _add_group_admin_to_config(self, target_group_id: str, target_user: str) -> None:
        """在配置中添加群管理员，若群不存在则创建条目。"""
        admins = self.config.filter.group_admins
        for item in admins:
            if isinstance(item, dict) and str(item.get("group_id", "")) == target_group_id:
                raw = str(item.get("admin_users", "") or "")
                existing = self._parse_admin_list(raw)
                if target_user not in existing:
                    existing.append(target_user)
                    item["admin_users"] = ", ".join(existing)
                self._save_config_to_file()
                return
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
            if not self._is_group_admin(group_id, user_id):
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
        target_group = str(matched_groups.get("target_group") or "").strip()

        if not attr_key or not attr_value_str:
            await self.ctx.send.text("用法：/投喂管理 属性 satiety <值> [群号]", stream_id)
            return False, "参数缺失", True

        if attr_key != "satiety":
            await self.ctx.send.text("无效属性名，当前仅支持：satiety", stream_id)
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

        if group_id and target_group_id != group_id:
            await self.ctx.send.text("群聊中只能取消授权当前群的管理员", stream_id)
            return False, "跨群取消授权", True

        # 不允许取消 Bot 管理员的授权
        if self._is_bot_admin(target_user):
            await self.ctx.send.text("无法取消Bot管理员的授权", stream_id)
            return False, "无法取消Bot管理员", True

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

        rowcount = await self.db.execute_rowcount(
            "UPDATE shop_items SET is_on_sale = 0 WHERE name = ? AND scope = 'group' AND group_id = ?",
            (item_name, group_id),
        )
        if rowcount == 0:
            await self.ctx.send.text(f"未找到本群道具「{item_name}」", stream_id)
            return False, "道具不存在", True

        await self.ctx.send.text(f"✅ 本群道具「{item_name}」已下架", stream_id)
        return True, f"群下架{item_name}", True
