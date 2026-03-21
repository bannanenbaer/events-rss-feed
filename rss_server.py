from flask import Flask, Response
import requests
from bs4 import BeautifulSoup
import re
from datetime import date, datetime, timedelta
from dataclasses import dataclass, field
import xml.etree.ElementTree as ET
from xml.dom import minidom
import time
from html import escape

app = Flask(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

CACHE_TTL = 3600  # 1 Stunde
_cache: dict = {'data': None, 'timestamp': 0.0}

GERMAN_MONTHS = {
    'januar': 1, 'februar': 2, 'märz': 3, 'april': 4,
    'mai': 5, 'juni': 6, 'juli': 7, 'august': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'dezember': 12,
    'jan': 1, 'feb': 2, 'mär': 3, 'apr': 4,
    'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'dez': 12,
}

INTERESTING_KW = [
    "markt", "konzert", "festival", "fest", "musik", "theater",
    "wanderung", "flohmarkt", "kino", "volksfest", "stadtfest", "jahrmarkt",
    "lesung", "kabarett", "comedy", "zirkus", "weihnachtsmarkt", "ostermarkt",
    "sommerfest", "herbstfest", "open air", "openair", "live", "party",
]

BORING_KW = [
    "ausstellung", "galerie", "vernissage", "kunstausstellung",
    "skulptur", "malerei", "bildende kunst", "fotoausstellung",
    "kunstwerk", "atelier", "grafik", "zeichnung",
    "sport", "fußball", "handball", "turnier", "wettkampf", "triathlon",
    "marathon", "lauf", "schwimmen", "tennis", "volleyball",
]

# Entfernung von Wennigsen (für Sortierung)
SOURCE_DISTANCE = {
    "Wennigsen": 0,
    "Deister": 1,
    "Hannover": 2,
    "Visit Hannover": 3,
}


@dataclass
class Event:
    title: str
    date_str: str
    date_obj: date | None
    time_str: str
    location: str
    url: str
    source: str  # "Wennigsen" | "Deister" | "Hannover" | "Visit Hannover"
    is_weekend: bool = False
    score: int = 5
    reason: str = ""

    def __post_init__(self) -> None:
        self.is_weekend = self.date_obj is not None and self.date_obj.weekday() >= 5
        self.score, self.reason = _score(self)


@dataclass
class MuseumInfo:
    status: str          # "offen" | "geschlossen" | "nicht erreichbar"
    detail: str          # z.B. "So 14–17 Uhr" oder Fehlermeldung
    events: list[Event] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def parse_german_date(text: str) -> date | None:
    """Parst verschiedene deutsche Datumsformate."""
    text = text.strip()

    # "22. März 2026"
    m = re.search(r'(\d{1,2})\.\s*(\w+)\s+(\d{4})', text)
    if m:
        day, month_str, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        month = GERMAN_MONTHS.get(month_str)
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                pass

    # "21.03.2026" oder "21.03.26"
    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{2,4})', text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            pass

    # "Do., 20.03." – kein Jahr, nehme aktuelles Jahr
    m = re.search(r'(\d{1,2})\.(\d{2})\.$', text.rstrip())
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = date.today().year
        try:
            d = date(year, month, day)
            if d < date.today() - timedelta(days=30):
                d = date(year + 1, month, day)
            return d
        except ValueError:
            pass

    return None


def _score(event: Event) -> tuple[int, str]:
    """Regelbasiertes Scoring."""
    title_lower = event.title.lower()

    if any(kw in title_lower for kw in BORING_KW):
        return 1, "Nicht empfohlen"

    score = 5
    reasons: list[str] = []

    if event.source == "Wennigsen":
        score += 4
        reasons.append("Lokal in Wennigsen")
    elif event.source == "Deister":
        score += 2
        reasons.append("Deister-Region")

    if event.is_weekend:
        score += 3
        reasons.append("Wochenendveranstaltung")

    if any(kw in title_lower for kw in INTERESTING_KW):
        score += 2
        reasons.append("Empfohlene Kategorie")

    return min(10, score), " · ".join(reasons) if reasons else "Allgemeine Veranstaltung"


