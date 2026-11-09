"""33 бренда сетки: название, сайт, группа. Пустой сайт значит «не подтверждён»."""

GROUPS = ["Лодки", "Водостоки", "Уборка домов", "Вывоз мусора", "Расчистка участков", "RV и кемперы"]

STATUSES = [
    ("none", "Не начат"), ("prep", "Готовлю"), ("reg", "Зарегистрирован"), ("warm", "Прогрев"),
    ("live", "Работает"), ("issue", "Проблема"), ("ban", "Бан"),
]

STEPS = [
    ("num", "Номер получен"), ("prx", "Прокси проверен"), ("reg", "Аккаунт зарегистрирован"),
    ("fill", "Аватар, описание и ссылка заполнены"), ("biz", "Переведён в бизнес-аккаунт"),
    ("fb", "Привязан к странице Facebook"), ("post", "Первый пост опубликован"),
]

BRANDS = [
    ("Captain Tom's Boat Removal", "ct-boatremoval.com", "Лодки"),
    ("NauticalHaul", "nauticalhaul.com", "Лодки"),
    ("All Clear Gutter", "allcleargutter.com", "Водостоки"),
    ("Gutter Cleaning Services", "gutter-cleaning-services.com", "Водостоки"),
    ("Gutters Cleaning", "gutteryard.com", "Водостоки"),
    ("Home Glow Cleaning Services", "homeglowcleaningservices.com", "Уборка домов"),
    ("Homesheen", "house-cleaning-services.com", "Уборка домов"),
    ("Nestly Home Services", "nestlyhomeservices.com", "Уборка домов"),
    ("All Star Hauling Services", "junkremoval111.com", "Вывоз мусора"),
    ("CW Services", "commercialwastemgnt.com", "Вывоз мусора"),
    ("IronHaul Co", "junkremovalandhaul.com", "Вывоз мусора"),
    ("Clearline Land Services", "landclearing-services.com", "Расчистка участков"),
    ("Florida Land Clearing Co", "floridalandclearingco.com", "Расчистка участков"),
    ("Iron Acre Landworks", "landclearingintexas.com", "Расчистка участков"),
    ("Land Clearing Ohio", "landclearing-ohio.com", "Расчистка участков"),
    ("LandClear Specialists", "landclearingspecialist.com", "Расчистка участков"),
    ("Pellegrino Land Works", "landclearingandmulching.com", "Расчистка участков"),
    ("Pruitt Range & Brush", "proclandclearing.com", "Расчистка участков"),
    ("Turner LAND & PINE", "landclearing-houston.com", "Расчистка участков"),
    ("Whitfield Clearing & Grading", "land-clearingservice.com", "Расчистка участков"),
    ("Junk Camper Removal", "junkcamper.com", "RV и кемперы"),
    ("Your Local Camper Removal", "camperremoval.com", "RV и кемперы"),
    # Сайты ниже взяты из карточек брендов в Google Drive владельца.
    ("A1 Removal Experts", "a1wasteremoval.com", "RV и кемперы"),
    ("Boat Removal Experts", "", "Лодки"),
    ("Boat Removal Hansons", "boatremoval.net", "Лодки"),
    ("Boat Removal Solutions", "", "Лодки"),
    ("Florida Boat Services", "boatremovalflorida.com", "Лодки"),
    ("Gutter Cleaning Now", "guttercleaningnow.com", "Водостоки"),
    ("EZ Hauling Services", "", "Вывоз мусора"),
    ("Florida Hauling & Disposal", "", "Вывоз мусора"),
    ("Heavy Junk Hauling", "", "Вывоз мусора"),
    ("Ace Removal Services", "", "Вывоз мусора"),
    ("RV Removal Experts", "rvcamperremoval.com", "RV и кемперы"),
]


def records() -> list[dict]:
    return [
        {"n": number, "brand": name, "site": site, "group": group, "profile_name": f"IG-{number:02d}"}
        for number, (name, site, group) in enumerate(BRANDS, start=1)
    ]
