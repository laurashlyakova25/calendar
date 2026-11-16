#!/usr/bin/env python3
"""
Сборщик календаря спроса для отеля.

Запуск:  python build.py            (обычный режим, ходит в интернет)
         python build.py --offline  (без интернета, только кэш и ручные события)

Что делает:
  1. Собирает события из афиш выставочных площадок, Concertinfo,
     производственного календаря и необязательного manual_events.csv.
  2. Если источник не ответил, берёт его прошлые данные из cache_<источник>.json,
     чтобы календарь не «пустел» из-за сбоя одного сайта.
  3. Считает баллы спроса по дням и пишет events.json, который читает index.html.

Для чтения официального PDF-календаря Экспоцентра используется pypdf из requirements.txt.
"""
import csv
import concurrent.futures
import datetime as dt
import hashlib
import html
import io
import json
import math
import re
import sys
import urllib.parse
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


def fetch_bytes(url, timeout=30):
    if OFFLINE:
        raise RuntimeError("offline mode")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru,en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


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
    На странице у каждой выставки: Название / Подзаголовок / «21 Января 2026 — 23 Января 2026» / ... / Павильон 3 Залы 13 / 14 / ... / Сайт: www.site.ru
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
        halls, site = 1, ""
        block = []
        for nxt in lines[i + 1:i + 30]:
            if DATE_RANGE_RE.search(nxt):
                break
            block.append(nxt)
        for j, nxt in enumerate(block):
            if halls == 1 and re.search(r"(?i)(pavilion|павильон)", nxt):
                halls = max(1, len(re.findall(r"\d+", re.sub(r"(?i)(pavilions?|павильоны?)\s*[\d/ ]+", "", nxt))) or 1)
            sm = re.match(r"(?i)\s*(сайт|website)\s*:?\s*(\S*)", nxt)
            if sm and not site:
                cand = sm.group(2) or (block[j + 1] if j + 1 < len(block) else "")
                if re.match(r"^(https?://)?[\w.-]+\.[a-zа-я]{2,}", cand, re.I):
                    site = cand if cand.startswith("http") else "https://" + cand
        # Влияние: малая выставка (1 зал) = 2, огромная (6+ залов) = 7
        weight = min(7, 1 + halls)
        out.append(ev(title, start, end, "Крокус Экспо", weight, "выставка", "crocus_expo",
                      url=site or "https://www.crocus-expo.ru/exhibition/",
                      detail=f"{subtitle} · залов: {halls}".strip(" ·")))
    return out


def src_crocus_expo():
    months = []
    d = TODAY.replace(day=1)
    for _ in range(14):
        months.append((d.year, d.month))
        d = (d + dt.timedelta(days=32)).replace(day=1)
    def load_month(item):
        y, m = item
        url = f"https://www.crocus-expo.ru/exhibition/?year={y}&month={m:02d}"
        try:
            return parse_crocus_lines(html_to_lines(fetch(url, timeout=12))), None
        except Exception as e:  # noqa
            return [], f"crocus {y}-{m:02d}: {e}"
    events, ok = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for items, error in pool.map(load_month, months):
            events += items
            if error:
                log(f"  {error}")
            else:
                ok += 1
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


CONCERTINFO_ALT_RE = re.compile(r'alt="([^"]{2,200}?),\s*(\d{2})\.(\d{2})\.(\d{4})"')
CONCERTINFO_GIG_RE = re.compile(r'href="(https://concertinfo\.ru/gig/\d+/?)"')


def parse_concertinfo(page, venue_name):
    """Страница площадки на concertinfo.ru. У обложки каждого концерта alt="Название, ДД.ММ.ГГГГ"."""
    out, seen = [], set()
    for m in CONCERTINFO_ALT_RE.finditer(page):
        title = html.unescape(m.group(1)).strip()
        d = dt.date(int(m.group(4)), int(m.group(3)), int(m.group(2)))
        if d < TODAY or (title.lower(), d) in seen:
            continue
        seen.add((title.lower(), d))
        links = CONCERTINFO_GIG_RE.findall(page[max(0, m.start() - 2000):m.start()])
        venue = find_venue(venue_name) or {"weight": 2}
        out.append(ev(title, d, d, venue_name, venue["weight"], "концерт/шоу", "concertinfo",
                      url=links[-1] if links else "", detail=venue_name))
    return out


