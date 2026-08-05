from __future__ import annotations

import importlib.util
import re
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
rd = games.riddle


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


def test_riddle_check_answer() -> None:
    assert rd.check_answer("雨", "雨") is True
    assert rd.check_answer("雨", "下雨了") is True
    assert rd.check_answer("花生", "花生") is True
    assert rd.check_answer("月亮", "太阳") is False
    assert rd.check_answer("月亮", "月 亮！") is True


def test_riddle_process_guess() -> None:
    state = rd.new_session("谜面", "雨", 5)
    status, text = rd.process_guess(state, "太阳")
    assert status == "wrong"
    assert "还剩 4 次" in text
    status, text = rd.process_guess(state, "雨")
    assert status == "win"
    assert "答案是「雨」" in text


def test_riddle_process_guess_over() -> None:
    state = rd.new_session("谜面", "雨", 1)
    rd.process_guess(state, "太阳")
    status, text = rd.process_guess(state, "太阳")
    assert status == "over"
    assert "答案是「雨」" in text


def test_riddle_parse_llm_result() -> None:
    riddle_text, answer = rd.parse_llm_result(
        "谜面：弯弯的月儿小小的船\n答案：月亮"
    )
    assert riddle_text == "弯弯的月儿小小的船"
    assert answer == "月亮"


def test_participation_command_pattern() -> None:
    pattern = (
        r"^/游戏\s+(?P<game>猜数字|猜谜语|猜大小|石头剪刀布)"
        r"\s+参与积分[：:](?P<participate>\d+)"
        r"\s+获得积分[：:](?P<reward>\d+)$"
    )
    m = re.match(pattern, "/游戏 猜数字 参与积分：5 获得积分：30")
    assert m is not None
    assert m.group("game") == "猜数字"
    assert m.group("participate") == "5"
    assert m.group("reward") == "30"
    m = re.match(pattern, "/游戏 石头剪刀布 参与积分:10 获得积分:20")
    assert m is not None
    assert m.group("game") == "石头剪刀布"


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
async def test_riddle_start_and_win_awards_points(plugin: PluginHarness) -> None:
    await _seed_user(plugin, points=0)
    result = await plugin.handle_game_riddle(
        stream_id="s", user_id="user", group_id="group"
    )
    assert result == (True, "开始猜谜语", True)
    assert "🧩 谜语" in plugin.ctx.send.messages[-1][0]
    assert "group:user" in plugin._game_sessions

    plugin._game_sessions["group:user"] = rd.new_session("谜面", "雨", 5)
    result = await plugin.handle_game_riddle(
        stream_id="s",
        user_id="user",
        group_id="group",
        matched_groups={"answer": "下雨"},
    )
    assert result == (True, "猜谜语答对", True)
    assert await plugin.db.get_points("group:user") == 200
    assert "group:user" not in plugin._game_sessions


@pytest.mark.asyncio
async def test_riddle_over_cap_no_points(plugin: PluginHarness) -> None:
    plugin.config.game.daily_earn_limit = 10
    await _seed_user(plugin, points=0)
    await plugin.db.settle("group:user", "group", "riddle", 0, 1, 10)
    plugin._game_sessions["group:user"] = rd.new_session("谜面", "雨", 5)
    result = await plugin.handle_game_riddle(
        stream_id="s",
        user_id="user",
        group_id="group",
        matched_groups={"answer": "雨"},
    )
    assert result == (True, "猜谜语答对", True)
    assert await plugin.db.get_points("group:user") == 10


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
async def test_admin_set_riddle_reward_and_switch(plugin: PluginHarness) -> None:
    result = await plugin.handle_game_admin_riddle_reward(
        stream_id="s",
        user_id="admin",
        group_id="",
        matched_groups={"value": "300"},
    )
    assert result == (True, "设置猜谜语奖励300", True)
    assert plugin.config.game.riddle_reward == 300
    assert await plugin.db.get_setting("game:riddle_reward") == "300"

    result = await plugin.handle_game_admin_switch(
        stream_id="s",
        user_id="admin",
        group_id="",
        matched_groups={"game": "猜谜语", "state": "关"},
    )
    assert result == (True, "开关猜谜语", True)
    assert plugin.config.game.riddle_enabled is False
    assert await plugin.db.get_setting("game:riddle_enabled") == "0"


@pytest.mark.asyncio
async def test_admin_set_participation(plugin: PluginHarness) -> None:
    result = await plugin.handle_game_admin_set_participation(
        stream_id="s",
        user_id="admin",
        group_id="",
        matched_groups={"game": "猜数字", "participate": "5", "reward": "30"},
    )
    assert result == (True, "设置猜数字参与5获得30", True)
    assert plugin.config.game.guess_number_participate == 5
    assert plugin.config.game.guess_number_reward == 30
    assert await plugin.db.get_setting("game:guess_number_participate") == "5"
    assert await plugin.db.get_setting("game:guess_number_reward") == "30"

    # 新实例从数据库恢复
    config2 = FeedBotConfig()
    harness2 = PluginHarness(plugin.db, config2)
    await harness2._apply_game_settings_overrides()
    assert config2.game.guess_number_participate == 5
    assert config2.game.guess_number_reward == 30


@pytest.mark.asyncio
async def test_guess_number_participate_fee_and_reward(plugin: PluginHarness) -> None:
    await _seed_user(plugin, points=100)
    await plugin.handle_game_admin_set_participation(
        stream_id="s",
        user_id="admin",
        group_id="",
        matched_groups={"game": "猜数字", "participate": "5", "reward": "30"},
    )
    result = await plugin.handle_game_guess_number(
        stream_id="s", user_id="user", group_id="group"
    )
    assert result == (True, "开始猜数字", True)
    assert await plugin.db.get_points("group:user") == 95

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
    assert await plugin.db.get_points("group:user") == 125


@pytest.mark.asyncio
async def test_guess_number_participate_insufficient(plugin: PluginHarness) -> None:
    await _seed_user(plugin, points=3)
    await plugin.handle_game_admin_set_participation(
        stream_id="s",
        user_id="admin",
        group_id="",
        matched_groups={"game": "猜数字", "participate": "5", "reward": "30"},
    )
    result = await plugin.handle_game_guess_number(
        stream_id="s", user_id="user", group_id="group"
    )
    assert result == (False, "参与积分不足", True)
    assert await plugin.db.get_points("group:user") == 3
    assert "group:user" not in plugin._game_sessions


@pytest.mark.asyncio
async def test_dice_fixed_participate_and_reward(plugin: PluginHarness) -> None:
    await _seed_user(plugin, points=1000)
    await plugin.handle_game_admin_set_participation(
        stream_id="s",
        user_id="admin",
        group_id="",
        matched_groups={"game": "猜大小", "participate": "50", "reward": "100"},
    )
    result = await plugin.handle_game_dice(
        stream_id="s",
        user_id="user",
        group_id="group",
        matched_groups={"choice": "大", "bet": "999"},
    )
    assert result == (True, "猜大小大", True)
    points = await plugin.db.get_points("group:user")
    assert points in (1100, 950), points
    row = await plugin.db.fetchone(
        "SELECT bet FROM game_records WHERE user_id = 'group:user' AND game = 'dice'"
    )
    assert row == (50,)


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
