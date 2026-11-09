"""Командная строка: python -m antidetect <команда>."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import config, fingerprint, launcher, store
from .proxy import Proxy, ProxyError


def _print_table(rows: list[list[str]], header: list[str]) -> None:
    widths = [max(len(str(row[i])) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip())


def cmd_import(args) -> int:
    records = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
    if not isinstance(records, list):
        raise store.StoreError("В файле должен быть массив профилей")
    result = store.import_records(records)
    print(f"Создано: {len(result['created'])}, обновлено: {len(result['updated'])}, без изменений: {len(result['unchanged'])}")
    for label, key in (("Созданы", "created"), ("Обновлены", "updated")):
        if result[key]:
            print(f"{label}: {', '.join(result[key])}")
    return 0


def cmd_list(args) -> int:
    profiles = [p for p in store.all_profiles() if not p.get("deleted_at")]
    if not profiles:
        print("Профилей нет. Импортируйте instagram-profiles.json или создайте профиль командой add.")
        return 0
    rows = []
    for profile in profiles:
        proxy = Proxy.from_dict(profile.get("proxy"))
        place = profile.get("geo") or {}
        rows.append(
            [
                profile["name"],
                profile.get("brand") or "",
                proxy.endpoint if proxy else "нет",
                f"{place['city']}, {place['region']}" if place.get("city") else profile.get("proxy_city") or "",
                launcher.resolve_timezone(profile) or "?",
                place.get("ip") or "не проверен",
                "открыт" if launcher.is_running(profile["id"]) else (profile.get("last_opened") or "")[:10],
            ]
        )
    _print_table(rows, ["Профиль", "Бренд", "Прокси", "Город", "Пояс", "IP выхода", "Вход"])
    repeats = store.duplicates(profiles)
    if repeats:
        print("\nПовторы между профилями:")
        for what, names in repeats.items():
            print(f"  {what}: {', '.join(names)}")
    return 0


def cmd_add(args) -> int:
    with store.transaction() as profiles:
        profile = store.new_profile(
            profiles,
            args.name,
            brand=args.brand,
            site=args.site,
            proxy=args.proxy,
            proxy_city=args.city,
            timezone=args.timezone,
            notes=args.notes,
        )
    print(f"Создан профиль {profile['name']} (папка {config.browser_data_dir(profile['id'])})")
    return 0


def cmd_set(args) -> int:
    with store.transaction() as profiles:
        profile = store.find(profiles, args.profile)
        if profile is None:
            raise store.StoreError(f"Профиль не найден: {args.profile}")
        store.apply_fields(
            profile,
            {
                "brand": args.brand,
                "site": args.site,
                "instagram": args.instagram,
                "proxy": args.proxy,
                "proxy_city": args.city,
                "timezone": args.timezone,
                "notes": args.notes,
            },
        )
        if args.proxy_user is not None or args.proxy_pass is not None:
            if not profile.get("proxy"):
                raise store.StoreError("Сначала задайте адрес прокси: --proxy host:port")
            if args.proxy_user is not None:
                profile["proxy"]["username"] = args.proxy_user
            if args.proxy_pass is not None:
                profile["proxy"]["password"] = args.proxy_pass
        if args.viewport:
            try:
                width, height = (int(part) for part in args.viewport.lower().split("x"))
            except ValueError:
                raise store.StoreError("Размер окна задаётся так: --viewport 1280x640")
            screen = profile["fingerprint"]["screen"]
            if width > screen["width"] or height > screen["availHeight"]:
                raise store.StoreError(
                    f"Окно не может быть больше экрана профиля ({screen['width']}x{screen['availHeight']})"
                )
            profile["fingerprint"]["viewport"] = {"width": width, "height": height}
    print(f"Профиль {profile['name']} обновлён")
    return 0


def cmd_show(args) -> int:
    print(json.dumps(store.public_view(store.get(args.profile)), ensure_ascii=False, indent=2))
    return 0


def cmd_check(args) -> int:
    if args.all:
        targets = [p for p in store.all_profiles() if p.get("proxy")]
    elif args.profile:
        targets = [store.get(args.profile)]
    else:
        raise store.StoreError("Укажите профиль или --all")
    failed = 0
    for profile in targets:
        try:
            result = launcher.check_profile(profile["id"])
        except launcher.LaunchError as exc:
            print(f"{profile['name']}: ОШИБКА. {exc}")
            failed += 1
            continue
        place = result["geo"]
        print(
            f"{profile['name']}: IP {place['ip']}, {place['city']}, {place['region']}, {place['country']}, "
            f"пояс {place['timezone'] or 'неизвестен'}, {place['org']}"
        )
        for error in result["errors"]:
            print(f"  ОШИБКА: {error}")
        for warning in result["warnings"]:
            print(f"  Внимание: {warning}")
        failed += bool(result["errors"])
    return 1 if failed else 0


def cmd_open(args) -> int:
    launcher.open_profile(
        args.profile,
        url=args.url,
        skip_check=args.skip_check,
        force=args.force,
        no_proxy=args.no_proxy,
        # Без окна: для серверов и автотестов.
        headless=bool(os.environ.get("ANTIDETECT_HEADLESS")),
    )
    return 0


def cmd_stop(args) -> int:
    profile = store.get(args.profile)
    if not launcher.is_running(profile["id"]):
        print(f"Профиль {profile['name']} не открыт")
        return 0
    launcher.request_stop(profile["id"])
    print(f"Профиль {profile['name']} закрывается")
    return 0


def cmd_regen(args) -> int:
    with store.transaction() as profiles:
        profile = store.find(profiles, args.profile)
        if profile is None:
            raise store.StoreError(f"Профиль не найден: {args.profile}")
        if launcher.is_running(profile["id"]):
            raise store.StoreError("Сначала закройте браузер профиля")
        if not args.yes:
            answer = input(
                f"Сайты увидят {profile['name']} как новое устройство. Сменить отпечаток? [y/N] "
            )
            if answer.strip().lower() not in ("y", "yes", "д", "да"):
                print("Отменено")
                return 1
        profile["fingerprint"] = fingerprint.generate()
    print(f"У профиля {profile['name']} новый отпечаток")
    return 0


def cmd_rm(args) -> int:
    profile = store.get(args.profile)
    if launcher.is_running(profile["id"]):
        raise store.StoreError("Сначала закройте браузер профиля")
    data_dir = config.browser_data_dir(profile["id"])
    if not args.yes:
        answer = input(
            f"Удалить профиль {profile['name']} вместе с cookies и сессиями ({data_dir})? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes", "д", "да"):
            print("Отменено")
            return 1
    with store.transaction() as profiles:
        profiles[:] = [p for p in profiles if p["id"] != profile["id"]]
    shutil.rmtree(data_dir, ignore_errors=True)
    print(f"Профиль {profile['name']} удалён")
    return 0


def cmd_seed(args) -> int:
    from . import brands

    result = store.import_records(brands.records())
    print(f"Создано карточек: {len(result['created'])}, обновлено: {len(result['updated'])}")
    return 0


def cmd_cookies(args) -> int:
    from . import cookiefile

    if args.action == "export":
        cookies = launcher.export_cookies(args.profile)
        Path(args.file).write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Выгружено cookies: {len(cookies)} -> {args.file}")
    else:
        cookies = cookiefile.parse(Path(args.file).read_text(encoding="utf-8-sig"))
        print(f"Загружено cookies: {launcher.import_cookies(args.profile, cookies)}")
    return 0


def cmd_ui(args) -> int:
    from . import ui

    ui.serve(port=args.port, open_browser=not args.no_browser)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m antidetect",
        description="Браузерные профили с отдельными отпечатками, прокси и часовым поясом.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="создать или обновить профили из instagram-profiles.json")
    p.add_argument("file")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("list", help="список профилей и повторы между ними")
    p.set_defaults(func=cmd_list)

    def profile_fields(p, with_name: bool) -> None:
        if with_name:
            p.add_argument("name", help="название профиля, например IG-01")
        else:
            p.add_argument("profile", help="название профиля, номер или бренд")
        p.add_argument("--brand")
        p.add_argument("--site")
        p.add_argument("--proxy", help="host:port, host:port:user:pass или scheme://user:pass@host:port")
        p.add_argument("--city", help="город и штат прокси, например «Miami, FL»")
        p.add_argument("--timezone", help="часовой пояс вручную, например America/Chicago")
        p.add_argument("--notes")

    p = sub.add_parser("add", help="создать профиль")
    profile_fields(p, with_name=True)
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("set", help="изменить профиль")
    profile_fields(p, with_name=False)
    p.add_argument("--instagram", help="логин Instagram")
    p.add_argument("--proxy-user")
    p.add_argument("--proxy-pass")
    p.add_argument("--viewport", help="размер окна сайта, например 1280x640")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("show", help="показать профиль и его отпечаток")
    p.add_argument("profile")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("check", help="проверить прокси: IP, страна, город, пояс")
    p.add_argument("profile", nargs="?")
    p.add_argument("--all", action="store_true", help="все профили с прокси")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("open", help="открыть браузер профиля")
    p.add_argument("profile")
    p.add_argument("--url", help="страница при запуске")
    p.add_argument("--skip-check", action="store_true", help="не проверять IP перед запуском")
    p.add_argument("--force", action="store_true", help="открыть, даже если страна IP не та")
    p.add_argument("--no-proxy", action="store_true", help="разрешить запуск профиля, у которого прокси не задан (сайт увидит ваш IP)")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("stop", help="закрыть браузер профиля")
    p.add_argument("profile")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("regen", help="сменить отпечаток профиля")
    p.add_argument("profile")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_regen)

    p = sub.add_parser("rm", help="удалить профиль вместе с данными браузера")
    p.add_argument("profile")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("seed", help="создать карточки 33 брендов сетки")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("cookies", help="экспорт и импорт cookies закрытого профиля (JSON или Netscape)")
    p.add_argument("action", choices=["export", "import"])
    p.add_argument("profile")
    p.add_argument("file")
    p.set_defaults(func=cmd_cookies)

    p = sub.add_parser("ui", help="панель управления в браузере")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true", help="не открывать панель автоматически")
    p.set_defaults(func=cmd_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (store.StoreError, launcher.LaunchError, ProxyError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
