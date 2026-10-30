from dataclasses import dataclass
from typing import Any


@dataclass
class CommandHandlerContext:
    message: Any
    content: str
    content_lower: str
    daily_channel_id: int
    weekly_channel_id: int
    events_channel_id: int
    match_recap_channel_id: int | None
    friends: list[str]
    riot_client: Any
    poro_service: Any
    report_timezone_name: str
    report_day_start_hour: int
    db_enabled: bool
    start_monotonic: float
    daily_request_lock: Any
    request_id_context: Any
    create_request_id: Any
    get_or_create_report_message: Any
    remember_report_message: Any
    normalize_riot_id: Any
    db_upsert_player: Any
    log: Any
    get_or_create_weekly_report_message: Any = None
    remember_weekly_report_message: Any = None
    weekly_report_channel_id: int | None = None
    resolve_channel: Any = None
    worker_stats: Any = None
    db_get_state: Any = None
    db_set_state: Any = None
    db_remove_player: Any = None
    admin_role_id: int | None = None
    db_load_tracked_puuids: Any = None
    db_load_player_analysis_rows: Any = None
    db_load_role_group_totals: Any = None
    db_iter_record_rows: Any = None
    db_iter_wrapped_rows: Any = None
    db_load_all_ranked_states: Any = None
    # Where !commands run; the events channel when unset.
    commands_channel_id: int | None = None
