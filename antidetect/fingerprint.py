"""Генерация отпечатка профиля.

Отпечаток создаётся один раз и хранится вместе с профилем: устройство должно
выглядеть одинаково при каждом входе. Меняется он только командой ``regen``.

Операционную систему и версию браузера не подменяем: они берутся от настоящего
браузера, иначе заголовки, шрифты и поведение движка разойдутся с заявленным.
"""

from __future__ import annotations

import random
import secrets
import sys
from pathlib import Path

INJECT_JS = Path(__file__).with_name("inject.js")

# (ширина, высота, вес). Веса близки к реальной статистике десктопов.
SCREENS = [
    (1920, 1080, 50),
    (2560, 1440, 14),
    (1536, 864, 12),
    (1366, 768, 10),
    (1440, 900, 7),
    (1600, 900, 7),
]

HARDWARE = [  # (ядра, память ГБ, вес). deviceMemory в Chrome не бывает больше 8.
    (4, 8, 20), (8, 8, 35), (12, 8, 20), (16, 8, 15), (6, 8, 5), (4, 4, 5),
]

WEBGL = {
    "win": [
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 2048SP Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ],
    "mac": [
        ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)"),
        ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)"),
        ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Pro, Unspecified Version)"),
        ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)"),
    ],
    "linux": [
        ("Google Inc. (Intel)", "ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6)"),
        ("Google Inc. (Intel)", "ANGLE (Intel, Mesa Intel(R) Xe Graphics (TGL GT2), OpenGL 4.6)"),
        ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon Graphics (radeonsi, renoir, LLVM 15.0.7, DRM 3.54), OpenGL 4.6)"),
    ],
}

# Высота панели задач: Windows 10, Windows 11; строка меню macOS; панель GNOME.
TASKBAR = {"win": [40, 48], "mac": [25], "linux": [27, 32]}
# Сколько вертикали съедают вкладки и адресная строка браузера.
BROWSER_CHROME_HEIGHT = 90


def host_platform() -> str:
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def _weighted(rng: random.Random, rows: list[tuple]) -> tuple:
    return rng.choices(rows, weights=[row[-1] for row in rows], k=1)[0][:-1]


def generate(platform: str | None = None, seed: int | None = None) -> dict:
    platform = platform or host_platform()
    if seed is None:
        seed = secrets.randbits(31) or 1
    rng = random.Random(seed)

    width, height = _weighted(rng, SCREENS)
    cores, memory = _weighted(rng, HARDWARE)
    vendor, renderer = rng.choice(WEBGL[platform])
    taskbar = rng.choice(TASKBAR[platform])

    # Окно браузера: не на весь экран, как у большинства людей на больших
    # мониторах, и гарантированно помещается на обычный 1366x768.
    view_w = min(width - rng.choice([0, 16, 80, 160]), rng.choice([1280, 1300, 1320, 1350]))
    view_h = min(height - taskbar - BROWSER_CHROME_HEIGHT, rng.choice([620, 640, 660]))

    return {
        "seed": seed,
        "platform": platform,
        "screen": {
            "width": width,
            "height": height,
            "availWidth": width,
            "availHeight": height - taskbar,
            "colorDepth": 24,
        },
        "viewport": {"width": view_w, "height": view_h},
        "hardwareConcurrency": cores,
        "deviceMemory": memory,
        "webgl": {"vendor": vendor, "renderer": renderer},
        "canvasNoise": True,
        "audioNoise": True,
    }


def spoof_flags() -> dict:
    """Отдельные подмены можно выключить в settings.json: {"spoof": {"webgl": false}}."""
    from . import config

    flags = {"hardware": True, "webgl": True, "canvas": True, "audio": True}
    flags.update(config.settings().get("spoof") or {})
    return flags


def build_init_script(fingerprint: dict) -> str:
    """JS, который выполняется в каждой вкладке, фрейме и воркере до скриптов сайта."""
    import json

    flags = spoof_flags()
    payload = dict(fingerprint)
    if not flags["hardware"]:
        payload["deviceMemory"] = None
    if not flags["webgl"]:
        payload["webgl"] = None
    payload["canvasNoise"] = bool(payload.get("canvasNoise")) and flags["canvas"]
    payload["audioNoise"] = bool(payload.get("audioNoise")) and flags["audio"]
    return INJECT_JS.read_text(encoding="utf-8").replace("__FINGERPRINT__", json.dumps(payload))


# --- Согласование с настоящим железом и ручная правка --------------------------------