def src_concertinfo():
    events, ok = [], 0
    for pid, venue_name in CONFIG.get("concertinfo_places", {}).items():
        try:
            events += parse_concertinfo(fetch(f"https://concertinfo.ru/place/{pid}/"), venue_name)
            ok += 1
        except Exception as e:  # noqa
            log(f"  concertinfo {venue_name}: {e}")
    if ok == 0:
        raise RuntimeError("concertinfo.ru не ответил")
    return events


def src_timepad():
    """Собирает публичные события TimePad в Москве на настроенных крупных площадках."""
    horizon = TODAY + dt.timedelta(days=CONFIG["horizon_days"])
    out, skip, total = [], 0, None
    page_size, pages = 100, 0
    while pages < 10 and (total is None or skip < total):
        params = {
            "limit": page_size,
            "skip": skip,
            "cities": "Москва",
            "starts_at_min": TODAY.isoformat() + "T00:00:00",
            "starts_at_max": horizon.isoformat() + "T23:59:59",
            "fields": "location,categories",
            "sort": "+starts_at",
        }
        url = "https://api.timepad.ru/v1/events.json?" + urllib.parse.urlencode(params)
        data = json.loads(fetch(url))
        if not isinstance(data, dict) or not isinstance(data.get("values"), list):
            raise RuntimeError("TimePad вернул неожиданный формат списка событий")
        values = data.get("values") or []
        if total is None:
            total = int(data.get("total") or 0)
        for item in values:
            location = item.get("location") or {}
            address = str(location.get("address") or "")
            venue = find_venue(address)
            if not venue:
                continue
            try:
                start = dt.date.fromisoformat(str(item["starts_at"])[:10])
                end = dt.date.fromisoformat(str(item.get("ends_at") or item["starts_at"])[:10])
            except (KeyError, TypeError, ValueError):
                continue
            if end < start:
                end = start
            if end < TODAY or start > horizon:
                continue
            raw_categories = item.get("categories") or []
            if not isinstance(raw_categories, list):
                raw_categories = []
            categories = " ".join(c.get("name", "") for c in raw_categories if isinstance(c, dict))
            if re.search(r"концерт|музык|шоу|спектак|матч|спорт", categories, re.I):
                category = "концерт/шоу"
            elif re.search(r"форум|конференц|выстав", categories, re.I):
                category = "форум"
            else:
                category = "событие"
            out.append(ev(
                item.get("name") or "Событие TimePad", start, end, venue["name"], venue["weight"],
                category, "timepad", url=item.get("url", ""), detail=address,
            ))
        pages += 1
        if not values:
            break
        skip += len(values)
    log(f"  timepad: просмотрено страниц {pages}, найдено событий на крупных площадках {len(out)}")
    return out


RU_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:[–—-]\s*(\d{1,2}))?\s+"
    r"(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
    r"(?:\s*[–—-]\s*(\d{1,2})\s+"
    r"(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря))?"
    r"\s+(20\d{2})\b", re.I)


def parse_ru_date(text):
    """Читает диапазоны дат афиш вида «26—29 января 2027» и «29 сентября — 3 октября 2027»."""
    m = RU_DATE_RE.search(text.replace("ё", "е").lower())
    if not m:
        return None
    d1, d2, month1, d3, month2, year = m.groups()
    month1 = MONTHS[month1]
    start = dt.date(int(year), month1, int(d1))
    end = (dt.date(int(year), MONTHS[month2], int(d3)) if d3 else
           dt.date(int(year), month1, int(d2 or d1)))
    return start, end


def clean_event_title(value):
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n—–-•|·")
    return re.sub(r"\s*[|—–-]\s*(ВДНХ ЭКСПО|ВК\s*[«\"]?Тимирязев Центр).*?$", "", value, flags=re.I).strip()


def previous_event_title(lines, index):
    """Берёт ближайшее осмысленное название, пропуская служебный мусор в HTML-разметке."""
    for candidate in reversed(lines[max(0, index - 5):index]):
        candidate = clean_event_title(candidate)
        if (not candidate or len(candidate) > 160 or parse_ru_date(candidate) or
                re.search(r"(?i)value_[a-z_]+|=>|\[.*_SORT.*\]|^\[", candidate) or
                candidate.casefold() in {"календарь", "мероприятия", "контакты", "главная"}):
            continue
        return candidate
    return ""


