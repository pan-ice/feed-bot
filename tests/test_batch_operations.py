from importlib import util
from pathlib import Path
from typing import Any

import re
import sys
import time

import pytest
import pytest_asyncio

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "feed_bot_test_plugin"


def _load_plugin_package() -> None:
    if PACKAGE_NAME in sys.modules:
        return
    spec = util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)


_load_plugin_package()

commands_admin = __import__(f"{PACKAGE_NAME}.commands_admin", fromlist=["*"])
commands_user = __import__(f"{PACKAGE_NAME}.commands_user", fromlist=["*"])
config_module = __import__(f"{PACKAGE_NAME}.config", fromlist=["*"])
db_module = __import__(f"{PACKAGE_NAME}.db", fromlist=["*"])

AdminCommandsMixin = commands_admin.AdminCommandsMixin
AsyncDatabase = db_module.AsyncDatabase
FeedBotConfig = config_module.FeedBotConfig
MAX_SIGN_BASE_POINTS = config_module.MAX_SIGN_BASE_POINTS
SET_SIGN_POINTS_COMMAND_PATTERN = commands_admin.SET_SIGN_POINTS_COMMAND_PATTERN
UserCommandsMixin = commands_user.UserCommandsMixin
parse_item_request = commands_user.parse_item_request


class FakeSend:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def text(self, content: str, stream_id: str) -> None:
        self.messages.append((content, stream_id))


class FakeContext:
    def __init__(self) -> None:
        self.send = FakeSend()


class PluginHarness(UserCommandsMixin, AdminCommandsMixin):
    def __init__(self, db: AsyncDatabase) -> None:
        self.db = db
        self.config = FeedBotConfig()
        self.config.admin.admin_users = ["bot-admin"]
        self.ctx = FakeContext()

    def _check_group_enabled(self, group_id: str) -> bool:
        return True

    async def _generate_feed_reply(self, **kwargs: Any) -> str:
        return "好吃！"


@pytest_asyncio.fixture
async def plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "feed_bot.db"))
    database = AsyncDatabase(FeedBotConfig())
    database.open()
    instance = PluginHarness(database)
    try:
        yield instance
    finally:
        database.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("面包", ("面包", 1, None)),
        ("面包 x5", ("面包", 5, None)),
        ("面包 X5", ("面包", 5, None)),
        ("面包 *5", ("面包", 5, None)),
        ("面包 ×5", ("面包", 5, None)),
        ("面包 5", ("面包", 5, None)),
        ("面包 99", ("面包", 99, None)),
        ("面包 100", ("面包", 0, "单次最多操作99个道具")),
        ("面包 -1", ("面包", 0, "数量必须是正整数")),
        ("面包 +1", ("面包", 0, "数量必须是正整数")),
        ("面包 1.5", ("面包", 0, "数量必须是正整数")),
        ("面包 x0", ("面包", 0, "数量必须是正整数")),
        ("面包 x000", ("面包", 0, "数量必须是正整数")),
        ("面包 x100", ("面包", 0, "单次最多操作99个道具")),
        ("面包 x101", ("面包", 0, "单次最多操作99个道具")),
        ("面包 x" + "9" * 5000, ("面包", 0, "单次最多操作99个道具")),
    ],
)
def test_parse_item_request(raw: str, expected: tuple[str, int, str | None]) -> None:
    assert parse_item_request(raw) == expected


async def _seed_user_and_item(
    plugin: PluginHarness,
    *,
    points: int = 100,
    inventory: int = 0,
    satiety_bonus: float = 10.0,
) -> None:
    now = time.time()
    await plugin.db.execute_commit(
        """
        INSERT INTO users (user_id, nickname, points, total_sign_days,
                           consecutive_sign_days, last_sign_time,
                           total_feed_count, created_at)
        VALUES ('group:user', '测试用户', ?, 0, 0, 0, 0, ?)
        """,
        (points, now),
    )
    await plugin.db.execute_commit(
        """
        INSERT INTO shop_items (name, emoji, description, price, satiety_bonus,
                                scope, group_id, is_on_sale, created_at)
        VALUES ('面包', '🍞', '', 10, ?, 'global', '', 1, ?)
        """,
        (satiety_bonus, now),
    )
    if inventory > 0:
        await plugin.db.execute_commit(
            "INSERT INTO user_inventory (user_id, item_id, quantity) VALUES ('group:user', 1, ?)",
            (inventory,),
        )


