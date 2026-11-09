import json

import pytest

from antidetect import fingerprint, geo, launcher, netcheck, store
from antidetect.proxy import ProxyError, parse_proxy


# --- прокси -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1.2.3.4:8000", ("http", "1.2.3.4", 8000, "", "")),
        ("1.2.3.4:8000:user:pa:ss", ("http", "1.2.3.4", 8000, "user", "pa:ss")),
        ("user:pass@host.example:9000", ("http", "host.example", 9000, "user", "pass")),
        ("socks5://1.2.3.4:1080", ("socks5", "1.2.3.4", 1080, "", "")),
        ("HTTP://u:p%40ss@1.2.3.4:80/", ("http", "1.2.3.4", 80, "u", "p@ss")),
    ],
)
def test_parse_proxy(text, expected):
    proxy = parse_proxy(text)
    assert (proxy.scheme, proxy.host, proxy.port, proxy.username, proxy.password) == expected


@pytest.mark.parametrize("text", ["host", "host:port", "ftp://1.2.3.4:21", "1.2.3.4:99999", "a:1:b"])
def test_parse_proxy_rejects(text):
    with pytest.raises(ProxyError):
        parse_proxy(text)


def test_empty_proxy_is_none():
    assert parse_proxy("  ") is None


# --- часовые пояса --------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Miami, FL", "America/New_York"),
        ("Pensacola, FL", "America/Chicago"),
        ("Houston, Texas", "America/Chicago"),
        ("El Paso, TX", "America/Denver"),
        ("Columbus OH", "America/New_York"),
        ("Phoenix, AZ", "America/Phoenix"),
        ("OH", "America/New_York"),
        ("Somewhere", None),
        ("", None),
    ],
)
def test_timezone_for(text, expected):
    assert geo.timezone_for(text) == expected


def test_timezone_priority():
    profile = {"geo": None, "timezone": "", "proxy_city": "Houston, TX"}
    assert launcher.resolve_timezone(profile) == "America/Chicago"
    profile["timezone"] = "America/Denver"
    assert launcher.resolve_timezone(profile) == "America/Denver"
    profile["geo"] = {"timezone": "America/New_York"}
    assert launcher.resolve_timezone(profile) == "America/New_York"


# --- отпечаток ------------------------------------------------------------------


def test_fingerprint_is_stable_for_seed_and_varies_between_seeds():
    assert fingerprint.generate("win", seed=42) == fingerprint.generate("win", seed=42)
    variants = {json.dumps(fingerprint.generate("win", seed=s), sort_keys=True) for s in range(1, 60)}
    assert len(variants) > 40


@pytest.mark.parametrize("platform", ["win", "mac", "linux"])
def test_fingerprint_is_plausible(platform):
    for seed in range(1, 300):
        fp = fingerprint.generate(platform, seed=seed)
        screen, view = fp["screen"], fp["viewport"]
        assert view["width"] <= screen["width"]
        assert view["height"] + fingerprint.BROWSER_CHROME_HEIGHT <= screen["availHeight"]
        # Окно должно помещаться на самый маленький ходовой монитор.
        assert view["width"] <= 1366 and view["height"] + fingerprint.BROWSER_CHROME_HEIGHT <= 768
        assert fp["deviceMemory"] in (4, 8)


def test_init_script_embeds_fingerprint():
    fp = fingerprint.generate("win", seed=7)
    script = fingerprint.build_init_script(fp)
    assert "__FINGERPRINT__" not in script
    assert fp["webgl"]["renderer"] in script


# --- хранилище ------------------------------------------------------------------

RECORDS = [
    {"n": 1, "brand": "Captain Tom's Boat Removal", "site": "ct-boatremoval.com",
     "profile_name": "IG-01", "proxy": "203.0.113.10:8000", "proxy_city": "Miami, FL",
     "timezone": "", "instagram": ""},
    {"n": 2, "brand": "NauticalHaul", "site": "nauticalhaul.com", "profile_name": "IG-02",
     "proxy": "", "proxy_city": "", "timezone": "", "instagram": ""},
]


