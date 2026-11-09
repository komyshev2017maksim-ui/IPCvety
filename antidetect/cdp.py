"""Минимальный клиент протокола Chrome DevTools.

Домен Runtime намеренно не включается (``Runtime.enable`` не вызывается):
по его побочным эффектам сайты отличают браузер под управлением программы.
"""

from __future__ import annotations

import asyncio
import itertools
import json

import websockets


class CDPError(Exception):
    pass


class CDP:
    def __init__(self, socket):
        self._socket = socket
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: dict[str, list] = {}
        self._tasks: set[asyncio.Task] = set()
        self.closed = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, url: str) -> "CDP":
        return cls(await websockets.connect(url, max_size=None, ping_interval=None))

    def on(self, method: str, handler) -> None:
        """handler(params, session_id) — корутина; запускается отдельной задачей."""
        self._handlers.setdefault(method, []).append(handler)

    async def send(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 30):
        if self.closed.is_set():
            raise CDPError("соединение с браузером закрыто")
        message_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        message = {"id": message_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        try:
            await self._socket.send(json.dumps(message))
            return await asyncio.wait_for(future, timeout)
        except websockets.ConnectionClosed:
            raise CDPError("соединение с браузером закрыто") from None
        finally:
            self._pending.pop(message_id, None)

    async def _read(self) -> None:
        try:
            async for raw in self._socket:
                message = json.loads(raw)
                if "id" in message:
                    future = self._pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(CDPError(message["error"].get("message", "ошибка")))
                        else:
                            future.set_result(message.get("result", {}))
                    continue
                for handler in self._handlers.get(message.get("method"), ()):
                    task = asyncio.create_task(handler(message.get("params", {}), message.get("sessionId")))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        except (websockets.ConnectionClosed, OSError):
            pass
        finally:
            self.closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CDPError("соединение с браузером закрыто"))

    async def close(self) -> None:
        try:
            await self._socket.close()
        except OSError:
            pass
        self._reader.cancel()
        for task in list(self._tasks):
            task.cancel()
