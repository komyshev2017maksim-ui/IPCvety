"""Запуск браузера профиля: своя папка данных, прокси, пояс, отпечаток.

Обычный Chrome (или Edge) запускается как отдельный процесс без флагов
автоматизации. Подмены ставятся через протокол DevTools до запуска скриптов
сайта, в каждой вкладке, фрейме и воркере.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import config, cookiefile, fingerprint, geo as geo_tables, netcheck, store
from .cdp import CDP, CDPError
from .proxy import Proxy
from .relay import Relay

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


# --- Поиск браузера -----------------------------------------------------------


def _candidates(kind: str) -> list[str]:
    env = os.environ
    if sys.platform == "win32":
        roots = [env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"), env.get("LOCALAPPDATA")]
        tails = {
            "chrome": r"Google\Chrome\Application\chrome.exe",
            "msedge": r"Microsoft\Edge\Application\msedge.exe",
            "chromium": r"Chromium\Application\chrome.exe",
        }
        return [os.path.join(root, tails[kind]) for root in roots if root]
    if sys.platform == "darwin":
        apps = {
            "chrome": "Google Chrome.app/Contents/MacOS/Google Chrome",
            "msedge": "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "chromium": "Chromium.app/Contents/MacOS/Chromium",
        }
        return [f"/Applications/{apps[kind]}", str(Path.home() / "Applications" / apps[kind])]
    names = {
        "chrome": ["google-chrome", "google-chrome-stable"],
        "msedge": ["microsoft-edge", "microsoft-edge-stable"],
        "chromium": ["chromium", "chromium-browser"],
    }
    return [found for name in names[kind] if (found := shutil.which(name))]


def find_browser(setting: str = "auto") -> str:
    if setting not in ("auto", "chrome", "msedge", "chromium"):
        if Path(setting).exists():
            return setting
        raise LaunchError(f"Браузер не найден по пути {setting}")
    kinds = ["chrome", "msedge", "chromium"] if setting == "auto" else [setting]
    for kind in kinds:
        for path in _candidates(kind):
            if Path(path).exists():
                return path
    raise LaunchError(
        "Не нашёл Google Chrome или Microsoft Edge. Установите Chrome либо укажите путь "
        'к браузеру в settings.json: {"browser": "D:\\\\...\\\\chrome.exe"}'
    )


# --- Настройки внутри профиля Chrome ----------------------------------------


def _write_preferences(user_data_dir: Path, languages: list[str]) -> None:
    """WebRTC не должен ходить мимо прокси, языки браузера как у жителя США."""
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
    # Не предлагать «восстановить вкладки» после закрытия из панели.
    prefs.setdefault("profile", {})["exit_type"] = "Normal"
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


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def build_command(profile: dict, settings: dict, *, debug_port: int, proxy_url: str | None, headless: bool) -> tuple[list[str], dict]:
    """Командная строка и окружение Chrome. Флагов автоматизации здесь нет."""
    fp = profile["fingerprint"]
    languages, locale = settings["languages"], settings["locale"]
    view = fp["viewport"]
    args = [
        find_browser(settings.get("browser", "auto")),
        f"--user-data-dir={config.browser_data_dir(profile['id'])}",
        # Порт должен быть ненулевым: при нуле Chrome выставляет navigator.webdriver.
        f"--remote-debugging-port={debug_port}",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        f"--lang={locale}",
        f"--accept-lang={','.join(languages)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=WebRtcHideLocalIpsWithMdns",
        f"--window-size={view['width']},{view['height'] + fingerprint.BROWSER_CHROME_HEIGHT}",
    ]
    if proxy_url:
        args += [f"--proxy-server={proxy_url}", "--proxy-bypass-list=<-loopback>"]
    if headless:
        args.append("--headless=new")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")  # песочница Chrome не работает под root (серверы, контейнеры)
    args.append("about:blank")

    env = dict(os.environ)
    env["LANGUAGE"] = locale.replace("-", "_")
    env["LANG"] = locale.replace("-", "_") + ".UTF-8"
    timezone = resolve_timezone(profile)
    if timezone:
        env["TZ"] = timezone
    return args, env


class Tab:
    """Вкладка для тестов и служебных действий."""

    def __init__(self, cdp: CDP, session: str):
        self.cdp, self.session = cdp, session

    async def evaluate(self, expression: str):
        result = await self.cdp.send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            self.session,
            timeout=60,
        )
        if "exceptionDetails" in result:
            raise CDPError(result["exceptionDetails"].get("exception", {}).get("description", "ошибка в странице"))
        return result["result"].get("value")

    async def goto(self, url: str) -> None:
        loaded = asyncio.get_running_loop().create_future()

        async def on_load(params, session):
            if session == self.session and not loaded.done():
                loaded.set_result(True)

        self.cdp.on("Page.loadEventFired", on_load)
        result = await self.cdp.send("Page.navigate", {"url": url}, self.session, timeout=60)
        if result.get("errorText"):
            raise CDPError(result["errorText"])
        await asyncio.wait_for(loaded, 60)


class _Session:
    """Одна запущенная копия браузера с подменами."""

    def __init__(self, profile: dict, settings: dict):
        self.profile, self.settings = profile, settings
        self.script = fingerprint.build_init_script(profile["fingerprint"])
        self.cdp: CDP | None = None
        self.pages: list[str] = []
        self.first_page = asyncio.get_running_loop().create_future()

    async def _emulate(self, session: str, *, worker: bool) -> None:
        fp, send = self.profile["fingerprint"], self.cdp.send
        commands = []
        timezone = resolve_timezone(self.profile)
        if timezone:
            commands.append(("Emulation.setTimezoneOverride", {"timezoneId": timezone}))
        if fp.get("hardwareConcurrency") and fingerprint.spoof_flags()["hardware"]:
            commands.append(("Emulation.setHardwareConcurrencyOverride", {"hardwareConcurrency": fp["hardwareConcurrency"]}))
        if not worker:
            commands.append(("Emulation.setLocaleOverride", {"locale": self.settings["locale"]}))
            # width и height равны нулю: размер окна не трогаем, подменяем только экран.
            commands.append(
                (
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": 0,
                        "height": 0,
                        "deviceScaleFactor": 0,
                        "mobile": False,
                        "screenWidth": fp["screen"]["width"],
                        "screenHeight": fp["screen"]["height"],
                    },
                )
            )
            place = self.profile.get("geo") or {}
            if place.get("lat") is not None and place.get("lon") is not None:
                # Сайт получит координаты, только если вы сами разрешите ему геолокацию.
                commands.append(
                    ("Emulation.setGeolocationOverride", {"latitude": place["lat"], "longitude": place["lon"], "accuracy": 1500})
                )
        for method, params in commands:
            try:
                await send(method, params, session)
            except CDPError:
                pass  # не каждая цель поддерживает каждую подмену

    async def on_attached(self, params: dict, parent: str | None) -> None:
        session, info = params["sessionId"], params["targetInfo"]
        kind, url = info.get("type", ""), info.get("url", "")
        send = self.cdp.send
        try:
            if url.startswith(("chrome-extension://", "chrome://", "devtools://")):
                return
            if kind in ("page", "iframe"):
                await self._emulate(session, worker=False)
                await send("Page.enable", {}, session)
                await send("Page.addScriptToEvaluateOnNewDocument", {"source": self.script, "runImmediately": True}, session)
                # Воркеры и фреймы с других сайтов прикрепляются к своей вкладке.
                await send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}, session)
                if kind == "page":
                    self.pages.append(session)
                    if not self.first_page.done():
                        self.first_page.set_result(session)
            elif "worker" in kind:
                await self._emulate(session, worker=True)
                await send("Runtime.evaluate", {"expression": self.script}, session)
        except CDPError:
            pass
        finally:
            # Цель стоит на паузе, пока мы её не отпустим: отпускаем в любом случае.
            if params.get("waitingForDebugger"):
                try:
                    await send("Runtime.runIfWaitingForDebugger", {}, session)
                except CDPError:
                    pass


async def _wait_for_devtools(port: int, process: subprocess.Popen, timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LaunchError("Браузер закрылся сразу после запуска. Возможно, профиль уже открыт в другом окне")
        try:
            raw = await asyncio.to_thread(lambda: opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2).read())
            return json.loads(raw)["webSocketDebuggerUrl"]
        except (OSError, ValueError, KeyError):
            await asyncio.sleep(0.15)
    raise LaunchError("Браузер не ответил за 30 секунд")


async def _terminate(process: subprocess.Popen, cdp: CDP | None) -> None:
    if process.poll() is None and cdp is not None and not cdp.closed.is_set():
        try:
            await cdp.send("Browser.close", timeout=5)
        except (CDPError, asyncio.TimeoutError):
            pass
    for _ in range(50):
        if process.poll() is not None:
            break
        await asyncio.sleep(0.1)
    if process.poll() is None:
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, 5)
        except subprocess.TimeoutExpired:
            process.kill()
    if cdp is not None:
        await cdp.close()


async def _check_through(proxy: Proxy, settings: dict) -> dict:
    relay = await Relay(proxy).start()
    try:
        return await asyncio.to_thread(netcheck.check_ip, relay.url, settings)
    finally:
        await relay.stop()


async def _open(profile: dict, settings: dict, *, url, skip_check, force, headless, on_ready, log) -> None:
    profile_id = profile["id"]
    proxy = Proxy.from_dict(profile.get("proxy"))
    relay = process = cdp = None
    try:
        if proxy:
            relay = await Relay(proxy).start()
        if proxy and not skip_check:
            log(f"Проверяю прокси {proxy.endpoint}...")
            try:
                found = await asyncio.to_thread(netcheck.check_ip, relay.url, settings)
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

        _write_preferences(config.browser_data_dir(profile_id), settings["languages"])
        port = _free_port()
        command, env = build_command(profile, settings, debug_port=port, proxy_url=relay.url if relay else None, headless=headless)
        process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cdp = await CDP.connect(await _wait_for_devtools(port, process))

        session = _Session(profile, settings)
        session.cdp = cdp
        cdp.on("Target.attachedToTarget", session.on_attached)
        await cdp.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        tab = Tab(cdp, await asyncio.wait_for(session.first_page, 20))

        target = url or settings["start_url"]
        try:
            await tab.goto(target)
        except (CDPError, asyncio.TimeoutError) as exc:
            log(f"Страница {target} не открылась: {exc}")

        with store.transaction() as profiles:
            store.find(profiles, profile_id)["last_opened"] = store.now_iso()
        store.log_event(profile_id, "Открыт")
        log(f"Профиль {profile['name']} открыт. Закройте окно браузера, когда закончите.")

        if on_ready is not None:
            await on_ready(tab)
            return

        stop_file = _stop_file(profile_id)
        while process.poll() is None and not cdp.closed.is_set() and not stop_file.exists():
            await asyncio.sleep(0.5)
    finally:
        if process is not None:
            await _terminate(process, cdp)
        if relay is not None:
            await relay.stop()
        _stop_file(profile_id).unlink(missing_ok=True)


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
    settings = config.settings()
    profile = store.get(key)
    if profile.get("deleted_at"):
        raise LaunchError(f"Профиль {profile['name']} в корзине. Сначала восстановите его")
    if is_running(profile["id"]):
        raise LaunchError(f"Профиль {profile['name']} уже открыт")
    if not profile.get("proxy") and not no_proxy:
        raise LaunchError(
            f"У профиля {profile['name']} не задан прокси. Сайт увидит ваш настоящий IP. "
            "Задайте прокси или запустите с --no-proxy, если так и задумано."
        )
    _stop_file(profile["id"]).unlink(missing_ok=True)
    with _Heartbeat(profile["id"]):
        asyncio.run(
            _open(profile, settings, url=url, skip_check=skip_check, force=force, headless=headless, on_ready=on_ready, log=log)
        )
    store.log_event(profile["id"], "Закрыт")
    log(f"Профиль {profile['name']} закрыт.")


def check_profile(key: str, log=print) -> dict:
    """Проверяет прокси профиля и запоминает IP, город и пояс."""
    settings = config.settings()
    profile = store.get(key)
    proxy = Proxy.from_dict(profile.get("proxy"))
    if proxy is None:
        raise LaunchError(f"У профиля {profile['name']} не задан прокси")
    try:
        found = asyncio.run(_check_through(proxy, settings))
    except netcheck.CheckError as exc:
        raise LaunchError(str(exc)) from exc
    errors, warnings = netcheck.problems(profile, found, settings, store.all_profiles())
    with store.transaction() as profiles:
        store.find(profiles, profile["id"])["geo"] = found
    store.log_event(profile["id"], f"Проверка IP: {found['ip']}, {found['city']} {found['region']}, {found.get('ping_ms', '?')} мс")
    return {"geo": found, "errors": errors, "warnings": warnings}


# --- Cookies: экспорт и импорт у закрытого профиля ---------------------------------


async def _with_hidden_browser(profile: dict, settings: dict, action):
    """Поднимает браузер профиля без окна и без сети, чтобы прочитать или записать cookies."""
    port = _free_port()
    command, env = build_command(profile, settings, debug_port=port, proxy_url="http://127.0.0.1:9", headless=True)
    process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp = None
    try:
        cdp = await CDP.connect(await _wait_for_devtools(port, process))
        return await action(cdp)
    finally:
        await _terminate(process, cdp)


def _require_closed(profile: dict) -> None:
    if is_running(profile["id"]):
        raise LaunchError("Сначала закройте браузер профиля")


def export_cookies(key: str) -> list[dict]:
    profile = store.get(key)
    _require_closed(profile)

    async def action(cdp: CDP):
        return (await cdp.send("Storage.getCookies"))["cookies"]

    with _Heartbeat(profile["id"]):
        return asyncio.run(_with_hidden_browser(profile, config.settings(), action))


def import_cookies(key: str, cookies: list[dict]) -> int:
    profile = store.get(key)
    _require_closed(profile)

    cookies = cookiefile.normalize(cookies)

    async def action(cdp: CDP):
        await cdp.send("Storage.setCookies", {"cookies": cookies})
        return len(cookies)

    with _Heartbeat(profile["id"]):
        count = asyncio.run(_with_hidden_browser(profile, config.settings(), action))
    store.log_event(profile["id"], f"Импортировано cookies: {count}")
    return count
