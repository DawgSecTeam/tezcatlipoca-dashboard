"""APScheduler wrapper that fires periodic IOC check cycles."""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta


class CheckScheduler:
    """Manages periodic execution of IOC checks.
    
    Runs checks every 5 minutes when competition is active.
    """

    def __init__(self, check_interval_minutes: int = 5) -> None:
        self.logger = logging.getLogger(__name__)
        self.check_interval = check_interval_minutes
        self.running = False
        self.paused = False
        self.task: asyncio.Task | None = None
        self.check_callback: Callable | None = None
        self.last_check_time: datetime | None = None
        self.next_check_time: datetime | None = None
        self.pause_time: datetime | None = None
        self.time_until_next: float | None = None

    def set_callback(self, callback: Callable) -> None:
        """Set the callback function to run checks."""
        self.check_callback = callback

    async def start(self) -> None:
        """Start the scheduler."""
        if self.running:
            self.logger.warning("Scheduler already running")
            return

        if not self.check_callback:
            raise RuntimeError("No check callback set")

        self.running = True
        self.task = asyncio.create_task(self._run_scheduler())
        self.logger.info(
            f"Check scheduler started with {self.check_interval} minute interval"
        )

    async def stop(self) -> None:
        """Stop the scheduler."""
        self.running = False
        self.paused = False
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.logger.info("Check scheduler stopped")

    async def pause(self) -> None:
        """Pause the scheduler - stops scheduling new checks but doesn't cancel the scheduler task."""
        if not self.running or self.paused:
            self.logger.warning("Scheduler not running or already paused")
            return

        self.paused = True
        self.pause_time = datetime.now(UTC)

        # Remaining seconds until the scheduled next check (clamped ≥ 0).
        # If next_check_time is in the past (check currently in flight), clamp
        # to 0 so resume() fires the next check immediately after unpause.
        if self.next_check_time:
            self.time_until_next = max(
                0.0,
                (self.next_check_time - self.pause_time).total_seconds(),
            )
        else:
            self.time_until_next = float(self.check_interval * 60)

        self.logger.info(
            f"Check scheduler paused with {self.time_until_next:.0f} seconds until next check"
        )

    async def resume(self) -> None:
        """Resume the scheduler from pause."""
        if not self.running or not self.paused:
            self.logger.warning("Scheduler not running or not paused")
            return

        self.paused = False
        resume_time = datetime.now(UTC)

        # Fire a check immediately after unpause so phase-6 fault recovery
        # succeeds within the soak budget regardless of where we were in the
        # current interval (waiting remaining time could exceed budget).
        self.next_check_time = resume_time
        self.logger.info("Check scheduler resumed, firing check immediately")

        self.pause_time = None
        self.time_until_next = None

    async def _run_scheduler(self) -> None:
        """Main scheduler loop."""
        # Run initial check immediately
        await self._run_check()

        while self.running:
            try:
                from fastapi_backend.core.config import config  # cycle-break
                self.check_interval = config.CHECK_INTERVAL_MINUTES
                self.next_check_time = datetime.now(UTC) + timedelta(
                    minutes=self.check_interval
                )

                # Wait until next_check_time, pausing when instructed.
                # resume() updates self.next_check_time, so the inner loop
                # naturally picks up a shorter deadline after unpause.
                while self.running:
                    if self.paused:
                        await asyncio.sleep(1)
                        continue
                    remaining = (self.next_check_time - datetime.now(UTC)).total_seconds()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(remaining, 5))

                if self.running and not self.paused:
                    await self._run_check()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in scheduler loop: {e}")
                await asyncio.sleep(30)

    async def _run_check(self) -> None:
        """Execute a check cycle."""
        self.last_check_time = datetime.now(UTC)
        self.logger.info(f"Starting scheduled check at {self.last_check_time}")

        try:
            # Run check callback
            if asyncio.iscoroutinefunction(self.check_callback):
                await self.check_callback()
            else:
                # Run synchronous callback in thread pool
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.check_callback)

            self.logger.info("Scheduled check completed successfully")

        except Exception as e:
            self.logger.error(f"Error during scheduled check: {e}")

    def get_status(self) -> dict:
        """Get scheduler status."""
        return {
            "running": self.running,
            "paused": self.paused,
            "check_interval_minutes": self.check_interval,
            "last_check_time": self.last_check_time.isoformat()
            if self.last_check_time
            else None,
            "next_check_time": self.next_check_time.isoformat()
            if self.next_check_time
            else None,
            "pause_time": self.pause_time.isoformat() if self.pause_time else None,
        }
