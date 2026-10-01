"""Разговорная просьба компании прислать материал (`v4_mode`): «давайте сюда», «присылайте»,
«скидывайте», «кидайте» взводят «ждём материал» так же, как «пришлите»; файлы клиента после
такой просьбы — ответ, а не новое обращение."""

from datetime import timedelta

import pytest

from app.db.models import InteractionState
from app.services.episodes import material_request
from tests.test_engine_v4_rules import STAFF_IRINA, msk, replay
from tests.test_engine_v4_requested_material import _case_material_request

COLLOQUIAL_ASKS = [
    "Давайте сюда выписку",
    "Присылайте акты, посмотрю",
    "Скидывайте счета",
    "Кидайте документы",
]


@pytest.mark.parametrize("text", COLLOQUIAL_ASKS)
def test_a_colloquial_material_ask_is_recognised(text):
    assert material_request(text)


@pytest.mark.parametrize("text", ["Давайте созвонимся завтра", "Присылали уже, спасибо"])
def test_other_texts_are_not_a_material_ask(text):
    assert not material_request(text)


@pytest.mark.parametrize("ask", COLLOQUIAL_ASKS)
async def test_requested_files_after_a_colloquial_ask_are_an_answer(ask):
    messages, verdicts = _case_material_request(ask, with_ack=False)
    items, result = await replay(messages, verdicts, staff={1: STAFF_IRINA},
                                 after=timedelta(seconds=msk(11, 5)), settle_open=True)
    assert result["answers"] == [2, 3]
    assert items[2].state is InteractionState.NO_RESPONSE_NEEDED
