"""Промпт-профили по модели: у каждой модели свой текст системного промпта
при общем контракте меток и JSON. Проверяются выбор профиля (по модели и по
`AI_PROMPT_PROFILE`), неизменность боевого `gptoss-p14` и отпечатки профилей."""

from app.services.ai import (
    CLIENT_SYSTEM_DS1B,
    CLIENT_SYSTEM_V14,
    COMPANY_SYSTEM_DS1B,
    COMPANY_SYSTEM_V14,
    DEFAULT_PROMPT_PROFILE,
    GPTOSS_CALL_PARAMS,
    MAX_COMPLETION_TOKENS,
    PROMPT_PROFILES,
    PROMPT_VERSION,
    AiClient,
    CallParams,
    PromptProfile,
    active_profile,
    profile_for_model,
    resolve_prompt_profile,
)

# Тело запроса gpt-oss. `reasoning_effort` — ВЕРХНЕУРОВНЕВОЕ поле:
# объект `reasoning` Cloud.ru у этой модели игнорирует.
GPTOSS_BODY = {
    "temperature": 0,
    "max_tokens": 800,
    "response_format": {"type": "json_object"},
    "reasoning_effort": "low",
}

# Тело запроса `CallParams()` по умолчанию; ds1b меняет в нём только
# рассуждения.
DEFAULT_BODY = {
    "temperature": 0,
    "max_tokens": 500,
    "response_format": {"type": "json_object"},
    "reasoning": {"effort": "low"},
}


# ── Выбор профиля ──────────────────────────────────────────────────────


def test_model_picks_its_profile():
    """Каждая из двух моделей приходит к своему профилю."""
    assert profile_for_model("deepseek-ai/DeepSeek-V4-Pro") == "deepseek-ds1b"
    # Та же модель без вендорного префикса: у провайдера встречаются оба вида.
    assert profile_for_model("DeepSeek-V4-Pro") == "deepseek-ds1b"
    assert profile_for_model("openai/gpt-oss-120b") == "gptoss-p14"
    assert profile_for_model("gpt-oss-120b") == "gptoss-p14"
    assert resolve_prompt_profile("openai/gpt-oss-120b").profile_id == "gptoss-p14"


def test_unknown_model_falls_back_to_the_production_profile():
    """Незнакомая модель не роняет воркер и не меняет промпт молча."""
    assert DEFAULT_PROMPT_PROFILE == "gptoss-p14"
    assert resolve_prompt_profile("some-vendor/unheard-of-9000").profile_id == (
        DEFAULT_PROMPT_PROFILE
    )
    assert profile_for_model("some-vendor/unheard-of-9000") is None
    # Своего профиля у GigaChat больше нет: он не используется.
    assert profile_for_model("ai-sage/GigaChat3.5-432B-A28B") is None
    # Пустая модель — тесты, профиль тот же.
    assert resolve_prompt_profile("").profile_id == DEFAULT_PROMPT_PROFILE
    assert resolve_prompt_profile(None).profile_id == DEFAULT_PROMPT_PROFILE


def test_explicit_override_beats_the_model():
    """`AI_PROMPT_PROFILE` перебивает выбор по модели: модель зовут через прокси."""
    chosen = resolve_prompt_profile("openai/gpt-oss-120b", "deepseek-ds1b")
    assert chosen.profile_id == "deepseek-ds1b"
    assert chosen.client_system is CLIENT_SYSTEM_DS1B


def test_typo_in_the_override_does_not_switch_the_prompt():
    """Опечатка в настройке — профиль по умолчанию, а не падение прохода."""
    assert resolve_prompt_profile("deepseek-ai/DeepSeek-V4-Pro", "deepsek-ds1b").profile_id == (
        DEFAULT_PROMPT_PROFILE
    )


def test_active_profile_reads_the_settings(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "ai_model", "deepseek-ai/DeepSeek-V4-Pro", raising=False)
    monkeypatch.setattr(settings, "ai_prompt_profile", "", raising=False)
    assert active_profile().profile_id == "deepseek-ds1b"

    monkeypatch.setattr(settings, "ai_prompt_profile", "gptoss-p14", raising=False)
    assert active_profile().profile_id == "gptoss-p14"


# ── Боевой профиль не сдвинулся ────────────────────────────────────────


def test_production_profile_is_the_measured_prompt():
    """Тексты профиля — ТЕ ЖЕ объекты, что и модульные константы v14."""
    profile = PROMPT_PROFILES["gptoss-p14"]
    assert profile.client_system is CLIENT_SYSTEM_V14
    assert profile.company_system is COMPANY_SYSTEM_V14
    assert profile.version == "p14"
    # В базу пишется номер редакции профиля, а не формат входа.
    assert profile.db_version == 1502 != PROMPT_VERSION
    assert profile.client_fingerprint == "aecd7ae1f282cd03"
    assert profile.company_fingerprint == "9a370a07e65105b5"


