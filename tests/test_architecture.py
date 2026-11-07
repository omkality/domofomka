import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("folder", ["application", "search", "services"])
def test_search_and_services_do_not_depend_on_http_controllers(folder):
    violations = []
    for path in (ROOT / "src" / folder).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf8"))):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for name in names:
                if name == "litestar" or name.startswith(("litestar.", "src.api")):
                    violations.append((str(path.relative_to(ROOT)), name))
    assert not violations


def test_unused_storage_protocol_and_sequential_bot_handler_are_removed():
    assert not (ROOT / "src/storages").exists()
    assert not (ROOT / "src/vkbot/handlers.py").exists()


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_routes_use_application_scenarios_without_database_or_transport_clients(version):
    path = ROOT / "src/api" / version / "routes.py"
    modules = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf8"))):
        if isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    assert "src.application" in modules
    assert not any(name.startswith(("sqlite3", "psycopg", "httpx", "src.search")) for name in modules)