def _day_label(d: date) -> str:
    names = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    return f"{names[d.weekday()]}, {d.day:02d}.{d.month:02d}.{d.year}"


def _format_date(event: Event) -> str:
    if event.date_obj:
        return _day_label(event.date_obj)
    return event.date_str or "Datum unbekannt"


def _sort_key(e: Event):
    """Primär: Datum (aufsteigend); Sekundär: Entfernung von Wennigsen."""
    d = e.date_obj or date(9999, 1, 1)
    dist = SOURCE_DISTANCE.get(e.source, 9)
    return (d, dist)


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

def scrape_wennigsen() -> list[Event]:
    url = "https://www.wennigsen.de/regional/veranstaltungen/sucheplus.html"
    events: list[Event] = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')

        for item in soup.select('span.manager_titel'):
            link = item.find('a')
            if not link:
                continue
            title = link.get_text(strip=True)
            if not title or title == "zuklappen / aufklappen":
                continue

            date_span = item.find_next_sibling('span', class_='manager_untertitel')
            date_text = date_span.get_text(strip=True).replace('\xa0', ' ') if date_span else ""

            href = link.get('href', '')
            event_url = f"https://www.wennigsen.de{href}" if href.startswith('/') else (href or url)

            events.append(Event(
                title=title, date_str=date_text,
                date_obj=parse_german_date(date_text),
                time_str="", location="Wennigsen",
                url=event_url, source="Wennigsen",
            ))

        if not events:
            pattern = r'title="[^"]*">\s*([^<]+)</a></span><span class="manager_untertitel"[^>]*>([^<]+)'
            for title, date_raw in re.findall(pattern, r.text)[:15]:
                title = title.strip()
                date_raw = date_raw.replace('&nbsp;', ' ').replace(' - ', '-').strip()
                if title and title != "zuklappen / aufklappen":
                    events.append(Event(
                        title=title, date_str=date_raw,
                        date_obj=parse_german_date(date_raw),
                        time_str="", location="Wennigsen",
                        url=url, source="Wennigsen",
                    ))
    except Exception as e:
        print(f"[wennigsen] Fehler: {e}")
    return events


def scrape_deister() -> list[Event]:
    url = "https://www.deister.de/veranstaltungen"
    events: list[Event] = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')

        for link in soup.find_all('a', href=re.compile(r'/veranstaltungen/\d')):
            h5 = link.find('h5')
            if not h5:
                continue
            title = h5.get_text(strip=True)
            if not title:
                continue

            paras = link.find_all('p')
            date_text = paras[0].get_text(strip=True) if paras else ""
            time_text = paras[1].get_text(strip=True) if len(paras) > 1 else ""

            href = link.get('href', '')
            event_url = f"https://www.deister.de{href}" if href.startswith('/') else href

            events.append(Event(
                title=title, date_str=date_text,
                date_obj=parse_german_date(date_text),
                time_str=time_text, location="Deister-Region",
                url=event_url, source="Deister",
            ))
    except Exception as e:
        print(f"[deister] Fehler: {e}")
    return events


HANNOVER_PAGES = [
    ("Konzerte",       "https://www.hannover.de/Veranstaltungskalender/Konzerte"),
    ("Festivals",      "https://www.hannover.de/Veranstaltungskalender/Festivals"),
    ("Märkte",         "https://www.hannover.de/Veranstaltungskalender/Maerkte-Flohmaerkte"),
    ("Theater & Tanz", "https://www.hannover.de/Veranstaltungskalender/Theater-Tanz"),
]


def scrape_hannover() -> list[Event]:
    events: list[Event] = []
    for _cat, url in HANNOVER_PAGES:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, 'html.parser')

            for h4 in soup.find_all('h4'):
                title = h4.get_text(strip=True)
                if not title:
                    continue

                h5 = h4.find_next('h5')
                date_text = h5.get_text(strip=True) if h5 else ""

                p = h4.find_next('p')
                location = p.get_text(strip=True) if p else "Hannover"

                parent_a = h4.find_parent('a') or h4.find_next('a')
                href = parent_a.get('href', '') if parent_a else ''
                event_url = f"https://www.hannover.de{href}" if href.startswith('/') else (href or url)

                events.append(Event(
                    title=title, date_str=date_text,
                    date_obj=parse_german_date(date_text),
                    time_str="", location=location or "Hannover",
                    url=event_url, source="Hannover",
                ))
        except Exception as e:
            print(f"[hannover] Fehler: {e}")
    return events


