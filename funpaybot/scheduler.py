from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, time

import pytz

from .config import AppConfig
from .funpay_client import BumpResult, FunPayClient


log = logging.getLogger(__name__)


class AutoBumpScheduler:
    def __init__(self, config: AppConfig, client: FunPayClient) -> None:
        self.config = config
        self.client = client
        self.enabled = config.auto_bump.enabled
        self.last_results: list[BumpResult] = []
        self.last_run_at: datetime | None = None
        self.next_run_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="funpay-auto-bump")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            await self._task

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def status_text(self) -> str:
        dry_mode = "DRY-RUN" if self.config.funpay.dry_run else "REAL"
        lines = [
            f"Режим: {dry_mode}",
            f"Авто-поднятие: {'включено' if self.enabled else 'выключено'}",
            f"Реальные действия: {'да' if self.client.can_write else 'нет'}",
            f"Категории: {', '.join(self.config.funpay.category_ids) or 'не заданы'}",
            f"Интервал: {self.config.auto_bump.interval_seconds} сек.",
            f"Последний запуск: {self.last_run_at.isoformat(sep=' ', timespec='seconds') if self.last_run_at else 'еще не было'}",
            f"Следующий запуск: {self.next_run_at.isoformat(sep=' ', timespec='seconds') if self.next_run_at else 'не запланирован'}",
        ]

        if self.last_results:
            lines.append("")
            lines.append("Последние результаты:")
            for result in self.last_results:
                marker = "OK" if result.ok else "ERR"
                lines.append(f"{marker} {result.category_id}: {result.message}")

        return "\n".join(lines)

    async def bump_now(self) -> list[BumpResult]:
        self.last_run_at = datetime.now()
        self.last_results = await self.client.bump_all(self.config.funpay.category_ids)
        self.next_run_at = self._calculate_next_run()
        return self.last_results

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            if not self.enabled:
                self.next_run_at = None
                await self._sleep(10)
                continue

            if self._is_quiet_hours():
                self.next_run_at = None
                await self._sleep(60)
                continue

            try:
                await self.bump_now()
            except Exception:
                log.exception("Auto bump failed")

            delay = self._delay_seconds()
            self.next_run_at = datetime.now() + _seconds_delta(delay)
            await self._sleep(delay)

    async def _sleep(self, seconds: int) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(1, seconds))
        except TimeoutError:
            pass

    def _delay_seconds(self) -> int:
        base = max(600, self.config.auto_bump.interval_seconds)
        jitter = max(0, self.config.auto_bump.jitter_seconds)
        return base + random.randint(0, jitter)

    def _calculate_next_run(self) -> datetime:
        return datetime.now() + _seconds_delta(self._delay_seconds())

    def _is_quiet_hours(self) -> bool:
        quiet = self.config.auto_bump.quiet_hours
        if not quiet.enabled:
            return False

        tz = pytz.timezone(quiet.timezone)
        now = datetime.now(tz).time()
        start = _parse_time(quiet.start)
        end = _parse_time(quiet.end)

        if start <= end:
            return start <= now <= end
        return now >= start or now <= end


def _parse_time(value: str) -> time:
    hours, minutes = value.split(":", 1)
    return time(hour=int(hours), minute=int(minutes))


def _seconds_delta(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)
