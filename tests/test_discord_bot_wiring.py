"""`src/discord_bot.py` is wiring only and connects to Discord and Postgres when imported,
so the tests don't import it. This reads it instead: every keyword it passes to a function
from `src` must be a parameter of that function, and every required one must be passed.
"""

import ast
import importlib
import inspect
from pathlib import Path


def _imported_from_src(tree):
    names = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src"):
            for alias in node.names:
                names[alias.asname or alias.name] = (node.module, alias.name)
    return names


def test_discord_bot_passes_the_keywords_each_function_takes():
    tree = ast.parse(Path("src/discord_bot.py").read_text(encoding="utf-8"))
    imported = _imported_from_src(tree)
    checked = 0
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id not in imported:
            continue
        module, name = imported[node.func.id]
        target = getattr(importlib.import_module(module), name)
        if not callable(target) or any(keyword.arg is None for keyword in node.keywords):
            continue
        parameters = inspect.signature(target).parameters
        if any(parameter.kind is parameter.VAR_KEYWORD for parameter in parameters.values()):
            continue
        checked += 1
        passed = {keyword.arg for keyword in node.keywords}
        unknown = passed - set(parameters)
        required = {
            key for key, parameter in parameters.items()
            if parameter.kind is parameter.KEYWORD_ONLY and parameter.default is parameter.empty
        }
        missing = required - passed
        if unknown or missing:
            problems.append(f"line {node.lineno} {name}: unknown={sorted(unknown)} missing={sorted(missing)}")
    # The workers, the message store, the service and the Riot client are all wired with keywords.
    assert checked >= 20
    assert problems == []
