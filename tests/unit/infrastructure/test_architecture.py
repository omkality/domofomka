"""Проверка границ компонентов и версий по реальным импортам всего src."""

import ast

import pytest

from tests.paths import ROOT


def imported_modules(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, (path, "Use absolute imports to expose component boundaries")
            yield node.module or ""
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


@pytest.mark.parametrize(
    ("component", "forbidden"),
    [
        ("code_service", ("src.vkbot",)),
        ("vkbot", ("src.code_service",)),
        ("infrastructure", ("src.code_service", "src.vkbot")),
        ("code_service/services/v1", ("src.code_service.services.v2",)),
        ("code_service/services", ("src.code_service.api", "src.code_service.main", "litestar")),
    ],
)
def test_components_only_depend_on_allowed_layers(component, forbidden):
    paths = list((ROOT / "src" / component).rglob("*.py"))
    assert paths, "Do not silently check a removed directory"
    violations = [(str(p.relative_to(ROOT)), m) for p in paths for m in imported_modules(p) if m.startswith(forbidden)]
    assert not violations


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_version_has_separate_routes_and_services(version):
    folder = ROOT / "src/code_service/services" / version
    assert (folder / "search.py").is_file()
    modules = list(imported_modules(ROOT / "src/code_service/api/routes" / version / "codes.py"))
    assert f"src.code_service.services.{version}" in modules
    assert not any(m.startswith(("sqlite3", "psycopg", "httpx")) for m in modules)
    for path in folder.rglob("*.py"):
        if path.name != "routes.py":
            assert not any(m.startswith(("litestar", "src.code_service.main")) for m in imported_modules(path))


def test_obsolete_packages_are_removed():
    for name in ["storages", "api", "application", "services", "search"]:
        assert not (ROOT / "src" / name).exists()
    assert not (ROOT / "src/vkbot/handlers.py").exists()


def test_contextmanager_annotations_use_generators():
    for base in ["src", "tools"]:
        for path in (ROOT / base).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf8"))):
                if isinstance(node, ast.FunctionDef) and any(
                    isinstance(d, ast.Name) and d.id == "contextmanager" for d in node.decorator_list
                ):
                    assert node.returns is not None, path
                    assert ast.unparse(node.returns).startswith("Generator["), (path, node.name)
