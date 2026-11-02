"""Shared FIFO admission for every image generation entrypoint."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from .api_types import APIError

_PROGRESS = ContextVar("generation_progress", default=None)
_PREVIEW = ContextVar("generation_preview", default=None)
_COUNT = ContextVar("generation_count", default=None)
_RESERVATION = ContextVar("generation_reservation", default=None)
_RESERVATION_PERSISTENT = ContextVar("generation_reservation_persistent", default=False)


@contextmanager
def generation_progress(callback, *, preview=None, count=None):
    if callback is None and preview is None and count is None:
        yield
        return
    token = _PROGRESS.set(callback) if callback is not None else None
    preview_token = _PREVIEW.set(preview) if preview is not None else None
    count_token = _COUNT.set(count) if count is not None else None
    try:
        yield
    finally:
        if token is not None:
            _PROGRESS.reset(token)
        if preview_token is not None:
            _PREVIEW.reset(preview_token)
        if count_token is not None:
            _COUNT.reset(count_token)


async def emit_generation_preview(image_base64, output_format="png"):
    callback = _PREVIEW.get()
    if callback is not None:
        await callback(image_base64, output_format)


async def emit_generation_count(count, stats):
    callback = _COUNT.get()
    if callback is not None:
        await callback(count, stats)


@contextmanager
def generation_reservation(ticket, *, persistent: bool = False):
    token = _RESERVATION.set(ticket)
    persistent_token = _RESERVATION_PERSISTENT.set(persistent)
    try:
        yield
    finally:
        _RESERVATION.reset(token)
        _RESERVATION_PERSISTENT.reset(persistent_token)


class GenerationTicket:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.future = asyncio.get_running_loop().create_future()
        self.granted = False
        self.released = False
        self.claimed = False

    def release(self):
        if self.released:
            return
        self.released = True
        if self.granted:
            self.scheduler.active -= 1
        else:
            self.scheduler.waiters.remove(self)
        if not self.future.done():
            self.future.cancel()
        self.scheduler._advance()


class GenerationScheduler:
    def __init__(self, concurrency=3, queue_size=100):
        self.concurrency = concurrency
        self.queue_size = queue_size
        self.active = 0
        self.waiters = deque()
        self.closed = False
        self.running = set()

    @property
    def accepting(self):
        return not self.closed and (
            self.active < self.concurrency or len(self.waiters) < self.queue_size
        )

    @property
    def busy(self):
        return bool(self.active or self.waiters)

    def reserve(self):
        if not self.accepting:
            raise APIError(
                "生成服务正在关闭" if self.closed else "生成队列已满，请稍后重试",
                None,
                "service_closed" if self.closed else "queue_full",
                retryable=False,
            )
        ticket = GenerationTicket(self)
        self.waiters.append(ticket)
        self._advance()
        return ticket

    def _advance(self):
        while not self.closed and self.waiters and self.active < self.concurrency:
            ticket = self.waiters.popleft()
            ticket.granted = True
            self.active += 1
            ticket.future.set_result(None)

    async def close(self):
        self.closed = True
        for ticket in list(self.waiters):
            ticket.release()
        tasks = [task for task in self.running if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def scheduled_generation(function):
    """旧调度路径：`scheduler_v2` 开启时由 v2 调度器替代，稳定后删除，见 docs/architecture/migration.md。"""

    @wraps(function)
    async def call(owner, *args, **kwargs):
        scheduler = getattr(owner, "generation_scheduler", None)
        if scheduler is None:
            return await function(owner, *args, **kwargs)
        ticket = _RESERVATION.get()
        persistent = _RESERVATION_PERSISTENT.get()
        if ticket is None or ticket.released or (ticket.claimed and not persistent):
            ticket = scheduler.reserve()
        ticket.claimed = True
        task = asyncio.current_task()
        scheduler.running.add(task)
        progress = _PROGRESS.get()
        try:
            if progress and not ticket.granted:
                await progress("queued")
            await asyncio.shield(ticket.future)
            if scheduler.closed:
                raise asyncio.CancelledError
            if progress:
                await progress("running")
            # Provider retry clocks start inside the wrapped call, after admission.
            return await function(owner, *args, **kwargs)
        finally:
            scheduler.running.discard(task)
            if not persistent:
                ticket.release()

    return call
