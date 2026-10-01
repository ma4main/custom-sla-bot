"""Проверка живости для docker healthcheck: `python -m app.health bot|worker`.

Каждый процесс отмечает в файле конец очередного круга работы; проверка смотрит
на свежесть отметки (код 0 — жив, 1 — отметка устарела или её нет). compose не
перезапускает контейнер по unhealthy, поэтому `start_watchdog` в отдельном потоке
(не asyncio: может зависнуть сам цикл) завершает процесс, и срабатывает `restart`.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

HEARTBEAT_DIR = Path("/tmp/heartbeat")

# Предел свежести отметки. С запасом: перезапустить работающий процесс хуже,
# чем узнать о зависшем на пару минут позже.
MAX_AGE_SECONDS = {"bot": 600, "worker": 300}


def beat(name: str) -> None:
    try:
        HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
        (HEARTBEAT_DIR / f"{name}.beat").write_text(str(int(time.time())))
    except OSError:
        # Ошибка записи отметки не роняет процесс.
        pass


def beat_age(name: str) -> int | None:
    try:
        stamp = int((HEARTBEAT_DIR / f"{name}.beat").read_text().strip())
    except (OSError, ValueError):
        return None
    return int(time.time()) - stamp


def check(name: str) -> int:
    limit = MAX_AGE_SECONDS.get(name, 300)
    age = beat_age(name)
    if age is None:
        print(f"{name}: отметки нет — процесс ещё не отработал ни одного круга")
        return 1

    if age > limit:
        print(f"{name}: последний круг {age} с назад, предел {limit} с")
        return 1
    print(f"{name}: жив, последний круг {age} с назад")
    return 0


WATCHDOG_INTERVAL_SECONDS = 30


def watchdog_reason(age: int | None, uptime: float, limit: int) -> str | None:
    """Почему процесс пора убить; None — всё в порядке. Отсутствие отметки терпится
    `limit` секунд от старта сторожа, чтобы медленный старт не убивал процесс.
    """
    if age is None:
        if uptime <= limit:
            return None
        return f"ни одного круга за {limit} с после запуска"
    if uptime <= limit and age > uptime:
        # Отметка старше процесса осталась от прошлого запуска (файл переживает рестарт)
        # и в отсрочке после старта равносильна отсутствию отметки.
        return None
    if age > limit:
        return f"последний круг {age} с назад, предел {limit} с"
    return None


def start_watchdog(name: str) -> threading.Thread:
    limit = MAX_AGE_SECONDS.get(name, 300)
    started = time.time()

    def loop() -> None:
        while True:
            time.sleep(WATCHDOG_INTERVAL_SECONDS)
            reason = watchdog_reason(beat_age(name), time.time() - started, limit)
            if reason is None:
                continue

            # Жёсткий выход: обычный ждал бы завершения зависшего цикла.
            print(f"watchdog: {name} завис — {reason}; выхожу", file=sys.stderr, flush=True)
            os._exit(1)

    thread = threading.Thread(target=loop, name=f"watchdog-{name}", daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "worker"
    sys.exit(check(target))
