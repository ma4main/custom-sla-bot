"""Поиск по справочнику и пагинация при привязке учётки к сотруднику:
найти можно любого действующего сотрудника, а не только первых двадцать."""

from app.db.models import Staff
from app.services.staff import list_staff, normalize_name
from tests.conftest import requires_db

NAMES = [
    "Ирина Соколова",
    "Лариса Карпова",
    "Вера Павлова",
    "Дарья Лебедева",
    "Пётр Семёнов",
    "Иван Петров",
]


async def _fill(session) -> None:
    for name in NAMES:
        session.add(
            Staff(full_name=name, normalized_name=normalize_name(name), active=True)
        )
    # Уволенный однофамилец не должен всплывать в выборе.
    session.add(
        Staff(
            full_name="Пётр Уволенный",
            normalized_name=normalize_name("Пётр Уволенный"),
            active=False,
        )
    )
    await session.flush()


@requires_db
async def test_search_finds_by_part_of_name(session):
    await _fill(session)

    people, total = await list_staff(session, active_only=True, query="сокол")

    assert total == 1
    assert [p.full_name for p in people] == ["Ирина Соколова"]


@requires_db
async def test_search_ignores_case_and_yo(session):
    """«Семёнов» и «Семенов» обязаны находиться одинаково.

    Ищем по нормализованному имени — тому же полю, по которому работает
    атрибуция, иначе человек не нашёл бы сотрудника, которого система
    прекрасно узнаёт в сообщениях.
    """
    await _fill(session)

    for needle in ("СЕМЁНОВ", "семенов", "Семёнов"):
        people, total = await list_staff(session, active_only=True, query=needle)
        assert total == 1, needle
        assert people[0].full_name == "Пётр Семёнов", needle


@requires_db
async def test_search_matches_everyone_who_fits(session):
    """«петр» — это и Семёнов, и Петров: подстрока, а не точное имя."""
    await _fill(session)

    people, total = await list_staff(session, active_only=True, query="петр")

    assert total == 2
    assert sorted(p.full_name for p in people) == ["Иван Петров", "Пётр Семёнов"]


@requires_db
async def test_search_skips_inactive(session):
    """Уволенный не должен предлагаться к привязке."""
    await _fill(session)

    _, total = await list_staff(session, active_only=True, query="уволенн")

    assert total == 0


@requires_db
async def test_wildcards_are_literal(session):
    """«%» вводит человек, и это не подстановка, а сам символ процента."""
    await _fill(session)

    _, total = await list_staff(session, active_only=True, query="%")

    assert total == 0, "процент сработал как шаблон и вернул весь справочник"


@requires_db
async def test_empty_query_is_the_whole_list(session):
    await _fill(session)

    _, total = await list_staff(session, active_only=True, query="   ")

    assert total == len(NAMES)


@requires_db
async def test_pagination_covers_everyone(session):
    """Каждый попадает ровно на одну страницу — иначе кого-то не привязать."""
    await _fill(session)

    seen: list[str] = []
    page = 0
    while True:
        people, total = await list_staff(
            session, offset=page * 2, limit=2, active_only=True
        )
        if not people:
            break
        seen.extend(p.full_name for p in people)
        page += 1

    assert sorted(seen) == sorted(NAMES)
    assert total == len(NAMES)
