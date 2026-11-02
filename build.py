#!/usr/bin/env python3
"""
Сборщик календаря спроса для отеля.

Запуск:  python build.py            (обычный режим, ходит в интернет)
         python build.py --offline  (без интернета, только кэш и ручные события)

Что делает:
  1. Собирает события из источников (Крокус Экспо, KudaGo, производственный календарь,
     ручной файл manual_events.csv).
  2. Если источник не ответил, берёт его прошлые данные из cache_<источник>.json,
     чтобы календарь не «пустел» из-за сбоя одного сайта.
  3. Считает баллы спроса по дням и пишет events.json, который читает index.html.

Только стандартная библиотека Python, ничего устанавливать не нужно.
"""
import csv
import datetime as dt
import hashlib
import html
import json
import math
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
OFFLINE = "--offline" in sys.argv
TODAY = dt.date.today()
UA = "Mozilla/5.0 (hotel-demand-calendar; +https://github.com)"

MONTHS = {
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # Russian (родительный падеж)
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- helpers

def fetch(url, timeout=30):
    if OFFLINE:
        raise RuntimeError("offline mode")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru,en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")


def html_to_lines(page):
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr|td|span|a|strong|b|em|i)>", "\n", page)
    page = re.sub(r"<[^>]+>", " ", page)
    page = html.unescape(page)
    lines = [re.sub(r"\s+", " ", ln).strip(" *_#") for ln in page.split("\n")]
    return [ln for ln in lines if ln]


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_venue(text):
    t = (text or "").lower()
    for v in CONFIG["venues"]:
        if any(a in t for a in v["aliases"]):
            return v
    return None


def distance_info(venue=None, lat=None, lon=None):
    """Возвращает (км или None, множитель)."""
    if venue and venue.get("lat"):
        lat, lon = venue["lat"], venue["lon"]
    if lat is None:
        return None, 1.0
    km = haversine_km(CONFIG["hotel"]["lat"], CONFIG["hotel"]["lon"], lat, lon)
    for rule in CONFIG["distance_factors"]:
        if km <= rule["max_km"]:
            return round(km, 1), rule["factor"]
    return round(km, 1), 1.0


def make_id(*parts):
    return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:10]


def ev(title, start, end, venue_name, weight, category, source, url="", detail="", lat=None, lon=None):
    venue = find_venue(venue_name)
    km, factor = distance_info(venue, lat, lon)
    return {
        "id": make_id(source, title.lower(), start),
        "title": title.strip(),
        "start": start.isoformat() if isinstance(start, dt.date) else start,
        "end": end.isoformat() if isinstance(end, dt.date) else end,
        "venue": (venue or {}).get("name", venue_name or ""),
        "km": km,
        "base_weight": weight,
        "weight": round(weight * factor, 2),
        "category": category,
        "source": source,
        "url": url,
        "detail": detail,
    }


# ---------------------------------------------------------------- sources

DATE_RANGE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-zА-Яа-яё]+)\s+(\d{4})\s*[—–-]\s*(\d{1,2})\s+([A-Za-zА-Яа-яё]+)\s+(\d{4})")


def parse_crocus_lines(lines):
    """Разбирает текст страницы календаря Крокус Экспо.
    На странице у каждой выставки: Название / Подзаголовок / «03 February 2026 — 06 February 2026» / ... / Location: Pavilion 1 Halls 3 / 4
    Выставки без точных дат (только «December») пропускаются."""
    out = []
    for i, ln in enumerate(lines):
        m = DATE_RANGE_RE.search(ln)
        if not m:
            continue
        d1, m1, y1, d2, m2, y2 = m.groups()
        if m1.lower() not in MONTHS or m2.lower() not in MONTHS:
            continue
        start = dt.date(int(y1), MONTHS[m1.lower()], int(d1))
        end = dt.date(int(y2), MONTHS[m2.lower()], int(d2))
        title = lines[i - 2] if i >= 2 else lines[i - 1]
        subtitle = lines[i - 1] if i >= 1 else ""
        # Если подзаголовка нет, строка i-2 может оказаться мусором — берём i-1.
        if DATE_RANGE_RE.search(title) or len(title) > 120:
            title, subtitle = subtitle, ""
        halls = 1
        for nxt in lines[i + 1:i + 25]:
            if DATE_RANGE_RE.search(nxt):
                break
            if re.search(r"(?i)(pavilion|павильон)", nxt):
                halls = max(1, len(re.findall(r"\d+", re.sub(r"(?i)(pavilions?|павильоны?)\s*[\d/ ]+", "", nxt))) or 1)
                break
        # Вес: малая выставка (1 зал) = 2, огромная (8+ залов) = 7
        weight = min(7, 1 + halls)
        out.append(ev(title, start, end, "Крокус Экспо", weight, "выставка", "crocus_expo",
                      url="https://www.crocus-expo.ru/exhibition/", detail=f"{subtitle} · залов: {halls}".strip(" ·")))
    return out


