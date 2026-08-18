"""APScheduler lifecycle and single-instance job boundary."""

from collections.abc import Callable

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler

from ops.config import Settings


class SchedulerService:
    """Manage one coalescing scheduler with an injectable synchronization job."""

    def __init__(self, settings: Settings, sync_job: Callable[[], None] | None = None) -> None:
        self.settings = settings
        self.sync_job = sync_job
        self.scheduler = BackgroundScheduler(timezone="UTC")

    def start(self) -> None:
        if self.scheduler.running or not self.settings.scheduler_enabled:
            return
        self._schedule_job()
        self.scheduler.start()

    def reconfigure(self, settings: Settings) -> None:
        """Apply GUI scheduler changes without requiring a shell restart."""

        self.settings = settings
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
                self.sync_job,
                trigger="interval",
                minutes=self.settings.sync_interval_minutes,
                id="synchronization-tick",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=300,
            )

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
