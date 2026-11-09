"""Проверка прокси: какой IP видит сайт, где он находится и какой там часовой пояс."""

from __future__ import annotations

from . import store
from .proxy import Proxy


class CheckError(Exception):
    pass


def _parse(data: dict) -> dict | None:
    """Приводит ответы ipinfo.io и ipapi.co к одному виду."""
    ip = data.get("ip") or data.get("query")
    if not ip:
        return None
    lat = data.get("latitude", data.get("lat"))
    lon = data.get("longitude", data.get("lon"))
    if lat is None and isinstance(data.get("loc"), str) and "," in data["loc"]:
        lat, lon = data["loc"].split(",", 1)
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        lat = lon = None
    country = data.get("country_code") or data.get("countryCode") or data.get("country") or ""
    return {
        "ip": ip,
        "country": country.upper() if len(country) == 2 else country,
        "region": data.get("region_code") or data.get("region") or data.get("regionName") or "",
        "city": data.get("city") or "",
        "timezone": data.get("timezone") or "",
        "lat": lat,
        "lon": lon,
        "org": data.get("org") or data.get("isp") or "",
        "checked_at": store.now_iso(),
    }


def check_ip(playwright, proxy: Proxy | None, settings: dict) -> dict:
    """Запрашивает сервисы определения IP через прокси. Первый ответивший побеждает."""
    options = {"timeout": settings["ip_check_timeout"] * 1000}
    if proxy:
        options["proxy"] = proxy.for_playwright()
    errors = []
    request = playwright.request.new_context(**options)
    try:
        for url in settings["ip_services"]:
            try:
                response = request.get(url)
                if not response.ok:
                    errors.append(f"{url}: HTTP {response.status}")
                    continue
                geo = _parse(response.json())
                if geo:
                    return geo
                errors.append(f"{url}: в ответе нет IP")
            except Exception as exc:  # сеть, таймаут, не JSON
                errors.append(f"{url}: {str(exc).splitlines()[0]}")
    finally:
        request.dispose()
    raise CheckError("Прокси не отвечает или сервисы IP недоступны:\n  " + "\n  ".join(errors))


def problems(profile: dict, geo: dict, settings: dict, others: list[dict]) -> tuple[list[str], list[str]]:
    """Возвращает (ошибки, предупреждения) по результату проверки."""
    errors, warnings = [], []
    expected = settings.get("expected_country")
    if expected and geo["country"] and geo["country"] != expected:
        errors.append(f"IP {geo['ip']} находится в {geo['country']}, а нужна страна {expected}")
    if not geo["timezone"]:
        warnings.append("Сервис не вернул часовой пояс, возьму пояс из настроек профиля")
    manual = profile.get("timezone")
    if manual and geo["timezone"] and manual != geo["timezone"]:
        warnings.append(
            f"В профиле указан пояс {manual}, а IP находится в {geo['timezone']}. "
            "Браузер получит пояс по IP"
        )
    previous = (profile.get("geo") or {}).get("ip")
    if previous and previous != geo["ip"]:
        warnings.append(f"IP выхода сменился: был {previous}, стал {geo['ip']}")
    for other in others:
        if other["id"] != profile["id"] and (other.get("geo") or {}).get("ip") == geo["ip"]:
            warnings.append(f"Тот же IP выхода был у профиля {other['name']}")
    return errors, warnings
