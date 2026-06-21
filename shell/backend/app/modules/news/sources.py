"""
Default RSS sources shipped with the News module.

Each source is a small dict; weight controls how much an article from this
outlet contributes to the heatmap score:
  - 1.0  major wire / paper of record (AP, Reuters, BBC)
  - 0.7  established broadsheet
  - 0.5  topical / niche but reputable
  - 0.3  blog / aggregator

You can add/override via /api/news/sources later. v1 ships read-only.
"""
from dataclasses import dataclass


@dataclass
class NewsSource:
    id:     str
    name:   str
    url:    str
    weight: float = 0.7
    topic:  str   = "world"      # rough category bucket


DEFAULT_SOURCES: list[NewsSource] = [
    # Wire services
    NewsSource("reuters_world",  "Reuters — World",
               "https://www.reutersagency.com/feed/?best-regions=world&post_type=best", 1.0, "world"),
    NewsSource("ap_top",         "Associated Press — Top News",
               "https://feeds.npr.org/1004/rss.xml", 1.0, "world"),
    # Major papers of record
    NewsSource("bbc_world",      "BBC — World",
               "http://feeds.bbci.co.uk/news/world/rss.xml", 1.0, "world"),
    NewsSource("guardian_world", "The Guardian — World",
               "https://www.theguardian.com/world/rss", 0.9, "world"),
    NewsSource("aljazeera",      "Al Jazeera — Top",
               "https://www.aljazeera.com/xml/rss/all.xml", 0.8, "world"),
    NewsSource("dw_world",       "Deutsche Welle — World",
               "https://rss.dw.com/rdf/rss-en-world", 0.8, "world"),
    # Tech
    NewsSource("hn_top",         "Hacker News — Top",
               "https://hnrss.org/frontpage", 0.5, "tech"),
    NewsSource("ars_technica",   "Ars Technica",
               "https://feeds.arstechnica.com/arstechnica/index", 0.6, "tech"),
    # Security
    NewsSource("krebs",          "Krebs on Security",
               "https://krebsonsecurity.com/feed/", 0.8, "security"),
    NewsSource("bleeping",       "BleepingComputer",
               "https://www.bleepingcomputer.com/feed/", 0.6, "security"),
    NewsSource("the_record",     "The Record by Recorded Future",
               "https://therecord.media/feed/", 0.7, "security"),
]


# Topic weights — applied per article based on its source's topic + keyword hits.
# Higher = louder in the heatmap.
TOPIC_WEIGHTS: dict[str, float] = {
    "war":        2.0,
    "disaster":   1.8,
    "crisis":     1.6,
    "security":   1.3,
    "world":      1.0,
    "tech":       0.8,
    "business":   0.9,
    "lifestyle":  0.4,
    "sport":      0.3,
}


# Keyword → topic boost (very rough — improved later with proper classifier).
# Keywords are lowercased and matched as substrings on title + first paragraph.
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "war":      ["war", "missile", "airstrike", "invasion", "front line", "ceasefire",
                 "drone strike", "shelling", "troops killed", "battlefield"],
    "disaster": ["earthquake", "tsunami", "hurricane", "typhoon", "wildfire",
                 "flooding", "landslide", "volcano", "magnitude"],
    "crisis":   ["coup", "protest", "riot", "uprising", "famine", "outbreak",
                 "evacuation", "state of emergency"],
    "security": ["ransomware", "breach", "zero-day", "vulnerability", "exploit",
                 "malware", "phishing", "leaked data", "cyberattack"],
}
