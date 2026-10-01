"""Вход классификатора: хвост переписки и открытые обращения.

Вход обязан быть воспроизводимым побайтно, поэтому проверяется форма —
порядок, обрезка, маркеры, пустые блоки, — а не то, что модель из неё поймёт.
"""

from datetime import datetime, timedelta, timezone

from app.services.ai import (
    OPEN_ITEMS,
    TAIL_EVENTS,
    OpenItem,
    TailEvent,
    client_payload,
    company_payload,
)

# 09:12 МСК — чтобы в строках было видно, что время переводится, а не
# печатается как есть: в UTC это 06:12.
MSK_0912 = datetime(2026, 9, 14, 6, 12, tzinfo=timezone.utc)


def _event(minutes: int, *, company: bool = False, text=None, media=None) -> TailEvent:
    return TailEvent(
        sent_at=MSK_0912 + timedelta(minutes=minutes),
        is_company=company,
        text=text,
        media_kind=media,
    )


def test_empty_blocks_are_explicit():
    """Пустой хвост и пустой список — «(нет)», а не отсутствующий блок.

    Молчание разделов модель читает как «поле забыли»: формат обязан быть
    один и тот же всегда.
    """
    assert client_payload("Добрый день") == (
        "=== ПРЕДЫДУЩИЕ СООБЩЕНИЯ ===\n(нет)\n"
        "=== ОТКРЫТЫЕ ОБРАЩЕНИЯ ===\n(нет)\n"
        "=== СООБЩЕНИЕ КЛИЕНТА ===\nДобрый день\n==="
    )


def test_client_and_company_payloads_differ_only_by_the_last_section():
    """Модель не должна путать, чьё сообщение классифицирует."""
    tail = [_event(0, text="Пришлите акт")]
    client = client_payload("ок", tail)
    company = company_payload("ок", tail)

    assert "=== СООБЩЕНИЕ КЛИЕНТА ===" in client
    assert "СООБЩЕНИЕ СОТРУДНИКА" not in client
    assert "=== СООБЩЕНИЕ СОТРУДНИКА ===" in company
    assert "СООБЩЕНИЕ КЛИЕНТА" not in company


def test_tail_lines_carry_msk_date_time_and_the_side():
    payload = client_payload(
        "И вот этот договор",
        [
            _event(0, text="Пришлите закрывающие за август"),
            _event(28, company=True, text="Принято"),
        ],
    )
    assert "[14.09 09:12] клиент: Пришлите закрывающие за август" in payload
    assert "[14.09 09:40] компания: Принято" in payload


def test_tail_events_of_different_days_are_distinguishable():
    """Окно хвоста растягивается на сутки — дата обязана быть в строке.

    Без неё соседние строки разных дней неразличимы: между «[15:18]»
    и «[10:39]» рядом могут пройти сутки.
    """
    payload = client_payload(
        "так что там с актом сверки?",
        [
            _event(0, company=True, text="Уточню у бухгалтера и вернусь"),
            _event(24 * 60 + 5, text="Добрый день"),
        ],
    )
    assert "[14.09 09:12] компания: Уточню у бухгалтера и вернусь" in payload
    assert "[15.09 09:17] клиент: Добрый день" in payload


def test_only_the_last_six_events_are_sent():
    """Хвост — шесть событий; лишние отрезаются с НАЧАЛА, а не с конца."""
    tail = [_event(n, text=f"реплика {n}") for n in range(10)]
    payload = client_payload("текст", tail)

    body = payload.split("=== ОТКРЫТЫЕ ОБРАЩЕНИЯ ===")[0]
    assert body.count("реплика") == TAIL_EVENTS
    assert "реплика 3" not in body, "отрезали новые события вместо старых"
    assert "реплика 4" in body and "реплика 9" in body


def test_long_line_is_cut_and_flattened():
    """Многострочное письмо — одна строка, 300 символов."""
    long_text = "начало\nвторая строка " + "я" * 500
    payload = client_payload("ок", [_event(0, text=long_text)])

    line = [row for row in payload.splitlines() if row.startswith("[14.09 09:12]")][0]
    assert "\n" not in line
    assert line.startswith("[14.09 09:12] клиент: начало вторая строка яяя")
    # «[14.09 09:12] клиент: » — служебная часть строки, лимит считается по тексту.
    assert len(line) - len("[14.09 09:12] клиент: ") == 300


