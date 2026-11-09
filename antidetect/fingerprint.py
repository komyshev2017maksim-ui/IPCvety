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
