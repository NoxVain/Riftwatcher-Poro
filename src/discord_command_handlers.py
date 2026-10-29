from src.commands.analysis_handlers import handle_analysis_commands
from src.commands.records_handlers import handle_records_commands
from src.commands.wrapped_handlers import handle_wrapped_commands
from src.commands.context import CommandHandlerContext
from src.commands.ops_handlers import handle_ops_commands
from src.commands.permissions import enforce_admin_permission
from src.commands.player_handlers import handle_player_commands
from src.commands.report_handlers import handle_report_commands
from src.commands.routing import enforce_command_channel, is_supported_command


async def handle_incoming_message(
    *,
    message,
    channel_id,
    friends,
    riot_client,
    poro_service,
    report_timezone_name,
    report_day_start_hour,
    db_enabled,
    start_monotonic,
    daily_request_lock,
    request_id_context,
    create_request_id,
    get_or_create_report_message,
    remember_report_message,
    normalize_riot_id,
    db_upsert_player,
    log,
    db_remove_player=None,
    get_or_create_weekly_report_message=None,
    remember_weekly_report_message=None,
    weekly_report_channel_id=None,
    events_channel_id=None,
    resolve_channel=None,
    worker_stats=None,
    db_get_state=None,
    db_set_state=None,
    match_recap_channel_id=None,
    admin_role_id=None,
    db_load_tracked_puuids=None,
    db_load_player_analysis_rows=None,
    db_load_role_group_totals=None,
    db_iter_record_rows=None,
    db_iter_wrapped_rows=None,
    db_load_all_ranked_states=None,
):
    content = message.content.strip()
    content_lower = content.casefold()
    daily_channel_id = channel_id
    weekly_channel_id = weekly_report_channel_id or daily_channel_id
    events_channel_id = events_channel_id or daily_channel_id

    ctx = CommandHandlerContext(
        message=message,
        content=content,
        content_lower=content_lower,
        daily_channel_id=daily_channel_id,
        weekly_channel_id=weekly_channel_id,
        events_channel_id=events_channel_id,
        match_recap_channel_id=match_recap_channel_id,
        friends=friends,
        riot_client=riot_client,
        poro_service=poro_service,
        report_timezone_name=report_timezone_name,
        report_day_start_hour=report_day_start_hour,
        db_enabled=db_enabled,
        start_monotonic=start_monotonic,
        daily_request_lock=daily_request_lock,
        request_id_context=request_id_context,
        create_request_id=create_request_id,
        get_or_create_report_message=get_or_create_report_message,
        remember_report_message=remember_report_message,
        normalize_riot_id=normalize_riot_id,
        db_upsert_player=db_upsert_player,
        db_remove_player=db_remove_player,
        log=log,
        get_or_create_weekly_report_message=get_or_create_weekly_report_message,
        remember_weekly_report_message=remember_weekly_report_message,
        weekly_report_channel_id=weekly_report_channel_id,
        resolve_channel=resolve_channel,
        worker_stats=worker_stats,
        db_get_state=db_get_state,
        db_set_state=db_set_state,
        admin_role_id=admin_role_id,
        db_load_tracked_puuids=db_load_tracked_puuids,
        db_load_player_analysis_rows=db_load_player_analysis_rows,
        db_load_role_group_totals=db_load_role_group_totals,
        db_iter_record_rows=db_iter_record_rows,
        db_iter_wrapped_rows=db_iter_wrapped_rows,
        db_load_all_ranked_states=db_load_all_ranked_states,
    )

    if await enforce_command_channel(ctx):
        return
    if not content.startswith("!") or not is_supported_command(content_lower):
        return

    token = request_id_context.set(create_request_id("cmd"))
    try:
        if await enforce_admin_permission(ctx):
            return
        if await handle_ops_commands(ctx):
            return
        if await handle_analysis_commands(ctx):
            return
        if await handle_records_commands(ctx):
            return
        if await handle_wrapped_commands(ctx):
            return
        if await handle_player_commands(ctx):
            return
        if await handle_report_commands(ctx):
            return
    finally:
        request_id_context.reset(token)
