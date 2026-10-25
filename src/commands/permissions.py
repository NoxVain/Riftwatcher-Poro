"""Author permission policy for mutating `!` commands.

Channel routing (`routing.py`) decides *where* a command may run; this module decides *who*
may run commands that change shared state (tracked players, historical stats, bot settings).
"""

from src.constants import ADD_COMMAND, BACKFILL_COMMAND, CHAMPION_DISPLAY_COMMAND, REMOVE_COMMAND, TTS_COMMAND


def is_admin_command(content_lower: str) -> bool:
    """Return True for commands that mutate shared state.

    Bare usage forms (e.g. `!add` with no argument) and read-only forms (`!tts status`)
    stay open to everyone.
    """
    if content_lower.startswith(f"{ADD_COMMAND.casefold()} "):
        return True
    if content_lower.startswith(f"{REMOVE_COMMAND.casefold()} "):
        return True
    if content_lower.startswith(f"{BACKFILL_COMMAND.casefold()} "):
        return True
    if content_lower.startswith(f"{TTS_COMMAND.casefold()} "):
        arg = content_lower[len(TTS_COMMAND):].strip()
        return arg != "status"
    if content_lower.startswith(f"{CHAMPION_DISPLAY_COMMAND.casefold()} "):
        arg = content_lower[len(CHAMPION_DISPLAY_COMMAND):].strip()
        return arg != "status"
    return False


def has_admin_access(author, admin_role_id: int | None = None) -> bool:
    """Allow Administrator / Manage Server, or members holding the configured admin role.

    DM authors (`discord.User`) have no `guild_permissions` and are always denied.
    """
    permissions = getattr(author, "guild_permissions", None)
    if permissions is None:
        return False
    if getattr(permissions, "administrator", False) or getattr(permissions, "manage_guild", False):
        return True
    if admin_role_id:
        return any(getattr(role, "id", None) == admin_role_id for role in getattr(author, "roles", ()) or ())
    return False


def format_admin_denied_text(command_name: str, admin_role_id: int | None = None) -> str:
    requirement = "the **Manage Server** permission"
    if admin_role_id:
        requirement += f" or the <@&{admin_role_id}> role"
    return f"`{command_name}` requires {requirement}."


async def enforce_admin_permission(ctx) -> bool:
    """Reply and return True when the author is not allowed to run a mutating command."""
    if not is_admin_command(ctx.content_lower):
        return False
    if has_admin_access(ctx.message.author, ctx.admin_role_id):
        return False
    command_name = ctx.content.split(" ", 1)[0]
    await ctx.message.channel.send(format_admin_denied_text(command_name, ctx.admin_role_id))
    ctx.log(f"[auth] Denied {command_name} for author {getattr(ctx.message.author, 'id', '?')}.")
    return True
