"""APScheduler lifecycle and single-instance job boundary."""

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler

from ops.config import Settings
from ops.providers.errors import diagnostic


class SchedulerService:
    """Manage one coalescing scheduler with an injectable synchronization job."""

    def __init__(self, settings: Settings, sync_job: Callable[[], None] | None = None) -> None:
        self.settings = settings
        self.sync_job = sync_job
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self.started_at = datetime.now(UTC)
        self.last_started_at = None
        self.last_completed_at = None
        self.last_error = None
        self.cycles = 0
        self._last_progress = time.monotonic()
        self._busy_since = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.scheduler.running or not self.settings.scheduler_enabled:
            return
        self._schedule_job()
        self.scheduler.add_job(
            self._watchdog,
            "interval",
            seconds=30,
            id="scheduler-watchdog",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self._last_progress = time.monotonic()
        self.scheduler.start()

    def reconfigure(self, settings: Settings) -> None:
        """Apply GUI scheduler changes without requiring a shell restart."""

        self.settings = settings
        self._last_progress = time.monotonic()
        if self.scheduler.running:
            try:
                self.scheduler.remove_job("synchronization-tick")
            except JobLookupError:
                pass
            if self.settings.scheduler_enabled:
                self._schedule_job()
        elif self.settings.scheduler_enabled:
            self.start()

    def _schedule_job(self) -> None:
        if self.sync_job is not None:
            self.scheduler.add_job(
                self._tick,
                trigger="interval",
                minutes=self.settings.sync_interval_minutes,
                id="synchronization-tick",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )

    def _tick(self) -> None:
        """Contain unexpected job errors; APScheduler always retains the next tick."""
        if not self.settings.scheduler_enabled or not self._lock.acquire(blocking=False):
            return
        self._busy_since = time.monotonic()
        self.last_started_at = datetime.now(UTC)
        try:
            ok = self.sync_job() if self.sync_job else True
            self.last_error = (
                "Scheduler could not enumerate pairs; retry scheduled" if ok is False else None
            )
        except Exception as exc:
            self.last_error = diagnostic(exc)["error"]
            logging.getLogger(__name__).error(
                "Scheduler cycle failed; retry scheduled: %s", diagnostic(exc)
            )
        finally:
            self.last_completed_at = datetime.now(UTC)
            self.cycles += 1
            self._last_progress = time.monotonic()
            self._busy_since = None
            self._lock.release()

    def _watchdog(self) -> None:
        """Repair a missing/paused timer, never duplicate an active worker."""
        if not self.settings.scheduler_enabled or self._busy_since is not None:
            return
        job = self.scheduler.get_job("synchronization-tick")
        if job is None:
            logging.getLogger(__name__).error("Scheduler timer missing; restoring interval job")
            self._schedule_job()
        elif job.next_run_time is None:
            logging.getLogger(__name__).error("Scheduler timer paused unexpectedly; resuming")
            self.scheduler.resume_job(job.id)

    def request_check(self) -> bool:
        """Bring forward the existing job, not queue a second job or bypass consent."""
        if not self.settings.scheduler_enabled or self._busy_since is not None:
            return False
        job = self.scheduler.get_job("synchronization-tick")
        now = datetime.now(UTC)
        if job is None or (job.next_run_time and job.next_run_time <= now):
            return False
        self.scheduler.modify_job(job.id, next_run_time=now)
        return True

    def snapshot(self) -> dict:
        job = self.scheduler.get_job("synchronization-tick")
        enabled = self.settings.scheduler_enabled
        interval = self.settings.sync_interval_minutes * 60
        now = time.monotonic()
        stalled = (
            now - self._busy_since > max(2700, interval * 2)
            if self._busy_since is not None
            else now - self._last_progress > interval * 2 + 120
        )
        next_tick = getattr(job, "next_run_time", None)
        healthy = not enabled or (
            self.scheduler.running and job is not None and next_tick is not None and not stalled
        )
        return {
            "enabled": enabled,
            "healthy": healthy,
            "state": "disabled"
            if not enabled
            else "stalled"
            if not healthy
            else "running"
            if self._busy_since is not None
            else "waiting",
            "interval_minutes": self.settings.sync_interval_minutes,
            "next_tick": next_tick,
            "last_started_at": self.last_started_at,
            "last_completed_at": self.last_completed_at,
            "cycles": self.cycles,
            "last_error": self.last_error,
        }

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
