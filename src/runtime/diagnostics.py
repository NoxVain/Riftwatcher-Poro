"""Stack dumps for diagnosing hangs from the logs (no shell access to production needed).

On a stall alert we log where every thread and asyncio task currently is. On
2026-10-26 a stalled DB read froze the scoreboards for hours and finding it took
reading Postgres logs; a dump points straight at the waiting line.
"""

import asyncio
import sys
import threading
import traceback

MAX_FRAMES = 12


def _thread_sections():
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    sections = []
    for thread_id, frame in sys._current_frames().items():
        stack = traceback.format_stack(frame)[-MAX_FRAMES:]
        sections.append((f"thread {names.get(thread_id, thread_id)}", stack))
    return sections


def _task_sections():
    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:  # no running event loop (called from a plain thread)
        return []
    sections = []
    for task in tasks:
        frames = task.get_stack(limit=MAX_FRAMES)
        if not frames:
            continue
        # A suspended coroutine's frames aren't linked via f_back, so format them as given.
        summary = traceback.StackSummary.extract((frame, frame.f_lineno) for frame in frames)
        sections.append((f"task {task.get_name()}", summary.format()))
    return sections


def format_stack_dump():
    lines = []
    for title, stack in _thread_sections() + _task_sections():
        lines.append(f"--- {title}")
        for entry in stack:
            lines.extend(line for line in entry.rstrip().splitlines())
    return lines


def log_stack_dump(log, *, reason):
    """Log all thread and task stacks; never raises (diagnostics must not add failures)."""
    try:
        lines = format_stack_dump()
    except Exception as exc:
        log(f"[diag] Stack dump failed ({reason}): {exc}")
        return
    log(f"[diag] Stack dump ({reason}): {len(lines)} lines")
    for line in lines:
        log(f"[diag] {line}")
