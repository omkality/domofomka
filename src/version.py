from pathlib import Path

import toml


def get_app_info() -> dict[str, str]:
    """Получение информации о приложении из pyproject.toml"""
    current_file_path = Path(__file__).resolve()
    project_root = current_file_path.parents[1]
    pyproject_path = project_root / "pyproject.toml"

    if not pyproject_path.exists():
        raise FileNotFoundError(f"Файл {pyproject_path} не найден.")

    with open(pyproject_path, encoding="utf-8") as f:
        info: dict = toml.load(f)

    project: dict[str, str] = info.get("project", {})
    return {
        "name": project.get("name", "unknown"),
        "version": project.get("version", "0.0.0"),
        "description": project.get("description", ""),
    }
