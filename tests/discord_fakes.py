"""Fakes for card (Components V2) messages that round-trip real Discord component payloads.

A sent view is serialized with `to_components()` and parsed back into discord.py
component objects, so `LayoutView.from_message` (used by the LP edit) sees exactly
what Discord would return.
"""

import discord
from discord.components import _component_factory

CONTAINER_TEXT_SEPARATOR = "\n\n---\n\n"


def view_text(view):
    """Card text as the plain-text recap would read: containers separated like sections."""
    sections = []
    for container in view.children:
        items = container.walk_children() if hasattr(container, "walk_children") else [container]
        texts = [item.content for item in items if isinstance(item, discord.ui.TextDisplay)]
        sections.append("\n\n".join(texts))
    return CONTAINER_TEXT_SEPARATOR.join(sections)


class ComponentMessage:
    def __init__(self, message_id, content="", view=None):
        self.id = message_id
        self.content = content or ""
        self.components = self._parse(view)
        self.edits = []

    @staticmethod
    def _parse(view):
        return [_component_factory(data) for data in view.to_components()] if view is not None else []

    @property
    def display(self):
        if self.components:
            return view_text(discord.ui.LayoutView.from_message(self, timeout=None))
        return self.content

    async def edit(self, *, content=None, view=None):
        if view is not None:
            self.components = self._parse(view)
            self.edits.append("view")
        if content is not None:
            self.content = content
            self.edits.append("content")
        return self
