"""Валидация настроек: мусор не должен попадать в базу.

Некоторые ошибки (несуществующий пояс, перевёрнутый рабочий день) глушат
алерты навсегда.
"""

import pytest

from app.services.settings_rules import RULES, SettingError, check_section, validate
from app.services.settings_store import (
    DEFAULTS,
    MODE_OFF,
    MODE_ON,
    next_in_cycle,
    normalize_mode,
    visible_fields,
)
from tests.conftest import requires_db


def test_every_visible_setting_has_a_rule():
    """Новое поле в интерфейсе без правила проверки — дыра по умолчанию."""
    missing = [
        f"{section}.{key}"
        for section in DEFAULTS
        for key in visible_fields(section, DEFAULTS[section])
        if key not in RULES.get(section, {})
    ]
    assert not missing, missing


@pytest.mark.parametrize(
    "section, key, value",
    [
        ("work_calendar", "timezone", "Europe/Атлантида"),
        ("work_calendar", "start", "25:00"),
        ("work_calendar", "start", "10-00"),
        ("work_calendar", "weekdays", "8"),
        ("work_calendar", "weekdays", ""),
        ("work_calendar", "holidays", "31.02.2026"),
        ("work_calendar", "holidays", "2026-02-31"),
        ("alerts", "threshold_minutes", "-5"),
        ("alerts", "threshold_minutes", "0"),
        ("alerts", "threshold_minutes", "не знаю"),
        ("alerts", "enabled", "может быть"),
        ("alerts", "substantive_mode", "включить"),
        ("alerts", "substantive_mode", "shadow"),
        ("episodes", "max_messages", "0"),
        ("episodes", "wait_reaction_hours", "9999"),
        ("episodes", "wait_specialist_days", "0"),
        ("retention", "raw_update_days", "-1"),
    ],
)
def test_garbage_is_rejected(section, key, value):
    with pytest.raises(SettingError):
        validate(section, key, value)


@pytest.mark.parametrize(
    "section, key, value, expected",
    [
        ("alerts", "threshold_minutes", " 45 ", 45),
        ("alerts", "enabled", "да", True),
        ("alerts", "respect_quiet_hours", "нет", False),
        ("alerts", "substantive_mode", " On ", "on"),
        ("work_calendar", "weekdays", "1,2,3", [1, 2, 3]),
        ("work_calendar", "weekdays", "3,1,2,1", [1, 2, 3]),
        ("work_calendar", "timezone", "Europe/Moscow", "Europe/Moscow"),
        ("work_calendar", "holidays", "2026-01-01, 2026-01-02", ["2026-01-01", "2026-01-02"]),
        ("retention", "message_text_days", "бессрочно", None),
        ("retention", "message_text_days", "90", 90),
    ],
)
def test_valid_values_are_normalised(section, key, value, expected):
    assert validate(section, key, value) == expected


def test_unknown_field_cannot_be_written():
    with pytest.raises(SettingError):
        validate("alerts", "routing_strategy", "everyone")


def test_reversed_working_day_is_rejected():
    with pytest.raises(SettingError):
        check_section("work_calendar", {"start": "19:00", "end": "10:00"})


def test_equal_start_and_end_is_rejected():
    with pytest.raises(SettingError):
        check_section("work_calendar", {"start": "10:00", "end": "10:00"})


def test_normal_working_day_passes():
    check_section("work_calendar", {"start": "10:00", "end": "19:00"})


# ── Диапазоны праздников ────────


def test_holidays_accept_single_dates_and_ranges():
    got = validate(
        "work_calendar", "holidays", "2027-01-01 - 2027-01-04, 2027-02-23"
    )
    assert got == [
        "2027-01-01",
        "2027-01-02",
        "2027-01-03",
        "2027-01-04",
        "2027-02-23",
    ]


def test_holidays_range_separators_and_order():
    assert validate("work_calendar", "holidays", "2027-01-03..2027-01-01") == [
        "2027-01-01",
        "2027-01-02",
        "2027-01-03",
    ]
    assert validate("work_calendar", "holidays", "2027-01-01—2027-01-02") == [
        "2027-01-01",
        "2027-01-02",
    ]


def test_holidays_reject_garbage_and_giant_ranges():
    with pytest.raises(SettingError):
        validate("work_calendar", "holidays", "01.01.2027")
    with pytest.raises(SettingError):
        validate("work_calendar", "holidays", "2027-02-30")
    # Опечатка в годе не должна превращать календарь в сплошной праздник.
    with pytest.raises(SettingError):
        validate("work_calendar", "holidays", "2026-01-01 - 2072-01-08")


# ── Значения, оставшиеся в базе от прежних версий ────────


def test_legacy_shadow_mode_reads_as_off():
    """Прежнее значение режима второго алерта «shadow» читается как «выключен»."""
    assert normalize_mode("shadow") == MODE_OFF
    assert next_in_cycle("alerts", "substantive_mode", "shadow") == MODE_ON


@requires_db
async def test_stored_keys_unknown_to_the_code_are_kept_but_hidden(session):
    """В сохранённом разделе могут лежать ключи, которых код не знает: чтение,
    проверка и правка на них не спотыкаются, на экран и в правку они не попадают,
    при записи раздела сохраняются как есть."""
    from app.db.models import Setting
    from app.services.settings_store import get_section, set_value

    legacy = {
        "routing_strategy": "supervisors_only",
        "escalation_enabled": False,
        "also_notify_supervisor": True,
    }
    session.add(Setting(key="alerts", value={
        **legacy, "enabled": True, "threshold_minutes": 30,
        "substantive_threshold_minutes": 1440, "substantive_mode": "on",
    }))
    await session.flush()

    values = await get_section(session, "alerts")
    assert values["threshold_minutes"] == 30 and values["substantive_mode"] == "on"
    assert not set(legacy) & set(visible_fields("alerts", values))
    check_section("alerts", values)
    with pytest.raises(SettingError):
        validate("alerts", "routing_strategy", "everyone")

    await set_value(session, "alerts", "threshold_minutes", 45, actor_id=None)
    await session.flush()
    stored = await session.get(Setting, "alerts")
    assert stored.value["threshold_minutes"] == 45
    assert {key: stored.value[key] for key in legacy} == legacy