def parse_vdnh_listing(page, page_url):
    """Разбирает карточки событий ВДНХ ЭКСПО из текстового списка."""
    out = []
    venue = find_venue("ВДНХ ЭКСПО")
    for line in html_to_lines(page):
        if "открыть страницу события" not in line.casefold():
            continue
        dates = parse_ru_date(line)
        if not dates:
            continue
        start, end = dates
        if end < TODAY or start > TODAY + dt.timedelta(days=CONFIG["horizon_days"]):
            continue
        tail = line[RU_DATE_RE.search(line.replace("ё", "е").lower()).end():]
        title = re.split(r"ВДНХ\s+ЭКСПО", tail, maxsplit=1, flags=re.I)[0]
        title = clean_event_title(re.sub(r"(?i)открыть страницу события.*$", "", title))
        if not title or len(title) > 160:
            continue
        out.append(ev(title, start, end, venue["name"], venue["weight"], "выставка/форум",
                      "vdnh_expo", url=page_url, detail="ВДНХ ЭКСПО · каталог деловых мероприятий Raido"))
    return out


def src_vdnh_raido():
    """Резервный список ВДНХ ЭКСПО из независимого каталога, сверяющего данные с площадками."""
    out = []
    pages = ["https://go.raido.moscow/vdnh-ekspo/"]
    pages += [f"https://go.raido.moscow/events/vystavki-{year}/"
              for year in range(TODAY.year, (TODAY + dt.timedelta(days=CONFIG["horizon_days"])).year + 1)]
    for url in pages:
        try:
            out.extend(parse_vdnh_listing(fetch(url, timeout=12), url))
        except Exception as e:  # noqa
            log(f"  резервный каталог ВДНХ {url}: {e}")
    if not out:
        raise RuntimeError("официальный сайт и резервный каталог ВДНХ ЭКСПО недоступны")
    return out


def src_vdnh_expo():
    """Собирает события ВДНХ ЭКСПО из официального списка и карточек мероприятий."""
    root = "https://expo.vdnh.ru"
    try:
        page = fetch(root + "/events", timeout=15)
    except Exception as e:  # noqa
        log(f"  официальный календарь ВДНХ ЭКСПО: {e}; пробую резервный каталог")
        return src_vdnh_raido()
    urls = list(dict.fromkeys(root + path for path in
                              re.findall(r"(?:https?://expo\.vdnh\.ru)?(/events/\d+)(?:[/\"?#]|$)", page)))
    def parse_card(url):
        try:
            card = fetch(url, timeout=8)
            h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", card)
            if not h1:
                h1 = re.search(r'(?is)<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', card)
            title = clean_event_title(h1.group(1)) if h1 else ""
            dates = next((d for line in html_to_lines(card) if (d := parse_ru_date(line))), None)
            if not title or not dates:
                return None
            start, end = dates
            if end < TODAY or start > TODAY + dt.timedelta(days=CONFIG["horizon_days"]):
                return None
            venue = find_venue("ВДНХ ЭКСПО")
            if not venue:
                raise RuntimeError("площадка ВДНХ не настроена в config.json")
            return ev(title, start, end, venue["name"], venue["weight"], "выставка/форум",
                      "vdnh_expo", url=url, detail="ВДНХ ЭКСПО")
        except Exception as e:  # noqa
            return (url, str(e))

    out, failures = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for result in pool.map(parse_card, urls[:80]):
            if isinstance(result, tuple):
                failures += 1
                log(f"  ВДНХ ЭКСПО карточка {result[0]}: {result[1]}")
            elif result:
                out.append(result)
    if not urls or not out:
        log(f"  официальный список ВДНХ ЭКСПО не дал событий (ссылок: {len(urls)}, ошибок: {failures}); пробую резервный каталог")
        return src_vdnh_raido()
    log(f"  vdnh_expo: просмотрено карточек {min(len(urls), 80)}, найдено предстоящих событий {len(out)}")
    return out


