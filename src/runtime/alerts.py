import asyncio
import threading
import time

from src.runtime.diagnostics import log_stack_dump

RIOT_KEY_ALERT_STATE_KEY = "riot_401_alert_sent"  # Name kept for existing deployments; also covers 403.
RIOT_KEY_STATUS_LABELS = {401: "401 Unauthorized", 403: "403 Forbidden"}
RIOT_KEY_RECOVERED_TEXT = "\u2705 Riot API key is working again; Riot requests are succeeding."
RIOT_KEY_RECOVERY_RETRY_SECONDS = 60.0


class RiotAlertState:
    def __init__(self):
        self.riot_401_alert_sent = False
        self.riot_alert_lock = asyncio.Lock()
        # Recovery bookkeeping, touched from Riot worker threads.
        self.persisted_flag_loaded = False
        self.recovery_pending = False
        self.recovery_retry_after = 0.0
        self.sync_lock = threading.Lock()


def format_riot_key_alert_text(status_code=401):
    label = RIOT_KEY_STATUS_LABELS.get(status_code, f"HTTP {status_code}")
    return (
        f"\u26A0\uFE0F Riot API returned {label}. "
        "`RIOT_API_KEY` is likely expired or invalid. "
        "Update the `RIOT_API_KEY` environment variable and restart or redeploy the bot. "
        "A confirmation will be posted here once the key works again."
    )


async def send_riot_key_expired_alert(*, resolve_channel, events_channel_id, log, status_code=401):
    channel = await resolve_channel(events_channel_id)
    if channel is None:
        return False
    await channel.send(format_riot_key_alert_text(status_code))
    log(f"[riot] Sent RIOT_API_KEY expiry alert (HTTP {status_code}).")
    return True


async def send_riot_key_recovered_message(*, resolve_channel, events_channel_id, log):
    channel = await resolve_channel(events_channel_id)
    if channel is None:
        return False
    await channel.send(RIOT_KEY_RECOVERED_TEXT)
    log("[riot] Sent RIOT_API_KEY recovery message.")
    return True


def riot_401_alert_already_sent(*, state: RiotAlertState, db_get_state):
    if state.riot_401_alert_sent:
        return True
    persisted = db_get_state(RIOT_KEY_ALERT_STATE_KEY)
    if persisted == "1":
        state.riot_401_alert_sent = True
        return True
    return False


def mark_riot_401_alert_sent(*, state: RiotAlertState, db_set_state):
    state.riot_401_alert_sent = True
    db_set_state(RIOT_KEY_ALERT_STATE_KEY, "1")


def clear_riot_401_alert(*, state: RiotAlertState, db_set_state):
    state.riot_401_alert_sent = False
    db_set_state(RIOT_KEY_ALERT_STATE_KEY, "0")


def trigger_riot_key_alert(
    *,
    state: RiotAlertState,
    client,
    resolve_channel,
    events_channel_id,
    db_get_state,
    db_set_state,
    log,
    status_code=401,
):
    async def _inner():
        async with state.riot_alert_lock:
            if riot_401_alert_already_sent(state=state, db_get_state=db_get_state):
                return
            try:
                sent = await send_riot_key_expired_alert(
                    resolve_channel=resolve_channel,
                    events_channel_id=events_channel_id,
                    log=log,
                    status_code=status_code,
                )
            except Exception as exc:
                log(f"[riot] Failed to send RIOT_API_KEY expiry alert: {exc}")
                return
            if sent:
                mark_riot_401_alert_sent(state=state, db_set_state=db_set_state)

    try:
        loop = client.loop
        asyncio.run_coroutine_threadsafe(_inner(), loop)
    except Exception as exc:
        log(f"[riot] Could not schedule key-expiry alert: {exc}")


def _recovery_needed(*, state: RiotAlertState, db_get_state, log):
    """Return True when an alert is outstanding and no recovery is in flight yet (claims it)."""
    if state.persisted_flag_loaded and not state.riot_401_alert_sent:
        return False  # Fast path for every successful Riot request.
    with state.sync_lock:
        if not state.persisted_flag_loaded:
            try:
                if db_get_state(RIOT_KEY_ALERT_STATE_KEY) == "1":
                    state.riot_401_alert_sent = True
            except Exception as exc:
                log(f"[riot] Could not read {RIOT_KEY_ALERT_STATE_KEY}: {exc}")
                return False
            state.persisted_flag_loaded = True
        if not state.riot_401_alert_sent or state.recovery_pending:
            return False
        if time.monotonic() < state.recovery_retry_after:
            return False
        state.recovery_pending = True
        return True


