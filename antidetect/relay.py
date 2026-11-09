"""Локальный переходник к прокси профиля.

Chrome не умеет SOCKS с логином и паролем, а для HTTP-прокси с паролем
показывает окно входа. Поэтому браузеру отдаётся адрес ``127.0.0.1:порт``
без авторизации, а переходник сам подставляет логин и пароль и пересылает
трафик на настоящий прокси (HTTP или SOCKS5). Имена сайтов резолвит прокси,
DNS-запросы с вашего компьютера не уходят.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import struct

from .proxy import Proxy

HEAD_LIMIT = 64 * 1024
CONNECT_TIMEOUT = 30


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > HEAD_LIMIT:
        raise ValueError("слишком длинные заголовки")
    return head


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _socks5_connect(proxy: Proxy, host: str, port: int):
    reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
    writer.write(b"\x05\x02\x00\x02" if proxy.username else b"\x05\x01\x00")
    version, method = await reader.readexactly(2)
    if method == 2:
        user, password = proxy.username.encode(), proxy.password.encode()
        writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
        if (await reader.readexactly(2))[1] != 0:
            raise ConnectionError("SOCKS5: неверный логин или пароль")
    elif method != 0:
        raise ConnectionError("SOCKS5: прокси не принял способ авторизации")
    try:
        address = ipaddress.ip_address(host)
        encoded = (b"\x01" if address.version == 4 else b"\x04") + address.packed
    except ValueError:
        name = host.encode("idna")
        encoded = b"\x03" + bytes([len(name)]) + name
    writer.write(b"\x05\x01\x00" + encoded + struct.pack(">H", port))
    reply = await reader.readexactly(4)
    if reply[1] != 0:
        raise ConnectionError(f"SOCKS5: прокси отказал в соединении (код {reply[1]})")
    skip = {1: 4, 4: 16}.get(reply[3])
    if skip is None:
        skip = (await reader.readexactly(1))[0]
    await reader.readexactly(skip + 2)
    return reader, writer


def _split_target(target: str, default_port: int) -> tuple[str, int]:
    host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        return target.strip("[]"), default_port
    return host.strip("[]"), int(port)


class Relay:
    def __init__(self, proxy: Proxy):
        self.proxy = proxy
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> "Relay":
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _accept(self, reader, writer) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            await self._serve(reader, writer)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError, ValueError, asyncio.TimeoutError):
            pass
        finally:
            self._tasks.discard(task)
            try:
                writer.close()
            except OSError:
                pass

    async def _serve(self, reader, writer) -> None:
        head = await _read_head(reader)
        request_line, _, rest = head.partition(b"\r\n")
        method, target, version = request_line.decode("latin-1").split(" ", 2)
        try:
            if self.proxy.scheme.startswith("socks"):
                upstream = await asyncio.wait_for(self._via_socks(method, target, version, rest, writer), CONNECT_TIMEOUT)
            else:
                upstream = await asyncio.wait_for(self._via_http(request_line, rest), CONNECT_TIMEOUT)
        except (ConnectionError, OSError, asyncio.TimeoutError) as error:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            raise ConnectionError(str(error)) from None
        up_reader, up_writer = upstream
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))

    async def _via_http(self, request_line: bytes, rest: bytes):
        up_reader, up_writer = await asyncio.open_connection(self.proxy.host, self.proxy.port)
        lines = [line for line in rest.split(b"\r\n") if line]
        is_connect = request_line.upper().startswith(b"CONNECT ")
        drop = (b"proxy-authorization:", b"proxy-connection:") + (() if is_connect else (b"connection:",))
        lines = [line for line in lines if not line.lower().startswith(drop)]
        if self.proxy.username:
            token = base64.b64encode(f"{self.proxy.username}:{self.proxy.password}".encode()).decode()
            lines.append(f"Proxy-Authorization: Basic {token}".encode())
        if not is_connect:
            # Одно соединение на запрос: следующий запрос снова пройдёт через
            # переходник и получит заголовок авторизации.
            lines += [b"Proxy-Connection: close", b"Connection: close"]
        up_writer.write(request_line + b"\r\n" + b"\r\n".join(lines) + b"\r\n\r\n")
        await up_writer.drain()
        return up_reader, up_writer

    async def _via_socks(self, method: str, target: str, version: str, rest: bytes, writer):
        if method.upper() == "CONNECT":
            host, port = _split_target(target, 443)
            upstream = await _socks5_connect(self.proxy, host, port)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            return upstream
        # Обычный HTTP: «GET http://host/path» превращаем в запрос к самому сайту.
        scheme, _, tail = target.partition("://")
        authority, slash, path = tail.partition("/")
        host, port = _split_target(authority, 80)
        up_reader, up_writer = await _socks5_connect(self.proxy, host, port)
        lines = [line for line in rest.split(b"\r\n") if line and not line.lower().startswith((b"proxy-", b"connection:"))]
        lines.append(b"Connection: close")
        up_writer.write(f"{method} /{path} {version}".encode("latin-1") + b"\r\n" + b"\r\n".join(lines) + b"\r\n\r\n")
        await up_writer.drain()
        return up_reader, up_writer
