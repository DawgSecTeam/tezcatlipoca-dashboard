"""In-process run event bus: threads publish, SSE/MCP subscribers receive.

Every publish also lands in the store first (see run_manager), so this bus is
best-effort live delivery; the event cursor is the source of truth for recovery."""

import asyncio
import threading


class EventBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs = {}  # run_id -> [(loop, queue)]

    def subscribe(self, run_id):
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        with self._lock:
            self._subs.setdefault(run_id, []).append((loop, queue))
        return queue

    def unsubscribe(self, run_id, queue):
        with self._lock:
            subs = self._subs.get(run_id) or []
            self._subs[run_id] = [(l, q) for (l, q) in subs if q is not queue]

    def publish(self, run_id, event):
        with self._lock:
            subs = list(self._subs.get(run_id) or [])
        for loop, queue in subs:
            loop.call_soon_threadsafe(self._put, queue, event)

    @staticmethod
    def _put(queue, event):
        queue.put_nowait(event)
