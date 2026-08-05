"""投喂插件 — 配置模型。"""

from __future__ import annotations

from maibot_sdk import Field, PluginConfigBase


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用投喂插件")
    config_version: str = Field(default="2.1.0", description="配置版本")


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

    __ui_label__ = "Bot属性（bot_attr）"
    __ui_icon__ = "heart"
    __ui_order__ = 3

    initial_satiety: float = Field(
        default=80.0,
        description="初始饱食度（0-100）",
        json_schema_extra={"label": "初始饱食度（initial_satiety）"},
    )
    satiety_decay_rate: float = Field(
        default=0.5,
        description="饱食度每小时衰减量",
        json_schema_extra={"label": "饱食度衰减量（satiety_decay_rate）"},
    )
    seek_feed_threshold: float = Field(
        default=30.0,
        description="饱食度低于此值触发求投喂",
        json_schema_extra={"label": "求投喂阈值（seek_feed_threshold）"},
    )
    seek_feed_cooldown: float = Field(
        default=7200.0,
        description="求投喂消息冷却时间（秒，默认2小时）",
        json_schema_extra={"label": "求投喂冷却时间（seek_feed_cooldown）"},
    )
    quiet_hours: str = Field(
        default="23:00-08:00",
        description="求投喂安静时段，格式 'HH:MM-HH:MM'（24小时制），该时段内不发送求投喂消息；留空则不限制",
        json_schema_extra={"label": "安静时段（quiet_hours）"},
    )
    seek_feed_messages: list[str] = Field(
        default_factory=lambda: [
            "呜呜...好饿好饿...有没有人投喂我呀？🥺",
            "肚子咕咕叫了...能投喂我一些吃的吗？😢",
            "有点想吃东西了...有人愿意投喂我吗？🥺",
            "虽然还不算太饿，但如果有人投喂我就好了~",
            "好想吃东西呀...谁来投喂我一下嘛~",
        ],
        description="求投喂消息列表，随机选择一条发送（LLM开启时优先用LLM生成）",
        json_schema_extra={"label": "求投喂消息（seek_feed_messages）"},
    )


class GroupAdminEntry(PluginConfigBase):
    """单个群的授权配置。"""

    __ui_label__ = "群配置"
    __ui_icon__ = "users"
    __ui_order__ = 0

    group_id: str = Field(default="", description="群号")
    admin_users: str = Field(
        default="",
        description="管理员QQ号（多个用逗号、空格或 | 分隔）",
    )

    def gid(self) -> str:
        """返回清理后的群号。"""
        return str(self.group_id or "").strip()

    def admin_list(self) -> list[str]:
        """返回解析后的管理员QQ列表。"""
        return [s.strip() for s in str(self.admin_users or "").replace(",", " ").replace("|", " ").split() if s.strip()]


class FilterConfig(PluginConfigBase):
    """触发控制配置。"""

    __ui_label__ = "触发控制"
    __ui_icon__ = "filter"
    __ui_order__ = 4

    group_admins: list[GroupAdminEntry] = Field(
        default_factory=list,
        description="授权群配置，每项包含群号和管理员QQ。只有在列表中的群才会响应命令",
    )


class GameConfig(PluginConfigBase):
    """小游戏配置。"""

    __ui_label__ = "游戏设置（game）"
    __ui_icon__ = "gamepad"
    __ui_order__ = 5

    daily_earn_limit: int = Field(
        default=1500,
        description="每日积分获取上限",
        json_schema_extra={"label": "每日积分获取上限（daily_earn_limit）"},
    )
    guess_number_reward: int = Field(
        default=20,
        description="猜数字奖励积分",
        json_schema_extra={"label": "猜数字奖励积分（guess_number_reward）"},
    )
    guess_number_max_tries: int = Field(
        default=7,
        description="猜数字每局最大次数",
        json_schema_extra={"label": "猜数字最大次数（guess_number_max_tries）"},
    )
    min_bet: int = Field(
        default=10,
        description="下注下限",
        json_schema_extra={"label": "下注下限（min_bet）"},
    )
    max_bet: int = Field(
        default=500,
        description="下注上限",
        json_schema_extra={"label": "下注上限（max_bet）"},
    )
    dice_win_multiplier: float = Field(
        default=2.0,
        description="猜大小赔率（含本金）",
        json_schema_extra={"label": "猜大小赔率（dice_win_multiplier）"},
    )
    rps_win_multiplier: float = Field(
        default=2.0,
        description="石头剪刀布赔率（含本金）",
        json_schema_extra={"label": "石头剪刀布赔率（rps_win_multiplier）"},
    )
    guess_number_enabled: bool = Field(
        default=True,
        description="猜数字开关",
        json_schema_extra={"label": "猜数字开关（guess_number_enabled）"},
    )
    dice_enabled: bool = Field(
        default=True,
        description="猜大小开关",
        json_schema_extra={"label": "猜大小开关（dice_enabled）"},
    )
    rps_enabled: bool = Field(
        default=True,
        description="石头剪刀布开关",
        json_schema_extra={"label": "石头剪刀布开关（rps_enabled）"},
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
    game: GameConfig = Field(default_factory=GameConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
