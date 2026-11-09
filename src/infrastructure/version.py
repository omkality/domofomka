import tomllib
from pathlib import Path


def get_app_info() -> dict[str, str]:
    """Получение информации о приложении из pyproject.toml"""
    current_file_path = Path(__file__).resolve()
    project_root = current_file_path.parents[2]
    pyproject_path = project_root / "pyproject.toml"

    if not pyproject_path.exists():
        raise FileNotFoundError(f"Файл {pyproject_path} не найден.")

    with open(pyproject_path, "rb") as f:
        info: dict = tomllib.load(f)

    project: dict[str, str] = info.get("project", {})
    return {
        "name": project.get("name", "unknown"),
        "version": project.get("version", "0.0.0"),
        "description": project.get("description", ""),
    }
