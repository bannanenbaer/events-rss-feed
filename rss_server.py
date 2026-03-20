from flask import Flask, Response
import requests
from bs4 import BeautifulSoup
import re
from datetime import date, datetime, timedelta
from dataclasses import dataclass
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
    "markt", "konzert", "festival", "sport", "fest", "musik", "theater",
    "wanderung", "flohmarkt", "kino", "volksfest", "stadtfest", "jahrmarkt",
    "lesung", "kabarett", "comedy", "zirkus", "weihnachtsmarkt", "ostermarkt",
    "sommerfest", "herbstfest", "open air", "openair", "live", "party",
]

BORING_KW = [
    "ausstellung", "galerie", "vernissage", "kunstausstellung",
    "skulptur", "malerei", "bildende kunst", "fotoausstellung",
    "kunstwerk", "atelier", "grafik", "zeichnung",
]


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
    """Regelbasiertes KI-Scoring – kein externer API-Aufruf nötig."""
    title_lower = event.title.lower()

    # Kunstausstellungen sofort herausfiltern
    if any(kw in title_lower for kw in BORING_KW):
        return 1, "Kunstausstellung – nicht empfohlen"

    score = 5
    reasons: list[str] = []

    # Lokaler Bonus
    if event.source == "Wennigsen":
        score += 4
        reasons.append("Lokal in Wennigsen")
    elif event.source == "Deister":
        score += 2
        reasons.append("Deister-Region")

    # Wochenend-Bonus
    if event.is_weekend:
        score += 3
        reasons.append("Wochenendveranstaltung")

    # Kategorie-Bonus
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

        # Fallback: Regex wenn BeautifulSoup nichts findet
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
    ("Sport",          "https://www.hannover.de/Veranstaltungskalender/Sport"),
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
# Aggregation & Cache
# ---------------------------------------------------------------------------

def get_events() -> list[Event]:
    now = time.time()
    if _cache['data'] is not None and (now - _cache['timestamp']) < CACHE_TTL:
        return _cache['data']  # type: ignore[return-value]

    all_events: list[Event] = []
    all_events += scrape_wennigsen()
    all_events += scrape_deister()
    all_events += scrape_hannover()
    all_events += scrape_visit_hannover()

    # Filtern und sortieren
    filtered = [e for e in all_events if e.score >= 3]
    filtered.sort(key=lambda e: (
        -e.score,
        -(1 if e.is_weekend else 0),
        e.date_obj or date(9999, 1, 1),
    ))

    # Duplikate entfernen (gleicher Titel)
    seen: set[str] = set()
    deduped: list[Event] = []
    for e in filtered:
        key = re.sub(r'\s+', ' ', e.title.lower().strip())
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    _cache['data'] = deduped
    _cache['timestamp'] = now
    return deduped


# ---------------------------------------------------------------------------
# HTML-Seite
# ---------------------------------------------------------------------------

SOURCE_COLORS = {
    "Wennigsen":      "#2e7d32",
    "Deister":        "#6a4c93",
    "Hannover":       "#1565c0",
    "Visit Hannover": "#0277bd",
}


def _event_card(e: Event) -> str:
    color = SOURCE_COLORS.get(e.source, "#555")
    weekend_bg = " background:#fffde7;" if e.is_weekend else ""
    stars = "★" * min(e.score // 2, 5)
    time_part = f"  ·  {escape(e.time_str)}" if e.time_str else ""
    return f"""
    <div style="border:1px solid #ddd;border-radius:8px;padding:14px 16px;margin:8px 0;{weekend_bg}">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px;">
        <span style="background:{color};color:#fff;border-radius:4px;padding:2px 8px;font-size:12px;font-weight:600;">{escape(e.source)}</span>
        <span style="color:#555;font-size:13px;">📅 {escape(_format_date(e))}{time_part}</span>
        {"<span style='font-size:11px;color:#f9a825;'>" + stars + "</span>" if stars else ""}
      </div>
      <a href="{escape(e.url)}" style="font-size:16px;font-weight:700;color:#1a1a1a;text-decoration:none;" target="_blank">{escape(e.title)}</a>
      {"<div style='margin-top:4px;font-size:13px;color:#555;'>📍 " + escape(e.location) + "</div>" if e.location else ""}
      {"<div style='margin-top:4px;font-size:12px;color:#999;font-style:italic;'>" + escape(e.reason) + "</div>" if e.reason else ""}
    </div>"""


def _section(heading: str, icon: str, items: list[Event]) -> str:
    if not items:
        return ""
    cards = "\n".join(_event_card(e) for e in items)
    return f"""
  <h2 style="margin-top:28px;color:#333;border-bottom:2px solid #eee;padding-bottom:6px;">{icon} {heading}</h2>
  {cards}"""


def render_html(events: list[Event]) -> str:
    wennigsen = [e for e in events if e.source == "Wennigsen"]
    weekend   = [e for e in events if e.is_weekend and e.source != "Wennigsen"]
    rest      = [e for e in events if not e.is_weekend and e.source != "Wennigsen"]
    total     = len(events)
    now_str   = datetime.now().strftime("%d.%m.%Y %H:%M")

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
  {_section("Wennigsen", "⭐", wennigsen)}
  {_section("Wochenende", "🎉", weekend)}
  {_section("Weitere Veranstaltungen", "📍", rest)}
  {"<p style='text-align:center;color:#aaa;margin-top:40px;'>Keine Veranstaltungen gefunden.</p>" if not events else ""}
</body>
</html>"""


# ---------------------------------------------------------------------------
# RSS-Feed
# ---------------------------------------------------------------------------

def generate_rss(events: list[Event]) -> str:
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Veranstaltungen – Wennigsen & Region"
    ET.SubElement(ch, "link").text = "https://www.wennigsen.de"
    ET.SubElement(ch, "description").text = "Events aus Wennigsen, Deister und Hannover – smart gefiltert"
    ET.SubElement(ch, "language").text = "de-de"
    ET.SubElement(ch, "lastBuildDate").text = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    if not events:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = "Keine Veranstaltungen gefunden"
        ET.SubElement(item, "description").text = "Aktuell sind keine Veranstaltungen verfügbar."
    else:
        for e in events:
            item = ET.SubElement(ch, "item")
            score_prefix = f"[{e.score}/10] " if e.score >= 7 else ""
            ET.SubElement(item, "title").text = f"{score_prefix}{e.title}"
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
    return Response(render_html(get_events()), mimetype="text/html")


@app.route("/feed")
@app.route("/feed.rss")
def rss_feed():
    return Response(generate_rss(get_events()), mimetype="application/rss+xml")


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