def test_attachments_without_text_become_markers():
    payload = client_payload(
        "вот и всё",
        [
            _event(0, media="document"),
            _event(1, media="photo"),
            _event(2, media="voice"),
            _event(3, media="video_note"),
        ],
    )
    assert "[14.09 09:12] клиент: [документ]" in payload
    assert "[14.09 09:13] клиент: [фото]" in payload
    assert "[14.09 09:14] клиент: [голосовое]" in payload
    # Кружок, стикер, видео — для классификации одно и то же.
    assert "[14.09 09:15] клиент: [файл]" in payload


def test_attachment_caption_goes_after_the_marker():
    """Подпись к файлу — тот же `text`; вид вложения при этом не теряется."""
    payload = client_payload(
        "ок", [_event(0, media="document", text="Отправьте по ЭДО")]
    )
    assert "[14.09 09:12] клиент: [документ]: Отправьте по ЭДО" in payload


def test_open_items_show_id_time_state_and_text():
    payload = company_payload(
        "Акт сверки направила",
        (),
        [
            OpenItem(message_id=4821, opened_at=MSK_0912, text="Пришлите акт сверки"),
            OpenItem(
                message_id=4830,
                opened_at=MSK_0912 + timedelta(hours=2),
                text="Когда будет справка?",
                first_reaction_at=MSK_0912 + timedelta(hours=2, minutes=5),
                handoff_at=MSK_0912 + timedelta(hours=2, minutes=5),
            ),
            OpenItem(
                message_id=4840,
                opened_at=MSK_0912 + timedelta(hours=3),
                text="Проведите оплату",
                first_reaction_at=MSK_0912 + timedelta(hours=3, minutes=1),
            ),
        ],
    )
    assert "#4821 [14.09 09:12] ждём первой реакции: Пришлите акт сверки" in payload
    assert "#4830 [14.09 11:12] передано специалисту: Когда будет справка?" in payload
    assert "#4840 [14.09 12:12] в работе: Проведите оплату" in payload


def test_handoff_answered_by_the_specialist_is_back_in_work():
    """Передали и специалист ответил — это уже не «передано специалисту»."""
    item = OpenItem(
        message_id=7,
        opened_at=MSK_0912,
        text="Вопрос",
        first_reaction_at=MSK_0912,
        handoff_at=MSK_0912,
        substantive_at=MSK_0912 + timedelta(hours=1),
    )
    assert item.state_label == "в работе"


def test_open_item_text_is_cut_to_two_hundred():
    payload = company_payload(
        "ок", (), [OpenItem(message_id=7, opened_at=MSK_0912, text="щ" * 500)]
    )
    line = [row for row in payload.splitlines() if row.startswith("#7 ")][0]
    # «щ» нет ни в одном названии состояния — считаем только текст обращения.
    assert line.count("щ") == 200


def test_only_three_open_items_are_sent():
    items = [
        OpenItem(message_id=n, opened_at=MSK_0912 + timedelta(minutes=n), text=f"дело {n}")
        for n in range(1, 6)
    ]
    payload = company_payload("ок", (), items)
    block = payload.split("=== ОТКРЫТЫЕ ОБРАЩЕНИЯ ===")[1]
    assert block.count("дело") == OPEN_ITEMS
    assert "дело 3" in block and "дело 5" in block
    assert "дело 2" not in block, "отрезали свежие обращения вместо старых"


def test_message_itself_is_not_touched():
    """Само классифицируемое сообщение уходит как есть, без обрезки.

    Обрезает уже `AiClient.classify_detailed` (4000 символов) — общий
    для всех версий промпта предохранитель.
    """
    text = "строка\nвторая\n" + "я" * 1000
    assert f"=== СООБЩЕНИЕ КЛИЕНТА ===\n{text}\n===" in client_payload(text)