def scrape_visit_hannover() -> list[Event]:
    url = "https://www.visit-hannover.com/Event-Highlights,-Kultur-Freizeit"
    events: list[Event] = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, 'html.parser')

        for link in soup.find_all('a', href=True):
            h3 = link.find('h3')
            if not h3:
                continue
            title = h3.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            p = link.find('p')
            desc = p.get_text(strip=True) if p else ""

            href = link.get('href', '')
            event_url = f"https://www.visit-hannover.com{href}" if href.startswith('/') else href

            events.append(Event(
                title=title, date_str=desc[:80] if desc else "",
                date_obj=parse_german_date(desc),
                time_str="", location="Hannover",
                url=event_url, source="Visit Hannover",
            ))
    except Exception as e:
        print(f"[visit-hannover] Fehler: {e}")
    return events


# ---------------------------------------------------------------------------
# Heimatmuseum Wennigsen
# ---------------------------------------------------------------------------

MUSEUM_URLS = [
    "http://heimatmuseum-wennigsen.de/",
    "http://heimatmuseum-wennigsen.de/aktuell/",
    "http://heimatmuseum-wennigsen.de/veranstaltungen/",
    "http://www.heimatmuseum-wennigsen.de/",
]

# Bekannte Öffnungszeiten als Fallback (Sonntag 14–17 Uhr, typisch für Heimatmuseen)
MUSEUM_OPENING_HOURS = {
    6: (14, 17),  # Sonntag: 14–17 Uhr  (0=Mo … 6=So)
}
MUSEUM_OPENING_LABEL = "So 14–17 Uhr (lt. Webseite)"


def _museum_open_by_schedule(now: datetime) -> bool:
    """Prüft anhand bekannter Öffnungszeiten ob das Museum jetzt offen ist."""
    hours = MUSEUM_OPENING_HOURS.get(now.weekday())
    if not hours:
        return False
    open_h, close_h = hours
    return open_h <= now.hour < close_h


def _parse_museum_opening(text: str) -> dict[int, tuple[int, int]]:
    """
    Versucht Öffnungszeiten aus Freitext zu parsen.
    Gibt {weekday: (open_hour, close_hour)} zurück.
    """
    day_map = {
        'montag': 0, 'mo': 0,
        'dienstag': 1, 'di': 1,
        'mittwoch': 2, 'mi': 2,
        'donnerstag': 3, 'do': 3,
        'freitag': 4, 'fr': 4,
        'samstag': 5, 'sa': 5,
        'sonntag': 6, 'so': 6,
    }
    result: dict[int, tuple[int, int]] = {}
    text_l = text.lower()
    # Pattern: "Sonntag 14 – 17 Uhr" oder "So. 14:00-17:00 Uhr"
    for m in re.finditer(
        r'(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|mo|di|mi|do|fr|sa|so)\.?\s+'
        r'(\d{1,2})(?::\d{2})?\s*[-–bis]+\s*(\d{1,2})(?::\d{2})?\s*uhr',
        text_l
    ):
        day_str, open_h, close_h = m.group(1), int(m.group(2)), int(m.group(3))
        wd = day_map.get(day_str)
        if wd is not None:
            result[wd] = (open_h, close_h)
    return result


