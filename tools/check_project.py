from __future__ import annotations

import ast
import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

MAX_RECOMMENDED_LINES = {
    "app/main.py": 2500,
    "app/repository.py": 1200,
    "app/connectors/ozon.py": 1200,
    "app/static/app.js": 2200,
    "app/static/styles.css": 3500,
}


def compile_python() -> list[str]:
    errors: list[str] = []
    for path in APP.rglob("*.py"):
        try:
            py_compile.compile(str(path), doraise=True)
        except Exception as exc:
            errors.append(f"{path.relative_to(ROOT)}: {exc}")
    return errors


def longest_python_functions(limit: int = 15) -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = []
    for path in APP.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and getattr(node, "end_lineno", None):
                length = int(node.end_lineno) - int(node.lineno) + 1
                rows.append((length, str(path.relative_to(ROOT)), node.name))
    return sorted(rows, reverse=True)[:limit]


def line_count_warnings() -> list[str]:
    warnings: list[str] = []
    for rel, max_lines in MAX_RECOMMENDED_LINES.items():
        path = ROOT / rel
        if not path.exists():
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > max_lines:
            warnings.append(f"{rel}: {lines} lines; recommended to split after {max_lines}")
    return warnings


def main() -> int:
    print("Arti CRM project check")
    print("=" * 24)

    errors = compile_python()
    if errors:
        print("\nPython compile errors:")
        for error in errors:
            print(" -", error)
        return 1

    print("\nPython compile: OK")

    warnings = line_count_warnings()
    if warnings:
        print("\nStructure warnings:")
        for warning in warnings:
            print(" -", warning)
    else:
        print("\nStructure warnings: none")

    print("\nLongest Python functions:")
    for length, rel, name in longest_python_functions():
        print(f" - {rel}:{name} — {length} lines")

    print("\nDone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
