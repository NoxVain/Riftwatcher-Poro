"""Round-trip real Discord component payloads in message tests."""

import discord
from discord.components import _component_factory


def view_text(view):
    return "\n\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


class ComponentMessage:
    def __init__(self, message_id, content="", view=None):
        self.id = message_id
        self.content = content or ""
        self.components = [_component_factory(data) for data in view.to_components()] if view else []
        self.edits = []

    @property
    def display(self):
        if self.components:
            return view_text(discord.ui.LayoutView.from_message(self, timeout=None))
        return self.content

    async def edit(self, *, content=None, view=None, allowed_mentions=None):
        if view is not None:
            self.components = [_component_factory(data) for data in view.to_components()]
            self.edits.append(view.to_components())
        if content is not None:
            self.content = content
            self.edits.append(content)
        return self
