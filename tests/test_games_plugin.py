from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "feed_bot_games_test_plugin"


def _load_plugin_package() -> None:
    if PACKAGE_NAME in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)


_load_plugin_package()

commands_user = __import__(f"{PACKAGE_NAME}.commands_user", fromlist=["*"])
commands_admin = __import__(f"{PACKAGE_NAME}.commands_admin", fromlist=["*"])
config_module = __import__(f"{PACKAGE_NAME}.config", fromlist=["*"])
db_module = __import__(f"{PACKAGE_NAME}.db", fromlist=["*"])
games = __import__(f"{PACKAGE_NAME}.games", fromlist=["*"])

AsyncDatabase = db_module.AsyncDatabase
FeedBotConfig = config_module.FeedBotConfig
UserCommandsMixin = commands_user.UserCommandsMixin
AdminCommandsMixin = commands_admin.AdminCommandsMixin
gn = games.guess_number
dc = games.dice
rp = games.rps


class FakeSend:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def text(self, content: str, stream_id: str) -> None:
        self.messages.append((content, stream_id))


class FakeContext:
    def __init__(self) -> None:
        self.send = FakeSend()


class PluginHarness(UserCommandsMixin, AdminCommandsMixin):
    def __init__(self, db: AsyncDatabase, config: FeedBotConfig) -> None:
        self.db = db
        self.config = config
        self.ctx = FakeContext()
        self._game_sessions: dict[str, dict] = {}

    def _check_group_enabled(self, group_id: str) -> bool:
        return True

    async def _generate_feed_reply(self, **kwargs: Any) -> str:
        return "好吃！"


@pytest_asyncio.fixture
async def plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "feed_bot.db"))
    config = FeedBotConfig()
    config.admin.admin_users = ["admin"]
    database = AsyncDatabase(config)
    database.open()
    instance = PluginHarness(database, config)
    try:
        yield instance
    finally:
        database.close()


async def _seed_user(plugin: PluginHarness, points: int = 1000) -> str:
    await plugin.db.ensure_user("user", "", "group")
    uid = plugin.db.user_key("user", "group")
    await plugin.db.execute_commit(
        "UPDATE users SET points = ? WHERE user_id = ?", (points, uid)
    )
    return uid


# ---- 游戏逻辑 ----


def test_guess_number_win() -> None:
    state = gn.new_game(7)
    state["number"] = 42
    status, text = gn.guess(state, 42)
    assert status == "win"
    assert "42" in text


def test_guess_number_hint_and_over() -> None:
    state = gn.new_game(2)
    state["number"] = 50
    status, text = gn.guess(state, 10)
    assert status == "low"
    status, text = gn.guess(state, 90)
    assert status == "over"
    assert "答案是 50" in text


def test_dice_resolve_and_net_change() -> None:
    assert dc.resolve("大", 4) is True
    assert dc.resolve("大", 3) is False
    assert dc.resolve("小", 3) is True
    assert dc.net_change(100, True, 2.0) == 100
    assert dc.net_change(100, False, 2.0) == -100


def test_rps_resolve_matrix() -> None:
    assert rp.resolve("石头", "剪刀") == "win"
    assert rp.resolve("剪刀", "布") == "win"
    assert rp.resolve("布", "石头") == "win"
    assert rp.resolve("石头", "布") == "lose"
    assert rp.resolve("石头", "石头") == "draw"
    assert rp.net_change(100, "draw", 2.0) == 0


# ---- 命令测试 ----


@pytest.mark.asyncio
async def test_dice_play_and_settle(plugin: PluginHarness) -> None:
    await _seed_user(plugin)
    result = await plugin.handle_game_dice(
        stream_id="s",
        user_id="user",
        group_id="group",
        matched_groups={"choice": "大", "bet": "50"},
    )
    assert result == (True, "猜大小大", True)
    row = await plugin.db.fetchone(
        "SELECT COUNT(*) FROM game_records WHERE user_id = 'group:user'"
    )
    assert row == (1,)


@pytest.mark.asyncio
async def test_bet_rejected_when_daily_cap_reached(plugin: PluginHarness) -> None:
    plugin.config.game.daily_earn_limit = 10
    await _seed_user(plugin)
    await plugin.db.settle("group:user", "group", "guess_number", 0, 1, 10)
    result = await plugin.handle_game_dice(
        stream_id="s",
        user_id="user",
        group_id="group",
        matched_groups={"choice": "大", "bet": "50"},
    )
    assert result == (False, "已达上限", True)


@pytest.mark.asyncio
async def test_guess_number_win_awards_points(plugin: PluginHarness) -> None:
    await _seed_user(plugin, points=0)
    plugin._game_sessions["group:user"] = {
        "number": 42,
        "tries_left": 7,
        "started_at": time.time(),
    }
    result = await plugin.handle_game_guess_number(
        stream_id="s",
        user_id="user",
        group_id="group",
        matched_groups={"num": "42"},
    )
    assert result == (True, "猜中", True)
    assert await plugin.db.get_points("group:user") == 20


@pytest.mark.asyncio
async def test_admin_set_daily_limit_and_override(plugin: PluginHarness) -> None:
    result = await plugin.handle_game_admin_daily_limit(
        stream_id="s",
        user_id="admin",
        group_id="",
        matched_groups={"value": "2000"},
    )
    assert result == (True, "设置每日积分获取上限2000", True)
    assert plugin.config.game.daily_earn_limit == 2000
    assert await plugin.db.get_setting("game:daily_earn_limit") == "2000"

    config2 = FeedBotConfig()
    harness2 = PluginHarness(plugin.db, config2)
    await harness2._apply_game_settings_overrides()
    assert config2.game.daily_earn_limit == 2000


@pytest.mark.asyncio
async def test_admin_reject_non_admin(plugin: PluginHarness) -> None:
    result = await plugin.handle_game_admin_daily_limit(
        stream_id="s",
        user_id="stranger",
        group_id="",
        matched_groups={"value": "2000"},
    )
    assert result == (False, "非管理员", True)


@pytest.mark.asyncio
async def test_ranking_after_games(plugin: PluginHarness) -> None:
    await _seed_user(plugin, points=0)
    await plugin.db.settle("group:user", "group", "dice", 50, 1, 100)
    result = await plugin.handle_game_ranking(
        stream_id="s", user_id="user", group_id="group"
    )
    assert result == (True, "游戏排行", True)
    assert "100积分" in plugin.ctx.send.messages[-1][0]