@pytest.mark.asyncio
async def test_batch_buy_is_atomic(plugin: PluginHarness) -> None:
    await _seed_user_and_item(plugin, points=100)

    result = await plugin.handle_buy(
        stream_id="stream",
        user_id="user",
        group_id="group",
        matched_groups={"item_request": "面包 x5"},
    )

    assert result == (True, "购买面包 x5", True)
    assert await plugin.db.fetchone(
        "SELECT points FROM users WHERE user_id = 'group:user'"
    ) == (50,)
    assert await plugin.db.fetchone(
        "SELECT quantity FROM user_inventory WHERE user_id = 'group:user' AND item_id = 1"
    ) == (5,)

    failed = await plugin.handle_buy(
        stream_id="stream",
        user_id="user",
        group_id="group",
        matched_groups={"item_request": "面包 x6"},
    )

    assert failed == (False, "积分不足", True)
    assert await plugin.db.fetchone(
        "SELECT points FROM users WHERE user_id = 'group:user'"
    ) == (50,)
    assert await plugin.db.fetchone(
        "SELECT quantity FROM user_inventory WHERE user_id = 'group:user' AND item_id = 1"
    ) == (5,)


@pytest.mark.asyncio
async def test_batch_feed_truncates_to_satiety_capacity(plugin: PluginHarness) -> None:
    await _seed_user_and_item(plugin, inventory=5, satiety_bonus=10.0)
    await plugin.db.set_satiety(85.0, "group")

    result = await plugin.handle_feed(
        stream_id="stream",
        user_id="user",
        group_id="group",
        message={"message_info": {"user_info": {"user_nickname": "测试用户"}}},
        matched_groups={"item_request": "面包 x5"},
    )

    assert result == (True, "投喂面包 x2", True)
    assert await plugin.db.fetchone(
        "SELECT quantity FROM user_inventory WHERE user_id = 'group:user' AND item_id = 1"
    ) == (3,)
    assert await plugin.db.fetchone(
        "SELECT total_feed_count FROM users WHERE user_id = 'group:user'"
    ) == (2,)
    assert await plugin.db.fetchone(
        "SELECT quantity, reply_text FROM feed_records WHERE user_id = 'group:user'"
    ) == (2, "好吃！")
    satiety = await plugin.db.fetchone(
        "SELECT satiety FROM feed_groups WHERE group_id = 'group'"
    )
    assert satiety is not None
    assert satiety[0] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_batch_feed_inventory_shortage_rolls_back(plugin: PluginHarness) -> None:
    await _seed_user_and_item(plugin, inventory=2, satiety_bonus=10.0)
    await plugin.db.set_satiety(50.0, "group")

    result = await plugin.handle_feed(
        stream_id="stream",
        user_id="user",
        group_id="group",
        matched_groups={"item_request": "面包 x3"},
    )

    assert result == (False, "库存不足", True)
    assert await plugin.db.fetchone(
        "SELECT quantity FROM user_inventory WHERE user_id = 'group:user' AND item_id = 1"
    ) == (2,)
    assert await plugin.db.fetchone(
        "SELECT total_feed_count FROM users WHERE user_id = 'group:user'"
    ) == (0,)
    assert await plugin.db.fetchone("SELECT COUNT(*) FROM feed_records") == (0,)
    satiety = await plugin.db.fetchone(
        "SELECT satiety FROM feed_groups WHERE group_id = 'group'"
    )
    assert satiety is not None
    assert satiety[0] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_sign_in_uses_db_override(plugin: PluginHarness) -> None:
    await _seed_user_and_item(plugin, points=0)

    # 默认基础积分 10
    result = await plugin.handle_sign_in(
        stream_id="stream", user_id="user", group_id="group"
    )
    assert result == (True, "签到获得10积分", True)
    assert await plugin.db.fetchone(
        "SELECT points FROM users WHERE user_id = 'group:user'"
    ) == (10,)

    # Bot管理员私聊设置基础积分后，下一次签到使用持久化覆盖值
    setting_result = await plugin.handle_admin_set_sign_points(
        stream_id="stream",
        user_id="bot-admin",
        matched_groups={"value": "20"},
    )
    assert setting_result == (True, "设置签到积分20", True)
    assert await plugin.db.get_setting("sign_base_points") == "20"
    assert plugin.config.sign.base_points == 10
    await plugin.db.execute_commit(
        "UPDATE users SET last_sign_time = ? WHERE user_id = 'group:user'",
        (time.time() - 86400,),
    )
    result = await plugin.handle_sign_in(
        stream_id="stream", user_id="user", group_id="group"
    )
    # 基础20 + 连续签到奖励5 = 25
    assert result == (True, "签到获得25积分", True)
    assert await plugin.db.fetchone(
        "SELECT points FROM users WHERE user_id = 'group:user'"
    ) == (35,)
    assert "基础20 + 连续奖励5" in plugin.ctx.send.messages[-1][0]