def scrape_heimatmuseum() -> MuseumInfo:
    """Scrapt Status und Veranstaltungen des Heimatmuseums Wennigsen."""
    now = datetime.now()
    html = ""
    fetch_ok = False
    fetch_error = ""

    for url in MUSEUM_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
            # 404-Seiten der alten PHP-Seite erkennen
            if r.status_code == 200 and "FEHLER: Die Seite ist nicht verfügbar" not in r.text:
                html = r.text
                fetch_ok = True
                break
        except Exception as e:
            fetch_error = str(e)

    events: list[Event] = []
    opening_label = MUSEUM_OPENING_LABEL

    if fetch_ok and html:
        soup = BeautifulSoup(html, 'html.parser')
        # Öffnungszeiten aus Seite lesen
        page_text = soup.get_text()
        parsed_hours = _parse_museum_opening(page_text)
        if parsed_hours:
            # Baue lesbare Beschreibung
            day_names = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
            parts = [f"{day_names[d]} {o}–{c} Uhr" for d, (o, c) in sorted(parsed_hours.items())]
            opening_label = ", ".join(parts) + " (lt. Webseite)"
            # Überschreibe Fallback-Zeiten
            active_hours = parsed_hours
        else:
            active_hours = MUSEUM_OPENING_HOURS

        # Events scrapen
        museum_base = "http://heimatmuseum-wennigsen.de"
        for tag in soup.find_all(['h2', 'h3', 'h4', 'li', 'p']):
            text = tag.get_text(strip=True)
            if not text or len(text) < 8:
                continue
            d = parse_german_date(text)
            if d and d >= date.today():
                # Titel: nächsten Textteil holen oder Tag selbst nutzen
                title = text[:120]
                href = ''
                a = tag.find('a') or tag.find_next_sibling('a')
                if a:
                    href = a.get('href', '')
                    if a.get_text(strip=True):
                        title = a.get_text(strip=True)[:120]
                event_url = f"{museum_base}{href}" if href.startswith('/') else (href or museum_base)
                events.append(Event(
                    title=title, date_str=text[:60],
                    date_obj=d, time_str="",
                    location="Heimatmuseum Wennigsen",
                    url=event_url, source="Wennigsen",
                ))
    else:
        active_hours = MUSEUM_OPENING_HOURS

    # Deduplizieren
    seen: set[str] = set()
    deduped_events: list[Event] = []
    for e in events:
        key = re.sub(r'\s+', ' ', e.title.lower().strip())
        if key not in seen:
            seen.add(key)
            deduped_events.append(e)
    deduped_events.sort(key=lambda e: e.date_obj or date(9999, 1, 1))

    # Status bestimmen
    hours = active_hours.get(now.weekday())
    if hours and hours[0] <= now.hour < hours[1]:
        is_open = True
    elif any(e.date_obj == date.today() for e in deduped_events):
        # Event heute → Museum offen
        is_open = True
    else:
        is_open = False

    if not fetch_ok:
        err_short = fetch_error[:60] if fetch_error else "Seite nicht erreichbar"
        return MuseumInfo(
            status="⚠ nicht erreichbar",
            detail=f"Webseite nicht erreichbar ({err_short}). Bekannte Zeiten: {MUSEUM_OPENING_LABEL}",
            events=[],
        )

    return MuseumInfo(
        status="offen" if is_open else "geschlossen",
        detail=opening_label,
        events=deduped_events,
    )


# ---------------------------------------------------------------------------
# Aggregation & Cache
# ---------------------------------------------------------------------------

def get_data() -> tuple[list[Event], MuseumInfo]:
    now = time.time()
    if _cache['data'] is not None and (now - _cache['timestamp']) < CACHE_TTL:
        return _cache['data']  # type: ignore[return-value]

    museum = scrape_heimatmuseum()

    all_events: list[Event] = []
    all_events += scrape_wennigsen()
    all_events += scrape_deister()
    all_events += scrape_hannover()
    all_events += scrape_visit_hannover()

    # Museum-Events aus Hauptliste heraushalten (werden separat angezeigt)
    museum_titles = {re.sub(r'\s+', ' ', e.title.lower().strip()) for e in museum.events}

    filtered = [
        e for e in all_events
        if e.score >= 3
        and re.sub(r'\s+', ' ', e.title.lower().strip()) not in museum_titles
    ]

    # Primär: Datum aufsteigend; Sekundär: Entfernung von Wennigsen
    filtered.sort(key=_sort_key)

    # Duplikate entfernen (gleicher Titel)
    seen: set[str] = set()
    deduped: list[Event] = []
    for e in filtered:
        key = re.sub(r'\s+', ' ', e.title.lower().strip())
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    result = (deduped, museum)
    _cache['data'] = result
    _cache['timestamp'] = now
    return result


