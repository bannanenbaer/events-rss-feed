from flask import Flask, Response
import requests
import re
from datetime import datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom

app = Flask(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

def fetch_wennigsen_events():
    url = "https://www.wennigsen.de/regional/veranstaltungen/sucheplus.html"
    events = []
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code == 200:
            pattern = r'title="[^"]*">\s*([^<]+)</a></span><span class="manager_untertitel"[^>]*>([^<]+)'
            matches = re.findall(pattern, response.text)
            for title, date in matches[:10]:
                title = title.strip()
                date = date.replace('&nbsp;', ' ').strip()
                if title and title != "zuklappen / aufklappen":
                    events.append({'title': title, 'date': date, 'location': 'Wennigsen'})
    except Exception as e:
        print(f"Error: {e}")
    return events

def generate_rss_feed():
    events = fetch_wennigsen_events()
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    title = ET.SubElement(channel, "title")
    title.text = "Events Wennigsen"
    link = ET.SubElement(channel, "link")
    link.text = "https://www.wennigsen.de"
    desc = ET.SubElement(channel, "description")
    desc.text = "Veranstaltungen in Wennigsen"
    lang = ET.SubElement(channel, "language")
    lang.text = "de-de"
    
    if not events:
        item = ET.SubElement(channel, "item")
        t = ET.SubElement(item, "title")
        t.text = "Keine Veranstaltungen"
    else:
        for e in events:
            item = ET.SubElement(channel, "item")
            t = ET.SubElement(item, "title")
            t.text = e['title']
            d = ET.SubElement(item, "description")
            d.text = f"{e['date']}\nOrt: {e['location']}"
    
    xml_str = ET.tostring(rss, encoding="unicode")
    dom = minidom.parseString(xml_str)
    return dom.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")

@app.route("/")
def index():
    return "<h1>Events RSS</h1><p><a href='/feed'>Feed</a></p>"

@app.route("/feed.rss")
@app.route("/feed")
def rss_feed():
    return Response(generate_rss_feed(), mimetype="application/rss+xml")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