@pytest.mark.asyncio
async def test_set_sign_points_requires_bot_admin_private_chat(
    plugin: PluginHarness,
) -> None:
    group_result = await plugin.handle_admin_set_sign_points(
        stream_id="stream",
        user_id="bot-admin",
        group_id="group",
        matched_groups={"value": "20"},
    )
    assert group_result == (False, "非私聊", True)

    user_result = await plugin.handle_admin_set_sign_points(
        stream_id="stream",
        user_id="user",
        matched_groups={"value": "20"},
    )
    assert user_result == (False, "非管理员", True)
    assert await plugin.db.get_setting("sign_base_points") is None


@pytest.mark.asyncio
async def test_set_sign_points_validates_input_range(plugin: PluginHarness) -> None:
    invalid_result = await plugin.handle_admin_set_sign_points(
        stream_id="stream",
        user_id="bot-admin",
        matched_groups={"value": "abc"},
    )
    assert invalid_result == (False, "数值格式错误", True)

    overflow_result = await plugin.handle_admin_set_sign_points(
        stream_id="stream",
        user_id="bot-admin",
        matched_groups={"value": str(MAX_SIGN_BASE_POINTS + 1)},
    )
    assert overflow_result == (False, "数值超出范围", True)
    assert await plugin.db.get_setting("sign_base_points") is None

    boundary_result = await plugin.handle_admin_set_sign_points(
        stream_id="stream",
        user_id="bot-admin",
        matched_groups={"value": str(MAX_SIGN_BASE_POINTS)},
    )
    assert boundary_result == (
        True,
        f"设置签到积分{MAX_SIGN_BASE_POINTS}",
        True,
    )
    assert await plugin.db.get_setting("sign_base_points") == str(
        MAX_SIGN_BASE_POINTS
    )


@pytest.mark.asyncio
async def test_sign_in_rejects_invalid_persisted_override(
    plugin: PluginHarness,
) -> None:
    await plugin.db.set_setting("sign_base_points", "invalid")

    with pytest.raises(ValueError, match="sign_base_points 不是整数"):
        await plugin.handle_sign_in(
            stream_id="stream", user_id="user", group_id="group"
        )


@pytest.mark.asyncio
async def test_get_satiety_initializes_admin_only_group(plugin: PluginHarness) -> None:
    await plugin.db.set_group_admins("group", ["group-admin"])

    satiety = await plugin.db.get_satiety("group", plugin.config)

    assert satiety == pytest.approx(plugin.config.bot_attr.initial_satiety)
    row = await plugin.db.fetchone(
        "SELECT satiety, last_decay_time, group_admin_users "
        "FROM feed_groups WHERE group_id = 'group'"
    )
    assert row is not None
    assert row[0] == pytest.approx(plugin.config.bot_attr.initial_satiety)
    assert row[1] > 0
    assert row[2] == "group-admin"


@pytest.mark.asyncio
async def test_feed_rules_separates_group_only_commands(plugin: PluginHarness) -> None:
    result = await plugin.handle_feed_rules(
        stream_id="stream", user_id="user", group_id=""
    )

    assert result == (True, "投喂规则", True)
    content = plugin.ctx.send.messages[-1][0]
    assert "通用指令：" in content
    assert "群聊指令：" in content


def test_bot_attr_schema_uses_bilingual_labels() -> None:
    schema = FeedBotConfig.model_json_schema()
    fields = schema["$defs"]["BotAttrConfig"]["properties"]

    assert fields["initial_satiety"]["label"] == "初始饱食度（initial_satiety）"
    assert fields["seek_feed_messages"]["label"] == "求投喂消息（seek_feed_messages）"


@pytest.mark.parametrize(
    ("command", "value"),
    [
        ("/投喂管理 签到积分", None),
        ("/投喂管理 签到积分 abc", "abc"),
        ("/投喂管理 签到积分 -1", "-1"),
        ("/投喂管理 签到积分 20", "20"),
    ],
)
def test_set_sign_points_command_captures_raw_value(
    command: str, value: str | None
) -> None:
    matched = re.fullmatch(SET_SIGN_POINTS_COMMAND_PATTERN, command)

    assert matched is not None
    assert matched.group("value") == value