def src_timiryazev():
    """Собирает события из официального календаря Тимирязев Центра."""
    page = fetch("https://texpo.ru/expo/", timeout=15)
    venue = find_venue("Тимирязев Центр")
    anchors = []
    for match in re.finditer(r"(?is)<a\b([^>]*)>(.*?)</a>", page):
        title = clean_event_title(" ".join(html_to_lines(match.group(2))))
        if (not title or len(title) > 150 or
                re.search(r"(?i)value_[a-z_]+|=>|\[.*_SORT.*\]|^\[", title) or
                re.search(r"(?i)получить|билет|купить|подробнее|перейти|читать далее|календарь|мероприятия|контакты", title)):
            continue
        anchors.append((match, title))
    out = []
    for i, (anchor, title) in enumerate(anchors):
        next_anchor = anchors[i + 1][0].start() if i + 1 < len(anchors) else len(page)
        block = " ".join(html_to_lines(anchor.group(2)) + html_to_lines(page[anchor.end():next_anchor]))
        dates = parse_ru_date(block)
        if not dates:
            continue
        start, end = dates
        if end < TODAY or start > TODAY + dt.timedelta(days=CONFIG["horizon_days"]):
            continue
        title = clean_event_title(RU_DATE_RE.sub("", title, count=1))
        if not title:
            continue
        href = re.search(r"(?i)\bhref\s*=\s*['\"]([^'\"]+)", anchor.group(1))
        event_url = urllib.parse.urljoin("https://texpo.ru/expo/", href.group(1)) if href else "https://texpo.ru/expo/"
        out.append(ev(title, start, end, venue["name"], venue["weight"], "выставка/форум",
                      "timiryazev", url=event_url, detail="Тимирязев Центр"))
    if not out:
        raise RuntimeError("в календаре Тимирязев Центра не найдены будущие события")
    return out


PDF_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:\.\s*(\d{1,2}))?\s*[–—-]\s*"
    r"(\d{1,2})\s*\.\s*(\d{1,2})(?!\d)|(?<!\d)(\d{1,2})\s*\.\s*(\d{1,2})(?!\d)"
)


def parse_expo_pdf_date(match, year):
    if match.group(1):
        day1, month1, day2, month2 = match.group(1, 2, 3, 4)
        month1 = int(month1 or month2)
        return dt.date(year, month1, int(day1)), dt.date(year, int(month2), int(day2))
    day, month = match.group(5, 6)
    value = dt.date(year, int(month), int(day))
    return value, value


def src_expocentr():
    """Читает официальные PDF-календари выставок Экспоцентра с названиями площадок."""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("не установлен pypdf для чтения календаря PDF") from e
    out, years_ok = [], 0
    horizon_year = (TODAY + dt.timedelta(days=CONFIG["horizon_days"])).year
    for year in range(TODAY.year, horizon_year + 1):
        url = (f"https://docs.expocentr.ru/common/img/uploaded/docs/{year}/rus/"
               f"Expocentre_Calendar_{year}_rus.pdf")
        try:
            reader = PdfReader(io.BytesIO(fetch_bytes(url, timeout=15)))
            raw = "\n".join(page.extract_text(extraction_mode="layout") or "" for page in reader.pages)
            if not raw.strip():
                raise RuntimeError("в PDF нет извлекаемого текста")
            years_ok += 1
        except Exception as e:  # noqa
            log(f"  Экспоцентр {year}: {e}")
            continue
        for line in raw.splitlines():
            match = PDF_DATE_RE.search(line)
            if not match:
                continue
            start, end = parse_expo_pdf_date(match, year)
            if end < TODAY or start > TODAY + dt.timedelta(days=CONFIG["horizon_days"]):
                continue
            tail = line[match.end():].strip(" \t|·")
            venue = find_venue(tail)
            if not venue:
                continue  # Не приписываем событие площадке, если PDF не указал место.
            title = re.split(r"(?i)\s+(?:МВЦ|ВК)\s*[«\"]", tail, maxsplit=1)[0].strip(" \t—–-|")
            title = clean_event_title(title)
            if not title or len(title) > 180:
                continue
            out.append(ev(title, start, end, venue["name"], venue["weight"], "выставка/форум",
                          "expocentr", url=url, detail=f"Календарь АО «Экспоцентр» · {venue['name']}"))
    if not years_ok:
        raise RuntimeError("не удалось скачать официальные календари Экспоцентра")
    return out


FIXED_HOLIDAYS = {(1, d) for d in range(1, 9)} | {(2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)}


HOLIDAY_FALLBACK_YEARS = []


