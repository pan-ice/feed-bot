from importlib import util
from pathlib import Path
import sys
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "feed_bot_llm_test_plugin"


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

config_module = __import__(f"{PACKAGE_NAME}.config", fromlist=["*"])
loops_module = __import__(f"{PACKAGE_NAME}.loops", fromlist=["*"])

FeedBotConfig = config_module.FeedBotConfig
LoopTasksMixin = loops_module.LoopTasksMixin


class FakeLogger:
    def warning(self, message: str) -> None:
        del message

    def error(self, message: str) -> None:
        del message


class FakeNewLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        prompt: str,
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        *,
        task_name: str = "utils",
        model_name: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "task_name": task_name,
                "model_name": model_name,
                **kwargs,
            }
        )
        return {"response": "收到啦！"}


class FakeOldLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        prompt: str,
        model: str = "",
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> dict[str, str]:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                **kwargs,
            }
        )
        return {"response": "收到啦！"}


class FakePluginConfig:
    async def get(self, key: str, default: str) -> str:
        del key
        return default


class FakeChat:
    async def get_group_streams(self) -> list[dict[str, str]]:
        return [{"group_id": "group", "session_id": "stream"}]


class FakeSend:
    def __init__(self, on_send: Any) -> None:
        self.messages: list[tuple[str, str]] = []
        self._on_send = on_send

    async def text(self, content: str, stream_id: str) -> None:
        self.messages.append((content, stream_id))
        self._on_send()


class FakeContext:
    def __init__(self, on_send: Any, llm: Any) -> None:
        self.llm = llm
        self.config = FakePluginConfig()
        self.chat = FakeChat()
        self.send = FakeSend(on_send)
        self.logger = FakeLogger()


class FakeDatabase:
    is_open = True

    async def get_satiety(self, group_id: str, config: FeedBotConfig) -> float:
        del group_id, config
        return 10.0

    async def fetchone(self, query: str, params: tuple[str]) -> tuple[int]:
        del query, params
        return (0,)

    async def execute_commit(self, query: str, params: tuple[float, str]) -> None:
        del query, params


class LoopHarness(LoopTasksMixin):
    def __init__(self, llm: Any | None = None) -> None:
        self.config = FeedBotConfig()
        self._running = True
        self.db = FakeDatabase()
        self.ctx = FakeContext(self._stop_after_send, llm or FakeNewLLM())

    def _is_group_enabled(self, group_id: str) -> bool:
        del group_id
        return True

    def _stop_after_send(self) -> None:
        self._running = False


@pytest.mark.parametrize(
    ("llm_type", "route_key"),
    [(FakeNewLLM, "task_name"), (FakeOldLLM, "model")],
)
@pytest.mark.asyncio
async def test_feed_reply_supports_new_and_old_sdk(
    llm_type: type[Any], route_key: str
) -> None:
    llm = llm_type()
    harness = LoopHarness(llm)

    result = await harness._generate_feed_reply(
        user_nickname="测试用户",
        item_name="面包",
        item_emoji="🍞",
        quantity=1,
        feed_reply_hint="",
        satiety_before=10.0,
        satiety_after=20.0,
        satiety_bonus=10.0,
        recent_feeds=[],
    )

    assert result == "收到啦！"
    assert llm.calls[0][route_key] == "replyer"
    if route_key == "task_name":
        assert llm.calls[0]["model"] == ""
    else:
        assert "task_name" not in llm.calls[0]


@pytest.mark.parametrize(
    ("llm_type", "route_key"),
    [(FakeNewLLM, "task_name"), (FakeOldLLM, "model")],
)
@pytest.mark.asyncio
async def test_seek_feed_supports_new_and_old_sdk(
    llm_type: type[Any], route_key: str
) -> None:
    llm = llm_type()
    harness = LoopHarness(llm)
    harness.config.bot_attr.quiet_hours = ""

    await harness._seek_feed_loop()

    assert harness.ctx.send.messages == [("收到啦！", "stream")]
    assert llm.calls[0][route_key] == "replyer"
    if route_key == "task_name":
        assert llm.calls[0]["model"] == ""
    else:
        assert "task_name" not in llm.calls[0]
