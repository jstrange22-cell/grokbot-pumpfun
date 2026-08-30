"""Kill-switch: файл на диске закрывает новые покупки.

Это ручной стоп без рестарта и без смены конфига. Пока файл существует,
пайплайн не открывает новые позиции. Открытые продолжает вести: стоп-лосс
и остальные выходы должны работать, иначе «стоп» оставляет деньги без
присмотра.

Путь: $GROKBOT_KILL_FILE, по умолчанию ./KILL. Достаточно `touch KILL`.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_KILL_FILE = "KILL"
ENV_KILL_FILE = "GROKBOT_KILL_FILE"

__all__ = [
    "DEFAULT_KILL_FILE",
    "ENV_KILL_FILE",
    "is_killed",
    "kill_file_path",
]


def kill_file_path(env: dict[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    raw = environ.get(ENV_KILL_FILE, DEFAULT_KILL_FILE)
    return Path(raw if raw else DEFAULT_KILL_FILE)


def is_killed(env: dict[str, str] | None = None) -> bool:
    """True, если оператор положил kill-файл. Новые покупки закрыты."""
    return kill_file_path(env).exists()