def apply_overrides(off, year):
    """Ручные поправки из config.json: перенесённые выходные и рабочие субботы."""
    for x in CONFIG.get("extra_days_off", []):
        d = dt.date.fromisoformat(x)
        if d.year == year:
            off.add(d)
    for x in CONFIG.get("extra_workdays", []):
        off.discard(dt.date.fromisoformat(x))
    return off


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
        return apply_overrides(off, year), True
    except Exception as e:  # noqa
        log(f"  производственный календарь {year}: {e} — использую базовый")
        off = set()
        d = dt.date(year, 1, 1)
        while d.year == year:
            if d.weekday() >= 5 or (d.month, d.day) in FIXED_HOLIDAYS:
                off.add(d)
            d += dt.timedelta(days=1)
        HOLIDAY_FALLBACK_YEARS.append(year)
        return apply_overrides(off, year), False


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
            if length >= 3 and (a == d or d == TODAY):
                out.append({**ev(f"Длинные выходные ({length} дн.)", max(a, TODAY), b, "", w["long_weekend_day"],
                                 "выходные", "holidays"), "per_day": True})
            if holiday:
                out.append({**ev("Государственный праздник", d, d, "", w["holiday"], "праздник", "holidays"),
                            "per_day": True})
            elif length < 3 and d.weekday() >= 5:
                out.append({**ev("Выходной", d, d, "", w["weekend"], "выходные", "holidays"),
                            "per_day": True, "hidden": True})
        d += dt.timedelta(days=1)
    # Особые даты (каждый год), например новогодняя ночь — config.json → special_days
    for y in range(TODAY.year, end.year + 1):
        for sd in CONFIG.get("special_days", []):
            mth, day = map(int, sd["date"].split("-"))
            d = dt.date(y, mth, day)
            if TODAY <= d <= end:
                out.append({**ev(sd["title"], d, d, "", sd["weight"], "праздник", "holidays"), "per_day": True})
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
                          "manual", url=(row.get("url") or "").strip(), detail=row.get("note") or ""))
    return out


SOURCES = {
    "crocus_expo": src_crocus_expo,
    "concertinfo": src_concertinfo,
    "timepad": src_timepad,
    "vdnh_expo": src_vdnh_expo,
    "timiryazev": src_timiryazev,
    "expocentr": src_expocentr,
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
            if name in ("crocus_expo", "kudago", "concertinfo", "timepad", "vdnh_expo", "timiryazev", "expocentr"):
                cache.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
            status[name] = {"ok": True, "count": len(items)}
            if name == "holidays" and HOLIDAY_FALLBACK_YEARS:
                status[name]["note"] = "официальный календарь недоступен, взяты базовые праздники и поправки из config.json"
            log(f"  {name}: {len(items)} событий")
        except Exception as e:  # noqa
            items = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else []
            status[name] = {"ok": False, "count": len(items), "error": str(e)[:200], "from_cache": cache.exists()}
            log(f"  {name}: ОШИБКА {e} — из кэша {len(items)}")
        all_events += items

    # убираем прошедшие и дубли
    horizon = TODAY + dt.timedelta(days=CONFIG["horizon_days"])
    # если событие на ту же дату и площадку есть в ручном файле, автоматическое не считаем второй раз
    manual_keys = {(e["start"], e["venue"]) for e in all_events if e["source"] == "manual" and e["venue"]}
    all_events = [e for e in all_events
                  if not (e["source"] in ("concertinfo", "kudago") and (e["start"], e["venue"]) in manual_keys)]
    other_event_keys = {
        (re.sub(r"[^0-9a-zа-яё]+", "", e["title"].casefold()), e["start"], e["venue"])
        for e in all_events if e["source"] != "timepad"
    }
    all_events = [e for e in all_events if e["source"] != "timepad" or
                  (re.sub(r"[^0-9a-zа-яё]+", "", e["title"].casefold()), e["start"], e["venue"]) not in other_event_keys]
    seen, events = set(), []
    seen_event_keys = set()
    for e in all_events:
        if dt.date.fromisoformat(e["end"]) < TODAY or dt.date.fromisoformat(e["start"]) > horizon:
            continue
        title_for_key = re.split(r"[\(\[]", e["title"].casefold(), maxsplit=1)[0]
        normalized_title = re.sub(r"[^0-9a-zа-яё]+", "", re.sub(r"\b20\d{2}\b", "", title_for_key))
        event_key = (normalized_title, e["start"], e["venue"].casefold())
        if e["id"] in seen or event_key in seen_event_keys:
            continue
        seen.add(e["id"])
        seen_event_keys.add(event_key)
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
