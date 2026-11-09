"""Чтение файлов cookies: JSON (как выгружают антидетекты и расширения) и Netscape."""

from __future__ import annotations

import json

ALLOWED = {"name", "value", "url", "domain", "path", "secure", "httpOnly", "sameSite", "expires"}
SAME_SITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}


class CookieError(ValueError):
    pass


def parse(text: str) -> list[dict]:
    text = text.lstrip("﻿").strip()
    if not text:
        raise CookieError("Файл cookies пустой")
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise CookieError(f"Не разобрал JSON: {exc}") from exc
        if isinstance(data, dict):
            data = data.get("cookies", [])
        return normalize(data)
    return normalize(_netscape(text))


def _netscape(text: str) -> list[dict]:
    cookies = []
    for line in text.splitlines():
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path, secure, expires, name, value = parts[:7]
        cookies.append({"domain": domain, "path": path, "secure": secure.upper() == "TRUE", "httpOnly": http_only,
                        "expires": float(expires or 0), "name": name, "value": value})
    return cookies


def normalize(cookies) -> list[dict]:
    """Приводит cookies к виду, который принимает Chrome (Storage.setCookies)."""
    if not isinstance(cookies, list):
        raise CookieError("Ожидается список cookies")
    result = []
    for raw in cookies:
        if not isinstance(raw, dict) or not raw.get("name") or not (raw.get("domain") or raw.get("url")):
            continue
        cookie = {key: raw[key] for key in ALLOWED if raw.get(key) is not None}
        cookie["value"] = str(raw.get("value", ""))
        expires = raw.get("expires", raw.get("expirationDate"))
        if isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = float(expires)
        else:
            cookie.pop("expires", None)  # сессионная
        same_site = SAME_SITE.get(str(raw.get("sameSite", "")).lower())
        if same_site:
            cookie["sameSite"] = same_site
        else:
            cookie.pop("sameSite", None)
        if cookie.get("sameSite") == "None":
            cookie["secure"] = True
        result.append(cookie)
    if not result:
        raise CookieError("В файле не нашлось ни одной cookie")
    return result
