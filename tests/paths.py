"""Корень проекта для тестов, не зависящий от глубины их каталогов."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
