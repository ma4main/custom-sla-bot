"""Отпечатки боевого промпта 14 (профиль gptoss-p14). Если тест упал — промпт
изменили: тогда профилю нужен новый `db_version`, а отпечатки здесь и в
tests/test_prompt_profiles.py обновляются осознанно."""

import hashlib

from app.services.ai import (
    CLIENT_SYSTEM_V14,
    COMPANY_SYSTEM_V14,
    PROMPT_VERSION,
    resolve_prompt_profile,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def test_prompt_version_marks_the_input_format_contract():
    # PROMPT_VERSION — версия формата входа (хвост, открытые обращения,
    # маркер вложения у текущего сообщения), а не номер редакции текста:
    # в базу пишется `db_version` профиля.
    assert PROMPT_VERSION == 14


def test_v14_is_the_active_measured_revision():
    """Основная модель размечается промптом 14."""
    profile = resolve_prompt_profile("openai/gpt-oss-120b")
    assert profile.client_system is CLIENT_SYSTEM_V14
    assert profile.company_system is COMPANY_SYSTEM_V14
    assert _sha(CLIENT_SYSTEM_V14) == "aecd7ae1f282cd03"
    assert _sha(COMPANY_SYSTEM_V14) == "9a370a07e65105b5"


def test_v14_states_the_arbiter_decisions():
    from app.services.ai import COMPANY_LABELS

    # (1) связь по единственному обращению этой темы.
    assert "единственное обращение" not in COMPANY_SYSTEM_V14
    assert "ровно одно обращение по этой теме — ставь его номер" in COMPANY_SYSTEM_V14
    # (2) mixed_request — только отдельное независимое поручение.
    assert "Соседство с вопросом компании" not in CLIENT_SYSTEM_V14
    assert "выполнимое независимо от ответа" in CLIENT_SYSTEM_V14
    # (3) отпуск — `other`, метка описана.
    assert "«С завтрашнего дня я в отпуске» → other" in COMPANY_SYSTEM_V14
    assert "other — сообщение не о работе клиента" in COMPANY_SYSTEM_V14
    assert "other" in COMPANY_LABELS
