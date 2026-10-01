"""Рассылка: одинаковый текст длиннее 60 символов в 3+ чатах за 10 минут."""

from datetime import datetime, timedelta, timezone

from app.services.broadcasts import broadcast_body, broadcast_message_ids

T0 = datetime(2026, 9, 10, 7, 1, tzinfo=timezone.utc)
LONG = (
    "Нина Козлова [corp.example.com] пишет:\n\nДобрый день.\n"
    "Уведомляю о том, что с 10.08 по 23.08 буду находиться в отпуске. "
    "Если что-то срочное, заменяет меня Ксения."
)


def _row(n: int, chat: int, minutes: float, text: str = LONG):
    return (n, chat, T0 + timedelta(minutes=minutes), text)


def test_same_text_in_three_chats_within_ten_minutes_is_a_broadcast():
    rows = [_row(1, 10, 0), _row(2, 11, 3), _row(3, 12, 9)]
    assert broadcast_message_ids(rows) == {1, 2, 3}


def test_two_chats_are_not_a_broadcast():
    rows = [_row(1, 10, 0), _row(2, 11, 3), _row(3, 10, 5)]
    assert broadcast_message_ids(rows) == set()


def test_window_is_ten_minutes():
    rows = [_row(1, 10, 0), _row(2, 11, 3), _row(3, 12, 10.5)]
    assert broadcast_message_ids(rows) == set()
    # Третий чат догоняет позже: окно, начатое во втором сообщении, его захватывает.
    rows.append(_row(4, 13, 12))
    assert broadcast_message_ids(rows) == {2, 3, 4}


def test_short_text_is_never_a_broadcast_even_with_signature():
    """«Добрый день, передала запрос бухгалтеру» в трёх чатах — живые передачи.

    С префиксом интегратора такой текст длиннее 60 символов, но тело — 39,
    и правило его не трогает: иначе пропала бы настоящая передача специалисту.
    """
    text = "Ирина Соколова [corp.example.com] пишет:\n\nДобрый день, передала запрос бухгалтеру"
    rows = [_row(1, 10, 0, text), _row(2, 11, 1, text), _row(3, 12, 2, text)]
    assert broadcast_body(text) is None
    assert broadcast_message_ids(rows) == set()


def test_body_ignores_prefix_and_whitespace():
    a = "Ирина Соколова [corp.example.com] пишет:\n\nДобрый день!  В банке подготовила платежку на уплату страховых взносов до 25.08"
    b = "Ирина Соколова [corp.example.​com] пишет: \n\nДобрый день! В банке подготовила платежку на уплату страховых взносов до 25.08"
    assert broadcast_body(a) == broadcast_body(b)
    assert broadcast_body(None) is None


def test_message_from_other_series_is_untouched():
    rows = [_row(1, 10, 0), _row(2, 11, 1), _row(3, 12, 2), _row(4, 13, 3, LONG + " Спасибо.")]
    assert broadcast_message_ids(rows) == {1, 2, 3}