# Куда «расти», если настоящий монитор больше заявленного экрана.
LARGE_SCREENS = [(1920, 1080), (1920, 1200), (2560, 1440), (3440, 1440), (3840, 2160)]
CORE_CHOICES = [2, 4, 6, 8, 12, 16, 24]
MEMORY_CHOICES = [2, 4, 8]


def gpu_family(text: str | None) -> str | None:
    """«intel», «nvidia», «amd» или «apple» по названию видеокарты."""
    low = (text or "").lower()
    for family, marks in (("nvidia", ("nvidia", "geforce")), ("amd", ("amd", "radeon")), ("intel", ("intel",)), ("apple", ("apple",))):
        if any(mark in low for mark in marks):
            return family
    return None


def adapt(fp: dict, real_screen: tuple[int, int] | None, real_gpu: str | None) -> list[str]:
    """Убирает расхождения отпечатка с компьютером, на котором он запущен.

    Окно браузера не может быть больше настоящего монитора, поэтому заявленный
    экран не должен быть меньше него. Видеокарта остаётся того же производителя,
    что настоящая: тогда возможности WebGL сходятся с названием.
    Меняет ``fp`` на месте и возвращает список сделанных правок.
    """
    notes = []
    if real_screen:
        width, height = real_screen
        screen = fp["screen"]
        if screen["width"] < width or screen["height"] < height:
            fits = [size for size in LARGE_SCREENS if size[0] >= width and size[1] >= height]
            new_w, new_h = min(fits, key=lambda size: size[0] * size[1]) if fits else (width, height)
            taskbar = screen["height"] - screen["availHeight"]
            fp["screen"] = {**screen, "width": new_w, "height": new_h, "availWidth": new_w, "availHeight": new_h - taskbar}
            notes.append(f"экран {screen['width']}x{screen['height']} -> {new_w}x{new_h} (монитор {width}x{height})")
    family = gpu_family(real_gpu)
    if fp.get("webgl") and family and gpu_family(fp["webgl"]["renderer"]) != family:
        same = [pair for pair in WEBGL.get(fp.get("platform") or host_platform(), []) if gpu_family(pair[1]) == family]
        if same:
            vendor, renderer = random.Random(fp["seed"]).choice(same)
            fp["webgl"] = {"vendor": vendor, "renderer": renderer}
            notes.append(f"видеокарта подобрана под настоящую ({family})")
    return notes


def options(platform: str | None = None) -> dict:
    """Варианты для ручной правки отпечатка в панели."""
    platform = platform or host_platform()
    screens = sorted({(w, h) for w, h, _ in SCREENS} | set(LARGE_SCREENS))
    return {
        "screens": [f"{w}x{h}" for w, h in screens],
        "cores": CORE_CHOICES,
        "memory": MEMORY_CHOICES,
        "gpus": [renderer for _, renderer in WEBGL[platform]],
    }


def edit(fp: dict, changes: dict) -> dict:
    """Возвращает отпечаток с ручными правками из панели. Неверные значения отклоняет."""
    result = {**fp, "screen": dict(fp["screen"])}
    choices = options(fp.get("platform"))
    current_screen = f"{fp['screen']['width']}x{fp['screen']['height']}"
    if changes.get("screen") and changes["screen"] != current_screen:
        if changes["screen"] not in choices["screens"]:
            raise ValueError("Неизвестный размер экрана")
        width, height = (int(part) for part in changes["screen"].split("x"))
        taskbar = fp["screen"]["height"] - fp["screen"]["availHeight"]
        result["screen"].update(width=width, height=height, availWidth=width, availHeight=height - taskbar)
    for key, allowed in (("hardwareConcurrency", choices["cores"]), ("deviceMemory", choices["memory"])):
        if changes.get(key) not in (None, ""):
            value = int(changes[key])
            if value not in allowed and value != fp.get(key):
                raise ValueError("Недопустимое значение ядер или памяти")
            result[key] = value
    if "gpu" in changes and changes["gpu"] is not None and changes["gpu"] != (fp.get("webgl") or {}).get("renderer"):
        if changes["gpu"] == "":
            result["webgl"] = None  # настоящая видеокарта
        else:
            pair = next((p for p in WEBGL[fp.get("platform") or host_platform()] if p[1] == changes["gpu"]), None)
            if pair is None:
                raise ValueError("Неизвестная видеокарта")
            result["webgl"] = {"vendor": pair[0], "renderer": pair[1]}
    for key in ("canvasNoise", "audioNoise"):
        if key in changes and changes[key] is not None:
            result[key] = bool(changes[key])
    return result
