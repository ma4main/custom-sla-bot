"""Отметка живости бота доказывает приём апдейтов, а не работу таймера.

Источников два: каждый входящий апдейт и активная проба `get_me`. Здесь
проверяется проба — она ловит залипший polling при живом процессе.
"""

import asyncio

import pytest

from app import health
from app.bot import main as bot_main


@pytest.fixture
def beats(tmp_path, monkeypatch):
    """Изолированный каталог отметок и мгновенный цикл пробы."""
    monkeypatch.setattr(health, "HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(bot_main, "PROBE_INTERVAL_SECONDS", 0)
    return tmp_path / "bot.beat"


class _Bot:
    def __init__(self, fail: bool = False, hang: bool = False) -> None:
        self.fail = fail
        self.hang = hang
        self.calls = 0

    async def get_me(self):
        self.calls += 1
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail:
            raise RuntimeError("Bad Gateway")
        return object()


async def _run_briefly(bot) -> None:
    """Дать циклу сделать несколько оборотов и остановить его."""
    task = asyncio.create_task(bot_main._heartbeat_loop(bot))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_successful_probe_refreshes_the_mark(beats):
    bot = _Bot()

    await _run_briefly(bot)

    assert bot.calls > 0, "проба вообще не вызывалась"
    assert beats.exists(), "успешная проба не обновила отметку"


async def test_failed_probe_leaves_the_mark_stale(beats):
    """Провал пробы — не повод считать бота живым.

    Отметка не обновляется, протухает, и сторож выходит из процесса;
    дальше срабатывает restart-policy докера.
    """
    bot = _Bot(fail=True)

    await _run_briefly(bot)

    assert bot.calls > 0, "проба вообще не вызывалась"
    assert not beats.exists(), (
        "отметка обновилась при недоступном Telegram — это и есть та самая "
        "ложная живость, из-за которой зависший бот выглядел здоровым"
    )


async def test_probe_does_not_hang_forever(beats, monkeypatch):
    """Зависший вызов не должен заморозить цикл пробы навсегда."""
    monkeypatch.setattr(bot_main, "PROBE_TIMEOUT_SECONDS", 0.01)
    bot = _Bot(hang=True)

    await _run_briefly(bot)

    assert bot.calls > 0
    assert not beats.exists(), "зависшая проба зачлась как успешная"


async def test_probe_failure_does_not_kill_the_loop(beats):
    """Единичный сбой сети не должен ронять бота: цикл продолжает пробовать."""
    bot = _Bot(fail=True)

    await _run_briefly(bot)

    assert bot.calls > 1, "цикл остановился после первой же неудачи"
