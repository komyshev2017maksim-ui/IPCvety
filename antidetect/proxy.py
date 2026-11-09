"""Разбор строки прокси в единый вид."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

SCHEMES = {"http", "https", "socks5", "socks4"}


class ProxyError(ValueError):
    pass


@dataclass(frozen=True)
class Proxy:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def server(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def endpoint(self) -> str:
        """Адрес и порт без пароля: по нему ищем повторы между профилями."""
        return f"{self.host}:{self.port}"

    def to_dict(self) -> dict:
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "password": self.password,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Proxy | None":
        if not data or not data.get("host"):
            return None
        return cls(
            scheme=data.get("scheme") or "http",
            host=data["host"],
            port=int(data["port"]),
            username=data.get("username") or "",
            password=data.get("password") or "",
        )


def parse_proxy(text: str) -> Proxy | None:
    """Понимает форматы:

    host:port
    host:port:user:pass
    user:pass@host:port
    scheme://host:port
    scheme://user:pass@host:port
    """
    text = (text or "").strip()
    if not text:
        return None

    scheme = "http"
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9]*)://(.*)$", text)
    if match:
        scheme, text = match.group(1).lower(), match.group(2)
        if scheme == "socks":
            scheme = "socks5"
        if scheme not in SCHEMES:
            raise ProxyError(f"Неизвестный тип прокси: {scheme}")
    text = text.rstrip("/")

    username = password = ""
    if "@" in text:
        parts = urlsplit(f"//{text}")
        try:
            host, port = parts.hostname, parts.port
        except ValueError as exc:
            raise ProxyError(f"Не разобрал прокси: {exc}") from exc
        username = unquote(parts.username or "")
        password = unquote(parts.password or "")
    else:
        chunks = text.split(":")
        if len(chunks) == 2:
            host, port = chunks
        elif len(chunks) >= 4:
            host, port, username = chunks[0], chunks[1], chunks[2]
            password = ":".join(chunks[3:])
        else:
            raise ProxyError(
                "Ожидается host:port, host:port:user:pass или user:pass@host:port"
            )

    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise ProxyError(f"Порт должен быть числом: {port!r}") from exc
    if not host or not 0 < port < 65536:
        raise ProxyError("Не указан адрес или порт прокси")
    return Proxy(scheme, host, port, username, password)
