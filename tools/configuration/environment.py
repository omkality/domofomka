"""Единая раскладка .env без вывода секретов и изменения уже заданных значений."""

import argparse
import io
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values
from dotenv.parser import parse_stream

ROOT = Path(__file__).resolve().parents[2]
ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.M)
DEV = {
    "DOMOFOMKA_ENV_FILE": ".env.dev",
    "API_PUBLISH_PORT": "18000",
    "VK_GROUP_ID": "223949627",
    "EXPECTED_VK_GROUP_ID": "223949627",
    "APP_ENVIRONMENT": "development",
    "TELEMETRY_ENABLED": "true",
}


def settings_defaults() -> dict[str, str | None]:
    """Прочитать объявленные defaults Pydantic, не используя личные секреты для метаданных."""
    placeholders = dotenv_values(ROOT / ".env.example")
    with patch.dict(os.environ, {k: v for k, v in placeholders.items() if v is not None}):
        from src.code_service.config import Settings
        from src.infrastructure.network import NetworkSettings
        from src.infrastructure.telemetry import TelemetrySettings
        from src.vkbot.config import VKBotSettings

    result = {}
    for model in [Settings, NetworkSettings, TelemetrySettings, VKBotSettings]:
        for name, field in model.model_fields.items():
            if field.is_required():
                value = None
            else:
                default = field.get_default()
                value = "" if default is None else str(default).lower() if isinstance(default, bool) else str(default)
            if name.upper() in result and result[name.upper()] != value:
                raise ValueError("Conflicting defaults for " + name.upper())
            result[name.upper()] = value
    return result


def bindings(source: str) -> dict[str, str]:
    """Сохранить исходное написание значений, включая кавычки, интерполяцию и комментарии."""
    result = {}
    for item in parse_stream(io.StringIO(source)):
        if item.error:
            raise ValueError("Invalid dotenv syntax at line " + str(item.original.line))
        if item.key is None:
            continue
        match = re.search(r"(?:^|\n)(?:export\s+)?" + re.escape(item.key) + r"\s*=(.*)", item.original.string, re.S)
        if match is None:
            raise ValueError("An explicit assignment is required for " + item.key)
        result[item.key] = match[1].rstrip("\r\n")
    return result


def template(profile: str) -> str:
    """Взять одну структуру examples, переопределив только значения профиля dev."""
    source = (ROOT / ".env.example").read_text(encoding="utf-8")
    if profile == "dev":
        for name, value in DEV.items():
            source = re.sub(r"^" + name + r"=.*$", name + "=" + value, source, flags=re.M)
    return source


def normalize(source: str, profile: str) -> str:
    """Привести файл к общей раскладке и явно дописать прежние неявные defaults."""
    original = bindings(source)
    defaults = settings_defaults()
    lines = []
    for template_line in template(profile).splitlines():
        line = template_line
        match = ASSIGNMENT.match(line)
        if match:
            name = match[1]
            if name in original:
                line = name + "=" + original.pop(name)
            elif name in defaults:
                if defaults[name] is None:
                    raise ValueError("Missing required setting " + name)
                line = name + "=" + defaults[name]
        lines.append(line)
    if original:
        lines.extend(["", "# Дополнительные параметры окружения; сохранены без изменения значений."])
        lines.extend(name + "=" + value for name, value in sorted(original.items()))
    normalized = "\n".join(lines).rstrip() + "\n"
    before = dotenv_values(stream=io.StringIO(source), interpolate=False)
    after = dotenv_values(stream=io.StringIO(normalized), interpolate=False)
    if any(after.get(name) != value for name, value in before.items()):
        raise ValueError("Normalization changed an existing setting")
    return normalized


def check_examples() -> None:
    """Проверить полноту примеров и одинаковый порядок всех параметров."""
    expected = template("prod")
    names = ASSIGNMENT.findall(expected)
    if len(names) != len(set(names)):
        raise ValueError("Duplicate example setting")
    missing = set(settings_defaults()) - set(names)
    if missing:
        raise ValueError("Missing explicit settings: " + ", ".join(sorted(missing)))
    if (ROOT / ".env.dev.example").read_text(encoding="utf-8") != template("dev"):
        raise ValueError("Dev example layout differs from the shared template")


def main() -> None:
    """Проверить примеры или оформить указанный .env с закрытой резервной копией."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-examples", action="store_true")
    parser.add_argument("--refresh-dev-example", action="store_true")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--profile", choices=["dev", "prod"], default="dev")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.refresh_dev_example:
        (ROOT / ".env.dev.example").write_text(template("dev"), encoding="utf-8")
    if args.check_examples:
        check_examples()
        sys.stdout.write("Environment examples are complete and consistently ordered.\n")
    if args.file:
        source = args.file.read_text(encoding="utf-8")
        result = normalize(source, args.profile)
        if args.write and source != result:
            backup = ROOT / ".codex-local/env-backups"
            backup.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            (backup / (args.file.name + "." + stamp)).write_text(source, encoding="utf-8")
            args.file.write_text(result, encoding="utf-8")
        sys.stdout.write(
            "Environment format " + ("updated" if args.write else "checked") + "; existing values preserved.\n"
        )


if __name__ == "__main__":
    main()