# Kompatibilitätsfunktion für RSS
def get_events() -> list[Event]:
    events, _ = get_data()
    return events


# ---------------------------------------------------------------------------
# HTML-Seite
# ---------------------------------------------------------------------------

def _event_row(e: Event) -> str:
    """Kompakte Zeile: Datum  Titel  [Ort]"""
    date_part = escape(_format_date(e))
    time_part = f" {escape(e.time_str)}" if e.time_str else ""
    weekend_mark = " 🎉" if e.is_weekend else ""
    loc = f" <span style='color:#888;font-size:12px;'>📍{escape(e.location)}</span>" if e.location else ""
    return (
        f"<div style='padding:5px 0;border-bottom:1px solid #f0f0f0;'>"
        f"<span style='color:#555;font-size:13px;min-width:160px;display:inline-block;'>"
        f"{date_part}{time_part}{weekend_mark}</span> "
        f"<a href='{escape(e.url)}' style='font-weight:600;color:#1a1a1a;text-decoration:none;' target='_blank'>"
        f"{escape(e.title)}</a>{loc}"
        f"</div>"
    )


def _source_section(label: str, items: list[Event]) -> str:
    if not items:
        return ""
    rows = "\n".join(_event_row(e) for e in items)
    return (
        f"<div style='margin-top:20px;'>"
        f"<div style='font-weight:700;font-size:15px;color:#444;border-bottom:2px solid #ccc;"
        f"padding-bottom:4px;margin-bottom:6px;letter-spacing:.5px;'>— {label} —</div>"
        f"{rows}"
        f"</div>"
    )


def _museum_block(info: MuseumInfo) -> str:
    if info.status == "offen":
        status_color = "#2e7d32"
        icon = "🟢"
    elif info.status == "geschlossen":
        status_color = "#c62828"
        icon = "🔴"
    else:
        status_color = "#e65100"
        icon = "⚠️"

    events_html = ""
    if info.events:
        rows = "\n".join(
            f"<div style='padding:3px 0;font-size:13px;'>"
            f"<span style='color:#666;min-width:150px;display:inline-block;'>{escape(_day_label(e.date_obj) if e.date_obj else e.date_str)}</span> "
            f"<a href='{escape(e.url)}' style='color:#1a1a1a;text-decoration:none;' target='_blank'>{escape(e.title)}</a>"
            f"</div>"
            for e in info.events
        )
        events_html = (
            f"<div style='margin-top:8px;padding-top:8px;border-top:1px solid #e0e0e0;'>"
            f"<div style='font-size:12px;color:#666;margin-bottom:4px;'>Veranstaltungen im Heimatmuseum:</div>"
            f"{rows}"
            f"</div>"
        )
    elif info.status != "⚠ nicht erreichbar":
        events_html = "<div style='margin-top:6px;font-size:13px;color:#888;'>Keine Veranstaltungen eingetragen.</div>"

    detail_html = f"<div style='font-size:12px;color:#888;margin-top:2px;'>{escape(info.detail)}</div>"

    return (
        f"<div style='background:#fff;border:2px solid {status_color};border-radius:8px;"
        f"padding:12px 16px;margin-bottom:16px;'>"
        f"<div style='font-size:16px;font-weight:700;color:{status_color};'>"
        f"{icon} Heimatmuseum Wennigsen "
        f"<span style='font-weight:400;font-size:14px;'>({escape(info.status)})</span>"
        f"</div>"
        f"{detail_html}"
        f"{events_html}"
        f"</div>"
    )


def render_html(events: list[Event], museum: MuseumInfo) -> str:
    wennigsen     = [e for e in events if e.source == "Wennigsen"]
    deister       = [e for e in events if e.source == "Deister"]
    hannover      = [e for e in events if e.source == "Hannover"]
    region        = [e for e in events if e.source == "Visit Hannover"]
    total         = len(events)
    now_str       = datetime.now().strftime("%d.%m.%Y %H:%M")

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Veranstaltungen – Wennigsen &amp; Region</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      max-width: 740px; margin: 0 auto; padding: 16px 12px;
      background: #f5f5f5; color: #1a1a1a;
    }}
    a {{ color: inherit; }}
  </style>
