"""Canonical startup configuration, independent of the process working directory."""
from __future__ import annotations

import os
from pathlib import Path

import dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RuntimeConfigurationError(RuntimeError):
    """A safe configuration error that never includes configured values."""


def resolve_database_path(value: str | os.PathLike[str] | None, *, project_root: Path | None = None) -> Path:
    """Resolve one explicit/default SQLite file without creating or moving it."""
    if value is not None:
        value = os.fspath(value)
    if value is not None and (not value.strip() or "\x00" in value or value == ":memory:" or value.startswith("file:")):
        raise RuntimeConfigurationError("DATABASE_PATH must name a SQLite file.")
    try:
        root = (project_root or PROJECT_ROOT).resolve()
        default = root / "crm.sqlite3"
        previous_default = root / "data" / "crm.sqlite3"
        if value is None:
            if previous_default.exists():
                raise RuntimeConfigurationError("Choose an existing database explicitly with DATABASE_PATH before startup.")
            selected = default
        else:
            selected = Path(value)
            if not selected.is_absolute():
                selected = root / selected
        selected = selected.resolve()
        if selected.is_dir():
            raise RuntimeConfigurationError("DATABASE_PATH must name a SQLite file.")
        if not selected.exists() and any(candidate.exists() for candidate in (default, previous_default)):
            raise RuntimeConfigurationError("DATABASE_PATH does not select the existing database; startup was refused.")
    except RuntimeConfigurationError:
        raise
    except (OSError, RuntimeError):
        raise RuntimeConfigurationError("DATABASE_PATH cannot be resolved safely.") from None
    return selected


def load_runtime_config() -> Path:
    """Load only this installation's dotenv file; hosting environment wins."""
    dotenv.load_dotenv(PROJECT_ROOT / ".env", override=False)
    database_path = resolve_database_path(os.environ.get("DATABASE_PATH"))
    os.environ["DATABASE_PATH"] = str(database_path)
    return database_path
