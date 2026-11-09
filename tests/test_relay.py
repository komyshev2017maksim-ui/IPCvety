"""Переходник к прокси: SOCKS5 с логином и паролем, которого Chrome сам не умеет."""

import asyncio
import struct

from antidetect.proxy import Proxy
from antidetect.relay import Relay


async def _fake_site(reader, writer):
    request = await reader.readuntil(b"\r\n\r\n")
    body = b"seen:" + request.split(b"\r\n")[0]
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
    await writer.drain()
    writer.close()


def _fake_socks5(site_port: int, log: list):
    async def handle(reader, writer):
        _, count = await reader.readexactly(2)
        methods = await reader.readexactly(count)
        assert 2 in methods
        writer.write(b"\x05\x02")
        await reader.readexactly(1)
        user = await reader.readexactly((await reader.readexactly(1))[0])
        password = await reader.readexactly((await reader.readexactly(1))[0])
        ok = (user, password) == (b"user", b"secret")
        writer.write(b"\x01\x00" if ok else b"\x01\x01")
        if not ok:
            writer.close()
            return
        _, _, _, kind = await reader.readexactly(4)
        assert kind == 3, "имя сайта должно уходить на прокси, а не резолвиться локально"
        host = await reader.readexactly((await reader.readexactly(1))[0])
        (port,) = struct.unpack(">H", await reader.readexactly(2))
        log.append((host.decode(), port))
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", site_port)
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")

        async def pipe(src, dst):
            while data := await src.read(4096):
                dst.write(data)
                await dst.drain()
            dst.close()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    return handle


async def _request(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(-1), 5)
    writer.close()
    return data


async def _scenario(password: str):
    log = []
    site = await asyncio.start_server(_fake_site, "127.0.0.1", 0)
    site_port = site.sockets[0].getsockname()[1]
    socks = await asyncio.start_server(_fake_socks5(site_port, log), "127.0.0.1", 0)
    relay = await Relay(Proxy("socks5", "127.0.0.1", socks.sockets[0].getsockname()[1], "user", password)).start()
    try:
        plain = await _request(relay.port, b"GET http://shop.example/page?x=1 HTTP/1.1\r\nHost: shop.example\r\nProxy-Connection: keep-alive\r\n\r\n")
        tunnel = await _request(relay.port, b"CONNECT secure.example:443 HTTP/1.1\r\nHost: secure.example:443\r\n\r\nGET /inside HTTP/1.1\r\n\r\n")
        return plain, tunnel, log
    finally:
        await relay.stop()
        socks.close()
        site.close()


def test_socks5_with_password():
    plain, tunnel, log = asyncio.run(_scenario("secret"))
    assert plain.endswith(b"seen:GET /page?x=1 HTTP/1.1"), "запрос к сайту без адреса прокси в строке"
    assert tunnel.startswith(b"HTTP/1.1 200 Connection established") and tunnel.endswith(b"seen:GET /inside HTTP/1.1")
    assert log == [("shop.example", 80), ("secure.example", 443)]


def test_socks5_wrong_password_gives_bad_gateway():
    plain, tunnel, log = asyncio.run(_scenario("wrong"))
    assert plain.startswith(b"HTTP/1.1 502") and tunnel.startswith(b"HTTP/1.1 502") and log == []
