"""The MCP server is the orchestrator's only tool surface, so it must never reach the local machine.

It may read its FCL_* settings from the environment and talk HTTP to FCL. Anything that could read or
write files, run commands, or load code is a way for the model to escape onto the laptop.
"""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "fcl" / "mcp_server.py"

ALLOWED_MODULES = {"__future__", "os", "uuid", "typing", "httpx", "anyio", "mcp", "mcp_types"}
ALLOWED_OS_ATTRS = {"environ"}
FORBIDDEN_NAMES = {"open", "eval", "exec", "compile", "__import__", "breakpoint", "input", "globals", "locals",
                   "getattr", "setattr", "vars"}


def _tree() -> ast.Module:
    return ast.parse(SOURCE.read_text(), filename=str(SOURCE))


def test_imports_only_http_and_mcp_modules() -> None:
    imported = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports would pull in FCL server modules that touch the database"
            imported.add((node.module or "").split(".")[0])
    assert imported <= ALLOWED_MODULES, f"unexpected imports: {sorted(imported - ALLOWED_MODULES)}"


def test_os_is_used_only_for_environment_variables() -> None:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
            assert node.attr in ALLOWED_OS_ATTRS, f"os.{node.attr} at line {node.lineno}"


def test_environment_reads_are_fcl_settings() -> None:
    for node in ast.walk(_tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "environ"):
            key = node.args[0]
            assert isinstance(key, ast.Constant) and str(key.value).startswith("FCL_"), f"line {node.lineno}"
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
            raise AssertionError(f"use os.environ.get with an FCL_ key, line {node.lineno}")


def test_no_file_or_dynamic_code_builtins() -> None:
    used = {n.id for n in ast.walk(_tree()) if isinstance(n, ast.Name)}
    assert not used & FORBIDDEN_NAMES, f"forbidden builtins: {sorted(used & FORBIDDEN_NAMES)}"