def src_crocus_expo():
    events = []
    months = []
    d = TODAY.replace(day=1)
    for _ in range(14):
        months.append((d.year, d.month))
        d = (d + dt.timedelta(days=32)).replace(day=1)
    ok = 0
    for y, m in months:
        url = f"https://eng.crocus-expo.ru/exhibition/?year={y}&month={m}"
        try:
            events += parse_crocus_lines(html_to_lines(fetch(url)))
            ok += 1
        except Exception as e:  # noqa
            log(f"  crocus {y}-{m:02d}: {e}")
    if ok == 0:
        raise RuntimeError("Крокус Экспо не ответил ни на один запрос")
    return events


def src_kudago():
    """Афиша KudaGo: оставляем только события на крупных площадках из config.venues."""
    since = int(dt.datetime.combine(TODAY, dt.time()).timestamp())
    until = int(dt.datetime.combine(TODAY + dt.timedelta(days=CONFIG["horizon_days"]), dt.time()).timestamp())
    url = ("https://kudago.com/public-api/v1.4/events/?location=msk&page_size=100"
           f"&actual_since={since}&actual_until={until}"
           "&fields=id,title,dates,place,site_url,categories&expand=place&lang=ru")
    out, pages = [], 0
    while url and pages < CONFIG["sources"]["kudago"]["max_pages"]:
        data = json.loads(fetch(url))
        pages += 1
        for item in data.get("results", []):
            place = item.get("place") or {}
            venue = find_venue(place.get("title", "")) or find_venue(place.get("address", ""))
            if not venue:
                continue
            coords = place.get("coords") or {}
            for d in item.get("dates", [])[:30]:
                s, e = d.get("start"), d.get("end")
                if not s or s <= 0:
                    continue
                sd = dt.datetime.fromtimestamp(s).date()
                ed = dt.datetime.fromtimestamp(e).date() if e and e > 0 else sd
                if (ed - sd).days > 7:   # длинные выставки/постоянные экспозиции не двигают загрузку
                    continue
                if sd < TODAY:
                    continue
                cat = ", ".join(item.get("categories") or [])
                out.append(ev(item["title"].capitalize(), sd, ed, venue["name"], venue["weight"],
                              "концерт/шоу" if "concert" in cat else "событие", "kudago",
                              url=item.get("site_url", ""), detail=place.get("title", ""),
                              lat=coords.get("lat"), lon=coords.get("lon")))
        url = data.get("next")
    log(f"  kudago: страниц {pages}")
    return out


FIXED_HOLIDAYS = {(1, d) for d in range(1, 9)} | {(2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)}


def days_off_for_year(year):
    """Нерабочие дни года. Сначала пытаемся взять официальный производственный календарь
    (с переносами), иначе — выходные + фиксированные праздники."""
    try:
        data = json.loads(fetch(f"https://xmlcalendar.ru/data/ru/{year}/calendar.json"))
        off = set()
        for m in data["months"]:
            for tok in m["days"].split(","):
                if "*" in tok:      # сокращённый рабочий день
                    continue
                off.add(dt.date(year, m["month"], int(re.sub(r"\D", "", tok))))
        return off, True
    except Exception as e:  # noqa
        log(f"  производственный календарь {year}: {e} — использую базовый")
        off = set()
        d = dt.date(year, 1, 1)
        while d.year == year:
            if d.weekday() >= 5 or (d.month, d.day) in FIXED_HOLIDAYS:
                off.add(d)
            d += dt.timedelta(days=1)
        return off, False


