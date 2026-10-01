"""Поиск по чатам.

Ищется по части названия без учёта регистра, по всем состояниям сразу;
«%» и «_» — обычные символы, а не подстановка; лимит с честным total.
"""

from app.db.models import Chat, ChatState
from app.services.chats import search_chats
from tests.conftest import requires_db


async def _chats(session):
    session.add_all(
        [
            Chat(tg_chat_id=-100960001, title="Бухгалтерия: РОМАШКА + ВЕКТОР", state=ChatState.TRACKED),
            Chat(tg_chat_id=-100960002, title="ромашка склад", state=ChatState.ARCHIVED),
            Chat(tg_chat_id=-100960003, title="Сигма", state=ChatState.PAUSED),
            Chat(tg_chat_id=-100960004, title="100% отчёт", state=ChatState.TRACKED),
            Chat(tg_chat_id=-100960005, title=None, state=ChatState.TRACKED),
        ]
    )
    await session.flush()


@requires_db
async def test_search_is_case_insensitive_and_spans_states(session):
    await _chats(session)
    rows, total = await search_chats(session, "РОМАШКА")
    assert total == 2
    assert {c.state for c in rows} == {ChatState.TRACKED, ChatState.ARCHIVED}


@requires_db
async def test_percent_and_underscore_are_literal(session):
    await _chats(session)
    rows, total = await search_chats(session, "%")
    assert total == 1 and rows[0].title == "100% отчёт", "«%» — символ, не «любой чат»"
    rows, total = await search_chats(session, "_")
    assert total == 0


@requires_db
async def test_limit_keeps_honest_total(session):
    await _chats(session)
    rows, total = await search_chats(session, "и", limit=1)
    assert len(rows) == 1
    assert total >= 2, "total — сколько всего совпало, а не сколько показано"
    assert await search_chats(session, "   ") == ([], 0)