def test_empty_settings_give_the_production_profile(monkeypatch):
    """Без AI_MODEL и AI_PROMPT_PROFILE классифицирует боевой промпт."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "ai_model", "", raising=False)
    monkeypatch.setattr(settings, "ai_prompt_profile", "", raising=False)
    profile = active_profile()
    assert profile.profile_id == DEFAULT_PROMPT_PROFILE
    assert profile.client_system is CLIENT_SYSTEM_V14
    assert profile.company_system is COMPANY_SYSTEM_V14


def test_production_call_params_are_byte_for_byte_the_measured_ones():
    """Параметры вызова и ПОРЯДОК ключей боевого профиля не меняются."""
    profile = PROMPT_PROFILES["gptoss-p14"]
    assert profile.call_params is GPTOSS_CALL_PARAMS
    body = profile.call_params.as_body()
    assert body == GPTOSS_BODY
    assert list(body) == list(GPTOSS_BODY)


def test_client_builds_the_production_request_body():
    """Полное тело запроса боевого клиента: model, messages и параметры профиля."""
    client = AiClient(profile=PROMPT_PROFILES["gptoss-p14"])
    body = client.build_payload("система", "сообщение", model="openai/gpt-oss-120b")
    assert body == {
        "model": "openai/gpt-oss-120b",
        "messages": [
            {"role": "system", "content": "система"},
            {"role": "user", "content": "сообщение"},
        ],
        **GPTOSS_BODY,
    }
    assert list(body)[:2] == ["model", "messages"]


def test_client_cuts_the_payload_at_the_production_limit():
    """Вход обрезается до 4000 символов."""
    client = AiClient(profile=PROMPT_PROFILES["gptoss-p14"])
    body = client.build_payload("s", "я" * 5000)
    assert len(body["messages"][1]["content"]) == 4000


def test_call_params_hand_out_copies():
    """Тело запроса нельзя испортить из чужого вызова: словари копируются."""
    params = CallParams()
    first = params.as_body()
    first["reasoning"]["effort"] = "high"
    assert params.as_body()["reasoning"] == {"effort": "low"}


# ── Резервный профиль ──────────────────────────────────────────────────


def test_deepseek_ds1b_fingerprints():
    """Редакция ds1b: тексты, номер в базе и параметры вызова."""
    profile = PROMPT_PROFILES["deepseek-ds1b"]
    assert profile.version == "ds1b"
    assert profile.db_version == 1402
    assert profile.client_system is CLIENT_SYSTEM_DS1B
    assert profile.company_system is COMPANY_SYSTEM_DS1B
    assert profile.client_fingerprint == "3ee05c9b836f1cdd"
    assert profile.company_fingerprint == "7921513a95b203ab"
    # Параметры вызова — по умолчанию, кроме рассуждений: `effort: "none"`.
    assert profile.call_params.as_body() == {**DEFAULT_BODY, "reasoning": {"effort": "none"}}
    assert list(profile.call_params.as_body()) == list(DEFAULT_BODY)


def test_ds1b_keeps_the_load_bearing_rules():
    """Что чинит промпт резерва, обязано остаться в тексте."""
    ds1b = PROMPT_PROFILES["deepseek-ds1b"]
    # Компанейский текст уходит в КАЖДЫЙ вызов стороны компании: потолок
    # длины держит цену резерва.
    assert len(ds1b.company_system) <= 4600
    assert len(ds1b.client_system) <= 4300

    # Требование JSON-режима DeepSeek: слово «json» строчной латиницей
    # и образец объекта в тексте.
    for text in (ds1b.client_system, ds1b.company_system):
        assert "json" in text
        assert '{"label":"' in text
        # Нумерованный порядок решений с остановкой на первом подошедшем.
        assert "остановись на первом подошедшем" in text

    # Признаки передачи специалисту: не меньше четырёх типовых формулировок.
    handoff_phrases = (
        "передала запрос бухгалтеру",
        "передала ваш запрос",
        "передала вопрос специалисту",
        "передал коллеге на исполнение",
        "бухгалтер ответит",
        "специалист свяжется",
    )
    present = [f for f in handoff_phrases if f in ds1b.company_system]
    assert len(present) >= 4, present
    # Граница handoff/ack и запрет ссылки вне списка.
    assert "ИНФОРМАЦИЮ" in ds1b.company_system
    assert "выдуманный запрещён" in ds1b.company_system
    # Ровно два основания для null: чужая тема и «относится ко всем сразу».
    assert "НИ К ОДНОЙ" in ds1b.company_system
    assert "КО ВСЕМ" in ds1b.company_system

    # Просьба позвонить — request, offline только про «уже решено вне чата».
    assert "жду звонка" in ds1b.client_system
    assert "Это единственный случай offline" in ds1b.client_system
    # Границы addition/correction: пустой список и уже сделанная работа.
    assert "метки addition и correction запрещены" in ds1b.client_system
    assert "по чужой теме" in ds1b.client_system
    # other только не о делах клиента.
    assert "не о делах клиента" in ds1b.company_system


def test_gptoss_params_differ_from_the_defaults_exactly_where_measured():
    """Отличий от параметров по умолчанию ровно два."""
    body = PROMPT_PROFILES["gptoss-p14"].call_params.as_body()
    assert body["temperature"] == DEFAULT_BODY["temperature"]
    assert body["response_format"] == DEFAULT_BODY["response_format"]
    # 1. Верхнеуровневое поле вместо объекта `reasoning`: его Cloud.ru читает.
    assert "reasoning" not in body
    assert body["reasoning_effort"] == "low"
    # 2. Запас по длине ответа: на 500 рассуждения съедали весь лимит,
    #    и ответ приходил пустым с finish_reason=length.
    assert body["max_tokens"] == 800 > MAX_COMPLETION_TOKENS


# ── Реестр целиком ─────────────────────────────────────────────────────


def test_every_profile_keeps_the_common_output_contract():
    """Промпт у каждой модели свой, а метки и поля JSON — общие."""
    from app.services.ai import CLIENT_LABELS, COMPANY_LABELS

    # Список действующего контракта. `info` из CLIENT_LABELS сюда не входит:
    # она осталась там ради прошлой разметки, промпты её не предлагают.
    client_labels = ("request", "question", "mixed_request", "answer",
                     "addition", "correction", "ack", "social", "offline")
    company_labels = ("substantive", "handoff", "promise", "question", "ack", "other")

    for profile in PROMPT_PROFILES.values():
        for label in client_labels:
            assert label in CLIENT_LABELS
            assert label in profile.client_system, (profile.profile_id, label)
        for label in company_labels:
            assert label in COMPANY_LABELS
            assert label in profile.company_system, (profile.profile_id, label)
        assert "requires_response" in profile.client_system
        assert "answers_request_id" in profile.company_system


def test_profile_versions_and_db_ids_are_unique():
    """Две редакции с одним номером в базе неразличимы."""
    db_versions = [p.db_version for p in PROMPT_PROFILES.values()]
    assert len(set(db_versions)) == len(db_versions)
    # Колонка Classification.prompt_version — Integer, строку туда не положить.
    assert all(isinstance(value, int) for value in db_versions)
    # Номера профилей живут выше тысячи и не пересекаются с номерами
    # прошлой разметки в базе.
    assert all(value > 1000 for value in db_versions)


# Отпечатки реестра: (профиль, клиент, сотрудник). Отпечаток профиля
# считается по версии, обоим текстам и телу запроса, поэтому любая правка
# промпта или параметров ломает эту таблицу — и правится она только вместе
# с новым `db_version`.
PROFILE_FINGERPRINTS = {
    "deepseek-ds1b": ("6da82a48060bb546", "3ee05c9b836f1cdd", "7921513a95b203ab"),
    "gptoss-p14": ("fae7a5e462379ae8", "aecd7ae1f282cd03", "9a370a07e65105b5"),
}


def test_registry_fingerprints_are_pinned():
    """Смена промпта или call_params — другая редакция с новым отпечатком."""
    assert {pid: (p.fingerprint, p.client_fingerprint, p.company_fingerprint)
            for pid, p in PROMPT_PROFILES.items()} == PROFILE_FINGERPRINTS


def test_profile_fingerprint_covers_texts_and_call_params():
    """Отпечаток профиля различает даже одинаковые тексты с разными параметрами."""
    fingerprints = {p.profile_id: p.fingerprint for p in PROMPT_PROFILES.values()}
    assert len(set(fingerprints.values())) == len(fingerprints)

    production = PROMPT_PROFILES["gptoss-p14"]
    louder = CallParams(
        max_tokens=800,
        extra_body={"reasoning_effort": "high"},
    )
    changed = PromptProfile(
        profile_id=production.profile_id,
        client_system=CLIENT_SYSTEM_V14,
        company_system=COMPANY_SYSTEM_V14,
        version=production.version,
        db_version=production.db_version,
        call_params=louder,
    )
    assert changed.client_fingerprint == production.client_fingerprint
    assert changed.fingerprint != production.fingerprint
