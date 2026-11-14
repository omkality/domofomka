"""Сравнение русских порядковых числительных; внешние пакеты нужны только для этого запуска.

python -m tools.experiments.numeral_parsers --output artifacts/private/numerals.json
Контроли синтетические. Результаты не означают качество поиска по перепискам.
"""

import argparse
import json
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

from src.code_service.services.v2.normalize import ordinals

CASES = [
    ("первый", "1"),
    ("первая", "1"),
    ("второго", "2"),
    ("восьмая", "8"),
    ("восьмой", "8"),
    ("десятая", "10"),
    ("третья", "3"),
    ("третьего", "3"),
    ("третий", "3"),
    ("двадцать первая", "21"),
    ("двадцать первый", "21"),
    ("тридцать третья", "33"),
    ("сороковая", "40"),
    ("девяносто девятая", "99"),
    ("сотая", "100"),
    ("пятницкая", "пятницкая"),
    ("третийский", "третийский"),
    ("первомайская", "первомайская"),
    ("восьмидесятилетия", "восьмидесятилетия"),
    ("двухсотлетия", "двухсотлетия"),
]


def compare() -> dict:
    # Do not make optional comparison packages dependencies of the API/bot.
    """Сравнить результаты одинаковых запросов на проверяемых вариантах."""
    from number_parser import parse
    from ru_word2number.w2n import word_to_num

    def ru_word(value: str) -> str:
        """Проверить преобразование числительного выбранной внешней библиотекой."""
        try:
            return str(word_to_num(value))
        except ValueError:
            return value

    parsers = {
        "address_rules": ordinals,
        "number-parser": lambda value: parse(value, language="ru"),
        "ru-word2number": ru_word,
    }
    results = {}
    for name, parser in parsers.items():
        start = perf_counter()
        rows = []
        for source, expected in CASES:
            actual = parser(source)
            rows.append({"input": source, "expected": expected, "actual": actual, "correct": actual == expected})
        results[name] = {
            "correct": sum(r["correct"] for r in rows),
            "total": len(rows),
            "ms": round((perf_counter() - start) * 1000, 3),
            "cases": rows,
        }
    return {
        "versions": {name: version(name) for name in ["number-parser", "ru-word2number"]},
        "results": results,
    }


def main() -> None:
    """Сравнение русских порядковых числительных; внешние пакеты нужны только для этого запуска."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(
        json.dumps(
            {name: {k: v for k, v in result.items() if k != "cases"} for name, result in report["results"].items()}
        )
    )


if __name__ == "__main__":
    main()