</head>
<body>
  <div style="background:#2e7d32;color:#fff;border-radius:10px;padding:20px 24px;margin-bottom:16px;">
    <h1 style="margin:0 0 6px;font-size:22px;">📅 Veranstaltungen</h1>
    <p style="margin:0;opacity:.9;">Wennigsen · Deister · Hannover — <strong>{total}</strong> Events</p>
    <p style="margin:6px 0 0;font-size:12px;opacity:.7;">
      Aktualisiert: {now_str} &nbsp;·&nbsp;
      <a href="/feed" style="color:#fff;">RSS-Feed</a> &nbsp;·&nbsp;
      <a href="/refresh" style="color:#fff;">Neu laden</a>
    </p>
  </div>

  {_museum_block(museum)}

  {_source_section("Wennigsen", wennigsen)}
  {_source_section("Deister", deister)}
  {_source_section("Hannover", hannover)}
  {_source_section("Region", region)}

  {"<p style='text-align:center;color:#aaa;margin-top:40px;'>Keine Veranstaltungen gefunden.</p>" if not events else ""}
</body>
</html>"""


# ---------------------------------------------------------------------------
# RSS-Feed
# ---------------------------------------------------------------------------

def generate_rss(events: list[Event], museum: MuseumInfo) -> str:
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Veranstaltungen – Wennigsen & Region"
    ET.SubElement(ch, "link").text = "https://www.wennigsen.de"
    ET.SubElement(ch, "description").text = "Events aus Wennigsen, Deister und Hannover"
    ET.SubElement(ch, "language").text = "de-de"
    ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    # Museum-Status als erstes Item
    museum_item = ET.SubElement(ch, "item")
    ET.SubElement(museum_item, "title").text = f"Heimatmuseum Wennigsen ({museum.status})"
    ET.SubElement(museum_item, "link").text = "http://heimatmuseum-wennigsen.de"
    museum_desc = museum.detail
    if museum.events:
        museum_desc += "\n\nVeranstaltungen:\n" + "\n".join(
            f"- {_day_label(e.date_obj) if e.date_obj else e.date_str}: {e.title}"
            for e in museum.events
        )
    ET.SubElement(museum_item, "description").text = museum_desc
    ET.SubElement(museum_item, "category").text = "Heimatmuseum"

    if not events:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = "Keine Veranstaltungen gefunden"
        ET.SubElement(item, "description").text = "Aktuell sind keine Veranstaltungen verfügbar."
    else:
        for e in events:
            item = ET.SubElement(ch, "item")
            ET.SubElement(item, "title").text = e.title
            ET.SubElement(item, "link").text = e.url
            ET.SubElement(item, "category").text = e.source

            time_part = f" um {e.time_str}" if e.time_str else ""
            weekend_note = " 🎉 Wochenende!" if e.is_weekend else ""
            ET.SubElement(item, "description").text = (
                f"{_format_date(e)}{time_part}{weekend_note}\n"
                f"📍 {e.location}\n\n"
                f"{e.reason}"
            )
            if e.date_obj:
                pub = datetime(e.date_obj.year, e.date_obj.month, e.date_obj.day, 12, 0, 0)
                ET.SubElement(item, "pubDate").text = pub.strftime("%a, %d %b %Y %H:%M:%S +0000")

    xml_str = ET.tostring(rss, encoding="unicode")
    dom = minidom.parseString(xml_str)
    return dom.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")


# ---------------------------------------------------------------------------
# Flask-Routen
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    events, museum = get_data()
    return Response(render_html(events, museum), mimetype="text/html")


@app.route("/feed")
@app.route("/feed.rss")
def rss_feed():
    events, museum = get_data()
    return Response(generate_rss(events, museum), mimetype="application/rss+xml")


@app.route("/refresh")
def refresh():
    _cache['data'] = None
    _cache['timestamp'] = 0.0
    return Response(
        "Cache geleert. <a href='/'>Zurück zur Übersicht</a>",
        mimetype="text/html",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
