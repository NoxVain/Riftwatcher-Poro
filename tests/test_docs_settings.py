"""Every setting the bot reads is named in the four places CONTRIBUTING.md lists."""

import re
from pathlib import Path

import pytest

SETTING_READ = re.compile(r'(?:get_env_\w+|require_env|os\.getenv|os\.environ\.get)\(\s*"([A-Z0-9_]+)"')
DOCS = ("README.md", "SELF_HOSTING.md", ".env.example", "AGENTS.md")


def settings_read_by_the_code():
    names = set()
    for path in Path("src").rglob("*.py"):
        names.update(SETTING_READ.findall(path.read_text(encoding="utf-8")))
    return names


@pytest.mark.parametrize("doc", DOCS)
def test_every_setting_is_named_in_the_docs(doc):
    settings = settings_read_by_the_code()
    # Guard against the pattern silently matching nothing.
    assert {"DISCORD_TOKEN", "DATABASE_URL", "REPORT_TIMEZONE"} <= settings
    text = Path(doc).read_text(encoding="utf-8")
    missing = sorted(name for name in settings if not re.search(rf"\b{name}\b", text))
    assert missing == [], f"{doc} does not mention: {', '.join(missing)}"
