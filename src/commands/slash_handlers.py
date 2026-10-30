from src.constants import SYNC_COMMAND
from src.discord_text import format_health_report, format_user_error


async def handle_health_slash(
    interaction,
    *,
    commands_channel_id,
    poro_service,
    start_monotonic,
    worker_stats,
    log,
):
    """Handle `/health`: restricted to the commands channel, deferred to survive slow checks."""
    if interaction.channel_id != commands_channel_id:
        await interaction.response.send_message(
            f"Use `/health` in <#{commands_channel_id}>.",
            ephemeral=True,
        )
        return

    # Acknowledge within Discord's 3s window; the health check can hit DB/Riot and be slower.
    await interaction.response.defer(thinking=True)
    try:
        stats = await poro_service.run_health_check(start_monotonic, worker_stats=worker_stats)
    except Exception as exc:
        log(f"[health] Slash health check failed: {type(exc).__name__}: {exc}")
        await interaction.followup.send(format_user_error("Health check failed", exc))
        return
    await interaction.followup.send(format_health_report(stats, title="**Health OK**"))
    log("[health] Sent slash health status message.")


async def handle_sync_command(message, *, tree, log):
    """Handle `!sync`: copy global slash commands to the current guild (admins only)."""
    guild = getattr(message, "guild", None)
    permissions = getattr(message.author, "guild_permissions", None)
    if guild is None or permissions is None:
        await message.channel.send(f"`{SYNC_COMMAND}` can only be used in a server channel.")
        return

    if not permissions.administrator:
        await message.channel.send("You need administrator permissions to sync commands.")
        return

    status = await message.channel.send("Syncing slash commands...")
    try:
        # Copy global commands to this specific guild for instant updates.
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        await status.edit(content=f"Successfully synced {len(synced)} commands to this server.")
        log(f"[startup] Synced {len(synced)} guild commands via {SYNC_COMMAND}.")
    except Exception as exc:
        await status.edit(content=format_user_error("Failed to sync commands", exc))
        log(f"[error] Sync failed: {type(exc).__name__}: {exc}")