def src_holidays():
    w = CONFIG["calendar_weights"]
    off = set()
    for y in {TODAY.year, (TODAY + dt.timedelta(days=CONFIG["horizon_days"])).year}:
        off |= days_off_for_year(y)[0]
    out = []
    d, end = TODAY, TODAY + dt.timedelta(days=CONFIG["horizon_days"])
    while d <= end:
        if d in off:
            # длина блока выходных, в который попадает день
            a = d
            while a - dt.timedelta(days=1) in off:
                a -= dt.timedelta(days=1)
            b = d
            while b + dt.timedelta(days=1) in off:
                b += dt.timedelta(days=1)
            length = (b - a).days + 1
            holiday = (d.month, d.day) in FIXED_HOLIDAYS
            if length >= 3 and a == d:
                out.append({**ev(f"Длинные выходные ({length} дн.)", a, b, "", w["long_weekend_day"],
                                 "выходные", "holidays"), "per_day": True})
            if holiday:
                out.append({**ev("Государственный праздник", d, d, "", w["holiday"], "праздник", "holidays"),
                            "per_day": True})
            elif length < 3 and d.weekday() >= 5:
                out.append({**ev("Выходной", d, d, "", w["weekend"], "выходные", "holidays"),
                            "per_day": True, "hidden": True})
        d += dt.timedelta(days=1)
    return out


def src_manual():
    out = []
    path = DATA / "manual_events.csv"
    if not path.exists():
        return out
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                s = dt.date.fromisoformat(row["date_start"].strip())
                e = dt.date.fromisoformat((row.get("date_end") or row["date_start"]).strip())
                w = float(row.get("weight") or 3)
            except Exception as err:  # noqa
                log(f"  manual: пропускаю строку {row}: {err}")
                continue
            out.append(ev(row["title"], s, e, row.get("venue", ""), w, row.get("category") or "событие",
                          "manual", detail=row.get("note", "")))
    return out


SOURCES = {
    "crocus_expo": src_crocus_expo,
    "kudago": src_kudago,
    "holidays": src_holidays,
    "manual": src_manual,
}


# ---------------------------------------------------------------- scoring

def day_contributions(e):
    """Как событие распределяет спрос по ночам.
    Выставки/форумы: участники заезжают накануне (70%), живут все дни (100%),
    в последний день в основном выезжают (30%). Концерты/праздники — только сами дни."""
    s, en, w = dt.date.fromisoformat(e["start"]), dt.date.fromisoformat(e["end"]), e["weight"]
    res = {}
    if e.get("per_day") or e["category"] in ("концерт/шоу", "событие") and s == en:
        d = s
        while d <= en:
            res[d] = w
            d += dt.timedelta(days=1)
        return res
    res[s - dt.timedelta(days=1)] = w * 0.7
    d = s
    while d < en:
        res[d] = w
        d += dt.timedelta(days=1)
    res[en] = res.get(en, 0) + w * 0.3 if en != s else w
    return res


def main():
    log(f"Сборка {TODAY} {'(offline)' if OFFLINE else ''}")
    all_events, status = [], {}
    for name, fn in SOURCES.items():
        if not CONFIG["sources"].get(name, {}).get("enabled", True):
            continue
        cache = DATA / f"cache_{name}.json"
        try:
            items = fn()
            if name in ("crocus_expo", "kudago"):
                cache.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
            status[name] = {"ok": True, "count": len(items)}
            log(f"  {name}: {len(items)} событий")
        except Exception as e:  # noqa
            items = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else []
            status[name] = {"ok": False, "count": len(items), "error": str(e)[:200], "from_cache": cache.exists()}
            log(f"  {name}: ОШИБКА {e} — из кэша {len(items)}")
        all_events += items

    # убираем прошедшие и дубли
    horizon = TODAY + dt.timedelta(days=CONFIG["horizon_days"])
    seen, events = set(), []
    for e in all_events:
        if dt.date.fromisoformat(e["end"]) < TODAY or dt.date.fromisoformat(e["start"]) > horizon:
            continue
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        events.append(e)
    events.sort(key=lambda e: (e["start"], -e["weight"]))

    days = {}
    for e in events:
        for d, w in day_contributions(e).items():
            if d < TODAY or d > horizon:
                continue
            k = d.isoformat()
            slot = days.setdefault(k, {"score": 0.0, "events": []})
            slot["score"] += w
            if not e.get("hidden"):
                slot["events"].append(e["id"])
    lv = CONFIG["levels"]
    for k, v in days.items():
        v["score"] = round(v["score"], 1)
        v["level"] = "high" if v["score"] >= lv["high"] else "medium" if v["score"] >= lv["medium"] else "low"

    out = {
        "generated_at": dt.datetime.now().isoformat(timespec="minutes"),
        "hotel": CONFIG["hotel"],
        "levels": lv,
        "sources": status,
        "events": [e for e in events if not e.get("hidden")],
        "days": dict(sorted(days.items())),
    }
    (DATA / "events.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"Готово: событий {len(out['events'])}, дней с баллами {len(days)}")


if __name__ == "__main__":
    main()