def test_import_is_idempotent():
    first = store.import_records(RECORDS)
    assert first["created"] == ["IG-01", "IG-02"]
    before = store.all_profiles()

    second = store.import_records(RECORDS)
    assert second == {"created": [], "updated": [], "unchanged": ["IG-01", "IG-02"]}
    assert store.all_profiles() == before


def test_reimport_updates_fields_but_keeps_fingerprint_and_password():
    store.import_records(RECORDS)
    with store.transaction() as profiles:
        profile = store.find(profiles, "IG-01")
        profile["proxy"]["username"], profile["proxy"]["password"] = "user", "secret"
    fp_before = store.get("IG-01")["fingerprint"]

    changed = [dict(RECORDS[0], instagram="captaintom", proxy_city="Tampa, FL"), RECORDS[1]]
    result = store.import_records(changed)
    assert result["updated"] == ["IG-01"] and result["unchanged"] == ["IG-02"]

    profile = store.get("IG-01")
    assert profile["instagram"] == "captaintom" and profile["proxy_city"] == "Tampa, FL"
    assert profile["fingerprint"] == fp_before
    assert profile["proxy"]["password"] == "secret"  # панель экспортирует прокси без пароля


def test_empty_values_from_panel_do_not_erase_local_data():
    store.import_records(RECORDS)
    with store.transaction() as profiles:
        store.find(profiles, "IG-02")["proxy_city"] = "Dallas, TX"
        store.set_proxy(store.find(profiles, "IG-02"), "198.51.100.7:9000:u:p")
    store.import_records(RECORDS)
    profile = store.get("IG-02")
    assert profile["proxy_city"] == "Dallas, TX" and profile["proxy"]["host"] == "198.51.100.7"


def test_changing_proxy_forgets_old_ip_check():
    store.import_records(RECORDS)
    with store.transaction() as profiles:
        profile = store.find(profiles, "IG-01")
        profile["geo"] = {"ip": "203.0.113.10", "timezone": "America/New_York"}
        store.set_proxy(profile, "203.0.113.10:8000")  # тот же адрес
        assert profile["geo"] is not None
        store.set_proxy(profile, "198.51.100.1:8000")
        assert profile["geo"] is None


def test_find_by_name_number_and_brand():
    store.import_records(RECORDS)
    for key in ("IG-02", "ig-02", "2", "nauticalhaul"):
        assert store.get(key)["name"] == "IG-02"
    with pytest.raises(store.StoreError):
        store.get("IG-99")


def test_duplicates_and_public_view():
    store.import_records(RECORDS)
    with store.transaction() as profiles:
        store.set_proxy(store.find(profiles, "IG-02"), "203.0.113.10:8000:u:secret")
    profiles = store.all_profiles()
    assert store.duplicates(profiles) == {"прокси 203.0.113.10:8000": ["IG-01", "IG-02"]}
    view = store.public_view(store.get("IG-02"))
    assert "password" not in view["proxy"] and view["proxy"]["has_password"] is True
    assert "secret" not in json.dumps(view)


# --- разбор ответа сервисов IP и проверки ----------------------------------------


def test_parse_ipinfo_and_ipapi():
    ipinfo = netcheck._parse({"ip": "8.8.8.8", "city": "Miami", "region": "Florida", "country": "US",
                              "loc": "25.77,-80.19", "org": "AS1 Example", "timezone": "America/New_York"})
    assert (ipinfo["country"], ipinfo["lat"], ipinfo["lon"]) == ("US", 25.77, -80.19)
    ipapi = netcheck._parse({"ip": "8.8.4.4", "city": "Austin", "region_code": "TX", "country_code": "US",
                             "latitude": 30.27, "longitude": -97.74, "timezone": "America/Chicago"})
    assert (ipapi["region"], ipapi["timezone"], ipapi["lat"]) == ("TX", "America/Chicago", 30.27)
    assert netcheck._parse({"error": True}) is None


def test_problems_flags_wrong_country_and_shared_ip():
    settings = {"expected_country": "US"}
    profile = {"id": "a", "name": "A", "timezone": "America/Chicago", "geo": {"ip": "1.1.1.1"}}
    other = {"id": "b", "name": "B", "geo": {"ip": "2.2.2.2"}}
    found = {"ip": "2.2.2.2", "country": "KZ", "timezone": "Asia/Almaty"}
    errors, warnings = netcheck.problems(profile, found, settings, [profile, other])
    assert len(errors) == 1 and "KZ" in errors[0]
    assert any("сменился" in w for w in warnings)
    assert any("B" in w for w in warnings)
    assert any("America/Chicago" in w for w in warnings)


