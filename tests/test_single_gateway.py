"""The single-gateway invariant guard (CLAUDE.md invariants #1 and #7; PRD §7, §6.5.1).

A submission that routes any AI call outside Mesh is disqualified. This test statically proves, by
parsing the AST of every Python file under ``app/ scripts/ evals/ tests/`` (not loose grepping):

  1. an LLM client (``AsyncOpenAI``/``OpenAI``) is constructed in **exactly one** file — ``app/llm/mesh.py``;
  2. **no** banned provider SDK is imported anywhere in the codebase;
  3. ``requirements.txt`` declares **none** of the banned provider SDKs.

Never weaken or skip this test. It is the highest-stakes invariant in the whole submission.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("app", "scripts", "evals", "tests")

# The one and only file permitted to construct an LLM client (PRD §7).
ALLOWED_CLIENT_FILE = "app/llm/mesh.py"

# Client constructor identifiers to detect (as either a bare Name or an attribute access).
CLIENT_CTORS = {"OpenAI", "AsyncOpenAI"}

# Banned runtime provider SDKs — as pip package names (requirements.txt) and as import module roots.
# `anthropic` and `openai` model *strings* (e.g. "anthropic/claude-sonnet-4.5") are fine: they are
# string literals resolved by Mesh, not imports — which is exactly why this check parses the AST.
BANNED_PIP_PACKAGES = {
    "groq",
    "openrouter",
    "anthropic",
    "google-generativeai",
    "google-genai",
    "together",
    "sentence-transformers",
    "fastembed",
}
BANNED_IMPORT_ROOTS = {
    "groq",
    "openrouter",
    "anthropic",
    "google.generativeai",
    "google.genai",
    "together",
    "sentence_transformers",
    "fastembed",
}


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _parse(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - surfaced as a test failure with the filename
        raise AssertionError(f"{_rel(path)} failed to parse: {exc}") from exc


def _constructs_client(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in CLIENT_CTORS:
            return True
        if isinstance(func, ast.Attribute) and func.attr in CLIENT_CTORS:
            return True
    return False


def _imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:  # skip relative `from . import x`
                roots.add(node.module)
    return roots


def _is_banned_import(module: str) -> bool:
    return any(module == root or module.startswith(root + ".") for root in BANNED_IMPORT_ROOTS)


def _normalize_pip_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _requirement_package_names() -> set[str]:
    names: set[str] = set()
    text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()  # drop comments (the header names the banned SDKs)
        if not line:
            continue
        # Package name is everything before a version specifier, extras bracket, or env marker.
        package = line
        for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", ";", " "):
            package = package.split(separator, 1)[0]
        if package:
            names.add(_normalize_pip_name(package))
    return names


def test_exactly_one_client_construction_site() -> None:
    construction_files = sorted(
        _rel(path) for path in _iter_python_files() if _constructs_client(_parse(path))
    )
    assert construction_files == [ALLOWED_CLIENT_FILE], (
        "Exactly one file may construct an LLM client (CLAUDE.md invariant #1). "
        f"Found construction sites in: {construction_files or 'none'}"
    )


def test_no_banned_provider_sdk_imports() -> None:
    violations: list[str] = []
    for path in _iter_python_files():
        for module in _imported_roots(_parse(path)):
            if _is_banned_import(module):
                violations.append(f"{_rel(path)} imports {module!r}")
    assert not violations, (
        "No banned provider SDK may be imported (CLAUDE.md invariants #1 and #7). "
        f"Violations: {violations}"
    )


def test_requirements_have_no_banned_sdk() -> None:
    declared = _requirement_package_names()
    banned_present = sorted(declared & BANNED_PIP_PACKAGES)
    assert not banned_present, (
        "requirements.txt must not declare any banned provider SDK (CLAUDE.md invariant #1). "
        f"Found: {banned_present}"
    )
    # Sanity: the one allowed LLM client SDK must still be present (critical CI check, PRD §2).
    assert "openai" in declared, "requirements.txt must declare the `openai` client SDK."
