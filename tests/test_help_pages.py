"""Справка: действующие настройки, короткие страницы и доступ активных учёток.

Без базы: help_text — чистые функции от контекста, ровно для этого.
"""

from app.bot.callbacks import HelpNav, Nav
from app.bot.handlers.help_ui import hub_keyboard, page_keyboard
from app.bot.keyboards import main_menu
from app.db.models import BotRole, BotUser, BotUserState
from app.services import help_text
from app.services.settings_store import DEFAULTS

# Потолок Telegram — 4096; запас на будущие правки текста.
TEXT_LIMIT = 3900


def _ctx(**overrides) -> help_text.HelpContext:
    base = {
        "calendar": dict(DEFAULTS["work_calendar"]),
        "alerts": dict(DEFAULTS["alerts"]),
        "episodes": dict(DEFAULTS["episodes"]),
        "digest": dict(DEFAULTS["digest"]),
        "ai_enabled": True,
    }
    base.update(overrides)
    return help_text.HelpContext(**base)


def _user(role: BotRole) -> BotUser:
    return BotUser(tg_user_id=1, role=role, permissions={}, state=BotUserState.ACTIVE)


def _targets(markup) -> set[str]:
    return {
        button.callback_data for row in markup.inline_keyboard for button in row
    }


def test_every_page_fits_telegram_and_is_unique():
    ctx = _ctx()
    texts = {key: help_text.render(key, ctx) for key, _ in help_text.PAGES}
    texts["hub"] = help_text.hub(ctx)
    for key, text in texts.items():
        assert len(text) <= TEXT_LIMIT, f"экран {key}: {len(text)} символов"
        assert text.startswith("<b>"), f"экран {key} без заголовка"
    assert len(set(texts.values())) == len(texts), "два экрана с одним текстом"


def test_pages_take_numbers_from_settings_not_from_text():
    """Порог, срок, окна и график — из настроек в момент показа."""
    ctx = _ctx(
        calendar={**DEFAULTS["work_calendar"], "start": "09:00", "end": "18:30"},
        alerts={
            **DEFAULTS["alerts"],
            "threshold_minutes": 45,
            "substantive_threshold_minutes": 240,
        },
        episodes={**DEFAULTS["episodes"], "wait_reaction_hours": 12, "wait_specialist_days": 3},
    )
    time_page = help_text.render("time", ctx)
    assert "09:00–18:30" in time_page
    assert "45 рабочих минут" in time_page
    assert "4 ч 00 мин после передачи" in time_page
    assert "12 ч без реакции" in time_page
    assert "3 календарных дн. после передачи" in time_page
    assert "понедельник 15:00" not in time_page


def test_default_specialist_deadline_reads_as_next_workday():
    text = help_text.render("time", _ctx())
    assert "то же время следующего рабочего дня" in text


def test_time_page_says_greeting_alone_is_not_reaction():
    """Как в движке (R-19, `episodes.is_greeting_only`): одно приветствие — не первая реакция."""
    text = help_text.render("time", _ctx())
    assert "одно приветствие («Добрый день») реакцией не считается" in text
    assert "даже приветствие" not in text


def test_calendar_history_does_not_replace_current_schedule():
    calendar = {
        **DEFAULTS["work_calendar"],
        "end": "17:00",
        "since": "2026-09-03T10:00:00+03:00",
        "history": [{**DEFAULTS["work_calendar"], "end": "19:00"}],
    }
    text = help_text.render("time", _ctx(calendar=calendar))
    assert "10:00–17:00" in text
    assert "10:00–19:00" not in text


def test_alerts_page_reflects_switches():
    off = help_text.render(
        "alerts",
        _ctx(alerts={**DEFAULTS["alerts"], "enabled": False, "substantive_mode": "off"}),
    )
    assert "Отправка алертов выключена" in off
    assert "«Нет ответа специалиста» выключен" in off

    on = help_text.render(
        "alerts",
        _ctx(
            notify_group_configured=True,
            alerts={
                **DEFAULTS["alerts"],
                "enabled": True,
                "to_group": True,
                "substantive_mode": "on",
            },
            digest={
                **DEFAULTS["digest"],
                "alerts_digest_enabled": True,
                "alerts_digest_times": ["10:10", "13:00", "16:40"],
                "weekly_enabled": True,
                "weekly_day": 4,
                "weekly_period": "last7",
                "weekly_html": True,
                "weekly_xlsx": False,
            },
        ),
    )
    assert "доставка алертов в группу уведомлений" in on
    assert "Отправка алертов включена" in on
    assert "10:10, 13:00, 16:40" in on
    assert "Каждый выпуск приходит, даже если алертов и новых итогов нет" in on
    assert "четверг, 10:05" in on
    assert "последние 7 дней" in on
    assert "HTML-страница" in on and "XLSX" not in on


def test_ai_page_handles_disabled():
    assert "ИИ сейчас выключен" not in help_text.render("ai", _ctx())
    assert "ИИ сейчас выключен" in help_text.render("ai", _ctx(ai_enabled=False))


def test_unknown_page_falls_back_to_hub():
    ctx = _ctx()
    assert help_text.render("nosuch", ctx) == help_text.hub(ctx)


def test_hub_keyboard_lists_every_page_and_way_back():
    targets = _targets(hub_keyboard())
    for key, _ in help_text.PAGES:
        assert HelpNav(page=key).pack() in targets
    assert Nav(to="main").pack() in targets

    page_targets = _targets(page_keyboard())
    assert HelpNav(page="hub").pack() in page_targets
    assert Nav(to="main").pack() in page_targets


def test_help_button_visible_to_all_active_roles_and_not_to_inactive():
    help_button = Nav(to="help").pack()
    for role in BotRole:
        user = _user(role)
        assert help_button in _targets(main_menu(user)), role.value
        for state in (BotUserState.DISABLED, BotUserState.PENDING):
            user.state = state
            assert not _targets(main_menu(user))


def test_alert_delivery_text_handles_quiet_hours_legacy_mode_and_missing_group():
    text = help_text.render("alerts", _ctx(alerts={
        **DEFAULTS["alerts"], "enabled": True, "to_group": True,
        "respect_quiet_hours": False, "substantive_mode": "shadow",
    }))
    assert "в любое время суток" in text
    assert "в рабочее время" not in text
    # Прежнее значение «shadow» читается как «выключен».
    assert "Вид «Нет ответа специалиста» выключен" in text
    assert "Группа уведомлений не задана" in text
    assert "доставка алертов в группу уведомлений" not in text


def test_action_hints_follow_permissions():
    reader = _ctx()
    admin = _ctx(can_review_authors=True, can_review_alerts=True)
    assert "Сотрудники →" not in help_text.render("reports", reader)
    assert "Сотрудники →" in help_text.render("reports", admin)
    assert "можно снять" not in help_text.render("alerts", reader)
    assert "можно снять" in help_text.render("alerts", admin)
