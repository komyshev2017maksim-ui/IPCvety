"""Запуск браузера профиля: отдельная папка данных, прокси, пояс, отпечаток."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import config, fingerprint, geo as geo_tables, netcheck, store
from .proxy import Proxy

HEARTBEAT_EVERY = 1.0
HEARTBEAT_STALE = 6.0


class LaunchError(Exception):
    pass


# --- Признак «профиль открыт»: файл, который живой процесс обновляет раз в секунду


def _running_file(profile_id: str) -> Path:
    return config.browser_data_dir(profile_id) / "antidetect-running.json"


def _stop_file(profile_id: str) -> Path:
    return config.browser_data_dir(profile_id) / "antidetect-stop"


def is_running(profile_id: str) -> bool:
    path = _running_file(profile_id)
    try:
        return time.time() - path.stat().st_mtime < HEARTBEAT_STALE
    except FileNotFoundError:
        return False


def request_stop(profile_id: str) -> None:
    _stop_file(profile_id).write_text("stop", encoding="utf-8")


class _Heartbeat:
    def __init__(self, profile_id: str):
        self.path = _running_file(profile_id)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_EVERY):
            try:
                os.utime(self.path)
            except OSError:
                pass

    def __enter__(self):
        self.path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=3)
        self.path.unlink(missing_ok=True)


# --- Настройки внутри профиля Chrome ----------------------------------------


def _write_preferences(user_data_dir: Path, languages: list[str]) -> None:
    """WebRTC не должен ходить мимо прокси, языки браузера как у жителя США.

    Правим файл настроек Chrome до запуска: так значения действуют на уровне
    браузера, в том числе в воркерах, куда скрипт подмены не попадает.
    """
    path = user_data_dir / "Default" / "Preferences"
    prefs = {}
    if path.exists():
        try:
            prefs = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            prefs = {}
    prefs.setdefault("webrtc", {}).update(
        {
            "ip_handling_policy": "disable_non_proxied_udp",
            "multiple_routes_enabled": False,
            "nonproxied_udp_enabled": False,
        }
    )
    joined = ",".join(languages)
    prefs.setdefault("intl", {}).update({"accept_languages": joined, "selected_languages": joined})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs), encoding="utf-8")


def resolve_timezone(profile: dict) -> str | None:
    """Пояс по IP прокси, затем указанный вручную, затем по городу и штату."""
    return (
        (profile.get("geo") or {}).get("timezone")
        or profile.get("timezone")
        or geo_tables.timezone_for(profile.get("proxy_city", ""))
        or None
    )


def build_launch_options(profile: dict, settings: dict, *, headless: bool = False) -> dict:
    fp = profile["fingerprint"]
    languages = settings["languages"]
    locale = settings["locale"]
    user_data_dir = config.browser_data_dir(profile["id"])
    view = fp["viewport"]

    args = [
        "--disable-blink-features=AutomationControlled",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        f"--lang={locale}",
        f"--accept-lang={','.join(languages)}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={view['width']},{view['height'] + fingerprint.BROWSER_CHROME_HEIGHT}",
    ]
    env = dict(os.environ)
    env["LANGUAGE"] = locale.replace("-", "_")
    env["LANG"] = locale.replace("-", "_") + ".UTF-8"

    options = {
        "user_data_dir": str(user_data_dir),
        "headless": headless,
        "args": args,
        "ignore_default_args": ["--enable-automation"],
        "viewport": dict(view),
        "screen": {"width": fp["screen"]["width"], "height": fp["screen"]["height"]},
        "env": env,
    }

    # Песочница Chrome не работает под root в Linux (серверы, контейнеры).
    is_root = hasattr(os, "geteuid") and os.geteuid() == 0
    options["chromium_sandbox"] = not is_root

    timezone = resolve_timezone(profile)
    if timezone:
        options["timezone_id"] = timezone
        env["TZ"] = timezone

    place = profile.get("geo") or {}
    if place.get("lat") is not None and place.get("lon") is not None:
        # Координаты города по IP. Сайт получит их, только если вы сами
        # разрешите доступ к геолокации в браузере.
        options["geolocation"] = {"latitude": place["lat"], "longitude": place["lon"], "accuracy": 1500}

    proxy = Proxy.from_dict(profile.get("proxy"))
    if proxy:
        options["proxy"] = proxy.for_playwright()

    browser = settings.get("browser", "auto")
    if browser not in ("auto", "chromium"):
        if os.sep in browser or "/" in browser:
            options["executable_path"] = browser
        else:
            options["channel"] = browser
    return options


def _launch(playwright, options: dict, browser_setting: str):
    if browser_setting != "auto":
        return playwright.chromium.launch_persistent_context(**options)
    try:
        return playwright.chromium.launch_persistent_context(**options, channel="chrome")
    except Exception as exc:
        text = str(exc).lower()
        if "chrome" in text and ("not found" in text or "install" in text):
            # Google Chrome не установлен: берём Chromium из Playwright.
            return playwright.chromium.launch_persistent_context(**options)
        raise


def open_profile(
    key: str,
    *,
    url: str | None = None,
    skip_check: bool = False,
    force: bool = False,
    no_proxy: bool = False,
    headless: bool = False,
    on_ready=None,
    log=print,
) -> None:
    """Открывает браузер профиля и ждёт, пока его закроют."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    settings = config.settings()
    profile = store.get(key)
    profile_id = profile["id"]

    if is_running(profile_id):
        raise LaunchError(f"Профиль {profile['name']} уже открыт")
    proxy = Proxy.from_dict(profile.get("proxy"))
    if proxy is None and not no_proxy:
        raise LaunchError(
            f"У профиля {profile['name']} не задан прокси. Сайт увидит ваш настоящий IP. "
            "Задайте прокси или запустите с --no-proxy, если так и задумано."
        )

    _stop_file(profile_id).unlink(missing_ok=True)
    with _Heartbeat(profile_id), sync_playwright() as playwright:
        if proxy and not skip_check:
            log(f"Проверяю прокси {proxy.endpoint}...")
            try:
                found = netcheck.check_ip(playwright, proxy, settings)
            except netcheck.CheckError as exc:
                raise LaunchError(str(exc)) from exc
            errors, warnings = netcheck.problems(profile, found, settings, store.all_profiles())
            log(f"IP {found['ip']}: {found['city']}, {found['region']}, {found['country']}; пояс {found['timezone'] or 'неизвестен'}")
            for warning in warnings:
                log(f"Внимание: {warning}")
            if errors and not force:
                raise LaunchError("; ".join(errors) + ". Запуск отменён (--force, чтобы открыть всё равно)")
            with store.transaction() as profiles:
                store.find(profiles, profile_id)["geo"] = found
            profile["geo"] = found

        if not resolve_timezone(profile) and not force:
            raise LaunchError(
                "Неизвестен часовой пояс профиля: браузер показал бы пояс вашего компьютера. "
                "Проверьте прокси командой check или укажите город и штат"
            )

        options = build_launch_options(profile, settings, headless=headless)
        _write_preferences(Path(options["user_data_dir"]), settings["languages"])
        context = _launch(playwright, options, settings.get("browser", "auto"))
        try:
            context.add_init_script(
                fingerprint.build_init_script(profile["fingerprint"], settings["languages"])
            )

            def apply_locale(tab) -> None:
                # Формат дат и чисел как в США, даже если система русская.
                # Действует на уровне движка, поэтому и в воркерах тоже.
                try:
                    context.new_cdp_session(tab).send(
                        "Emulation.setLocaleOverride", {"locale": settings["locale"]}
                    )
                except Exception:
                    pass

            context.on("page", apply_locale)
            page = context.pages[0] if context.pages else context.new_page()
            for tab in context.pages:
                apply_locale(tab)
            target = url or settings["start_url"]
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:
                log(f"Страница {target} не открылась: {str(exc).splitlines()[0]}")

            with store.transaction() as profiles:
                store.find(profiles, profile_id)["last_opened"] = store.now_iso()
            log(f"Профиль {profile['name']} открыт. Закройте окно браузера, когда закончите.")

            if on_ready is not None:
                on_ready(context, page)
                return

            stop_file = _stop_file(profile_id)
            closed = threading.Event()
            context.on("close", lambda *_: closed.set())
            while not closed.is_set():
                if stop_file.exists():
                    break
                try:
                    context.wait_for_event("close", timeout=1000)
                    break
                except PlaywrightTimeout:
                    continue
                except Exception:
                    break  # браузер закрыли крестиком или он упал
        finally:
            try:
                context.close()
            except Exception:
                pass
            _stop_file(profile_id).unlink(missing_ok=True)
    log(f"Профиль {profile['name']} закрыт.")


def check_profile(key: str, log=print) -> dict:
    """Проверяет прокси профиля и запоминает IP, город и пояс."""
    from playwright.sync_api import sync_playwright

    settings = config.settings()
    profile = store.get(key)
    proxy = Proxy.from_dict(profile.get("proxy"))
    if proxy is None:
        raise LaunchError(f"У профиля {profile['name']} не задан прокси")
    with sync_playwright() as playwright:
        try:
            found = netcheck.check_ip(playwright, proxy, settings)
        except netcheck.CheckError as exc:
            raise LaunchError(str(exc)) from exc
    errors, warnings = netcheck.problems(profile, found, settings, store.all_profiles())
    with store.transaction() as profiles:
        store.find(profiles, profile["id"])["geo"] = found
    return {"geo": found, "errors": errors, "warnings": warnings}