# --- согласование с железом, ручная правка, самопроверка ----------------------------


def test_adapt_grows_screen_to_real_monitor_and_matches_gpu_vendor():
    fp = fingerprint.generate("win", seed=3)
    fp["screen"].update(width=1366, height=768, availWidth=1366, availHeight=728)
    fp["webgl"] = {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"}
    notes = fingerprint.adapt(fp, (1920, 1080), "NVIDIA NVIDIA GeForce RTX 3070")
    assert len(notes) == 2
    assert (fp["screen"]["width"], fp["screen"]["height"], fp["screen"]["availHeight"]) == (1920, 1080, 1040)
    assert "NVIDIA" in fp["webgl"]["renderer"]
    assert fingerprint.adapt(fp, (1920, 1080), "NVIDIA GeForce") == [], "повторный запуск ничего не меняет"
    assert fingerprint.adapt(fp, (1280, 720), "llvmpipe") == [], "меньший монитор и неизвестная карта не трогают отпечаток"
    odd = fingerprint.generate("win", seed=4)
    fingerprint.adapt(odd, (5120, 2880), None)
    assert (odd["screen"]["width"], odd["screen"]["height"]) == (5120, 2880)


def test_edit_fingerprint_validates_values():
    fp = fingerprint.generate("win", seed=5)
    gpu = fingerprint.options("win")["gpus"][3]
    edited = fingerprint.edit(fp, {"screen": "2560x1440", "hardwareConcurrency": "16", "deviceMemory": "4", "gpu": gpu, "canvasNoise": False})
    assert (edited["screen"]["width"], edited["hardwareConcurrency"], edited["deviceMemory"]) == (2560, 16, 4)
    assert edited["webgl"]["renderer"] == gpu and edited["canvasNoise"] is False and edited["seed"] == fp["seed"]
    assert fingerprint.edit(fp, {"gpu": ""})["webgl"] is None
    same = {"screen": f"{fp['screen']['width']}x{fp['screen']['height']}", "gpu": fp["webgl"]["renderer"],
            "hardwareConcurrency": str(fp["hardwareConcurrency"]), "canvasNoise": True, "audioNoise": True}
    assert fingerprint.edit(fp, same) == fp
    for bad in ({"screen": "1x1"}, {"hardwareConcurrency": "3"}, {"gpu": "Voodoo 3"}):
        with pytest.raises(ValueError):
            fingerprint.edit(fp, bad)


def test_preflight_blocks_on_real_mismatch_only():
    from antidetect import preflight

    fp = fingerprint.generate("win", seed=6)
    profile = {"fingerprint": fp}
    settings = {"locale": "en-US"}
    good = {"webdriver": False, "cores": fp["hardwareConcurrency"], "timezone": "America/Chicago", "language": "en-US",
            "locale": "en-US", "screen": [fp["screen"]["width"], fp["screen"]["height"]], "outer": [1200, 700],
            "renderer": fp["webgl"]["renderer"], "native": True,
            "worker": {"cores": fp["hardwareConcurrency"], "timezone": "America/Chicago", "language": "en-US",
                       "renderer": fp["webgl"]["renderer"]}}
    report = preflight.summary(preflight.evaluate(good, profile, settings, "America/Chicago"))
    assert report["failed"] == [] and not report["blocked"] and report["total"] >= 9

    leaking = {**good, "timezone": "Asia/Almaty", "worker": {**good["worker"], "cores": 4}}
    report = preflight.summary(preflight.evaluate(leaking, profile, settings, "America/Chicago"))
    assert report["blocked"]
    assert {item["name"] for item in report["failed"]} == {"Часовой пояс", "Воркер видит то же устройство"}

    no_worker = {**good, "worker": None, "native": False}
    report = preflight.summary(preflight.evaluate(no_worker, profile, settings, "America/Chicago"))
    assert len(report["failed"]) == 2 and not report["blocked"], "некритичные проверки запуск не останавливают"