def trigger_riot_key_recovered(
    *,
    state: RiotAlertState,
    client,
    resolve_channel,
    events_channel_id,
    db_set_state,
    db_get_state,
    log,
):
    """Called after every successful Riot response; posts a recovery notice once per alert."""
    if not _recovery_needed(state=state, db_get_state=db_get_state, log=log):
        return

    async def _inner():
        sent = False
        try:
            async with state.riot_alert_lock:
                if not state.riot_401_alert_sent:
                    return
                try:
                    sent = await send_riot_key_recovered_message(
                        resolve_channel=resolve_channel,
                        events_channel_id=events_channel_id,
                        log=log,
                    )
                except Exception as exc:
                    log(f"[riot] Failed to send RIOT_API_KEY recovery message: {exc}")
                    return
                if sent:
                    try:
                        await asyncio.to_thread(clear_riot_401_alert, state=state, db_set_state=db_set_state)
                    except Exception as exc:
                        log(f"[riot] Failed to clear {RIOT_KEY_ALERT_STATE_KEY}: {exc}")
        finally:
            if not sent:
                state.recovery_retry_after = time.monotonic() + RIOT_KEY_RECOVERY_RETRY_SECONDS
            state.recovery_pending = False

    try:
        loop = client.loop
        asyncio.run_coroutine_threadsafe(_inner(), loop)
    except Exception as exc:
        state.recovery_pending = False
        log(f"[riot] Could not schedule key-recovery message: {exc}")


def worker_stall_state_key(worker_name):
    return f"worker_stall_alert_sent::{worker_name}"


async def check_and_notify_worker_stalls(
    *,
    state,
    resolve_channel,
    events_channel_id,
    worker_stats,
    stale_thresholds_seconds,
    db_get_state=None,
    db_set_state=None,
    now_monotonic,
    log,
):
    channel = await resolve_channel(events_channel_id)
    if channel is None:
        return

    alerted_by_worker = state.setdefault("alerted_by_worker", {})

    for worker_name, threshold_seconds in stale_thresholds_seconds.items():
        entry = worker_stats.get(worker_name) or {}
        # Workers that never finished a cycle still count: their clock starts at process
        # start (last_success_at), so a worker stuck in its first cycle also alerts.
        last_success_at = float(entry.get("last_success_at", 0.0) or 0.0)
        if last_success_at <= 0:
            continue

        stale_for = now_monotonic - last_success_at
        is_stale = stale_for >= float(threshold_seconds)
        alerted = alerted_by_worker.get(worker_name)
        if alerted is None and db_get_state is not None:
            persisted = await asyncio.to_thread(db_get_state, worker_stall_state_key(worker_name))
            alerted = persisted == "1"
            alerted_by_worker[worker_name] = alerted
        if alerted is None:
            alerted = False
            alerted_by_worker[worker_name] = False

        if is_stale and not alerted:
            await channel.send(
                f"\u26A0\uFE0F Worker `{worker_name}` appears stalled. "
                f"No successful cycle for `{int(stale_for)}s` (threshold `{int(threshold_seconds)}s`)."
            )
            alerted_by_worker[worker_name] = True
            if db_set_state is not None:
                await asyncio.to_thread(db_set_state, worker_stall_state_key(worker_name), "1")
            log(
                f"[health] Worker stall alert sent for {worker_name}: "
                f"stale_for={int(stale_for)}s threshold={int(threshold_seconds)}s"
            )
            # Show where everything is waiting, so a hang can be diagnosed from the logs.
            log_stack_dump(log, reason=f"{worker_name} stalled")
            continue

        if (not is_stale) and alerted:
            await channel.send(
                f"\u2705 Worker `{worker_name}` recovered. "
                f"Latest successful cycle was `{int(now_monotonic - last_success_at)}s` ago."
            )
            alerted_by_worker[worker_name] = False
            if db_set_state is not None:
                await asyncio.to_thread(db_set_state, worker_stall_state_key(worker_name), "0")
            log(f"[health] Worker stall cleared for {worker_name}.")
