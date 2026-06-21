"""
Lightweight country detection — no external geocoder, no NLP model.

Matches ISO 3166-1 country names (with common aliases) as whole-word
substrings against article title + first paragraph. Good enough for v1
heat-map tagging; can be swapped for a real geocoder later.

Returns ISO Alpha-2 codes so the frontend Leaflet choropleth can match
them against the GeoJSON properties.ISO_A2 attribute.
"""
from __future__ import annotations

import re
from typing import Iterable


# Compact registry: ISO_A2 -> [canonical, aliases…]
# Not exhaustive (no micro-states); covers ~95 % of news mentions.
COUNTRIES: dict[str, list[str]] = {
    "US":  ["United States", "USA", "U.S.", "America", "American"],
    "GB":  ["United Kingdom", "UK", "Britain", "British", "England"],
    "FR":  ["France", "French"],
    "DE":  ["Germany", "German"],
    "IT":  ["Italy", "Italian"],
    "ES":  ["Spain", "Spanish"],
    "PT":  ["Portugal", "Portuguese"],
    "NL":  ["Netherlands", "Dutch", "Holland"],
    "BE":  ["Belgium", "Belgian"],
    "CH":  ["Switzerland", "Swiss"],
    "AT":  ["Austria", "Austrian"],
    "SE":  ["Sweden", "Swedish"],
    "NO":  ["Norway", "Norwegian"],
    "FI":  ["Finland", "Finnish"],
    "DK":  ["Denmark", "Danish"],
    "IE":  ["Ireland", "Irish"],
    "IS":  ["Iceland", "Icelandic"],
    "PL":  ["Poland", "Polish"],
    "CZ":  ["Czech Republic", "Czechia", "Czech"],
    "SK":  ["Slovakia", "Slovak"],
    "HU":  ["Hungary", "Hungarian"],
    "RO":  ["Romania", "Romanian"],
    "BG":  ["Bulgaria", "Bulgarian"],
    "GR":  ["Greece", "Greek"],
    "TR":  ["Turkey", "Turkish", "Türkiye"],
    "RU":  ["Russia", "Russian"],
    "UA":  ["Ukraine", "Ukrainian"],
    "BY":  ["Belarus", "Belarusian"],
    "MD":  ["Moldova", "Moldovan"],
    "RS":  ["Serbia", "Serbian"],
    "HR":  ["Croatia", "Croatian"],
    "SI":  ["Slovenia", "Slovenian"],
    "BA":  ["Bosnia and Herzegovina", "Bosnia"],
    "AL":  ["Albania", "Albanian"],
    "MK":  ["North Macedonia", "Macedonia"],
    "EE":  ["Estonia", "Estonian"],
    "LV":  ["Latvia", "Latvian"],
    "LT":  ["Lithuania", "Lithuanian"],

    "CN":  ["China", "Chinese"],
    "JP":  ["Japan", "Japanese"],
    "KR":  ["South Korea", "Korea", "Korean"],
    "KP":  ["North Korea", "DPRK"],
    "IN":  ["India", "Indian"],
    "PK":  ["Pakistan", "Pakistani"],
    "BD":  ["Bangladesh", "Bangladeshi"],
    "LK":  ["Sri Lanka"],
    "AF":  ["Afghanistan", "Afghan"],
    "IR":  ["Iran", "Iranian"],
    "IQ":  ["Iraq", "Iraqi"],
    "SY":  ["Syria", "Syrian"],
    "LB":  ["Lebanon", "Lebanese"],
    "JO":  ["Jordan", "Jordanian"],
    "IL":  ["Israel", "Israeli"],
    "PS":  ["Palestine", "Palestinian", "Gaza", "West Bank"],
    "SA":  ["Saudi Arabia", "Saudi"],
    "AE":  ["United Arab Emirates", "UAE"],
    "QA":  ["Qatar", "Qatari"],
    "BH":  ["Bahrain", "Bahraini"],
    "KW":  ["Kuwait", "Kuwaiti"],
    "OM":  ["Oman", "Omani"],
    "YE":  ["Yemen", "Yemeni"],
    "EG":  ["Egypt", "Egyptian"],
    "LY":  ["Libya", "Libyan"],
    "TN":  ["Tunisia", "Tunisian"],
    "DZ":  ["Algeria", "Algerian"],
    "MA":  ["Morocco", "Moroccan"],
    "SD":  ["Sudan", "Sudanese"],
    "SS":  ["South Sudan"],
    "ET":  ["Ethiopia", "Ethiopian"],
    "ER":  ["Eritrea", "Eritrean"],
    "SO":  ["Somalia", "Somali"],
    "KE":  ["Kenya", "Kenyan"],
    "TZ":  ["Tanzania", "Tanzanian"],
    "UG":  ["Uganda", "Ugandan"],
    "RW":  ["Rwanda", "Rwandan"],
    "ZA":  ["South Africa"],
    "NG":  ["Nigeria", "Nigerian"],
    "GH":  ["Ghana", "Ghanaian"],
    "CI":  ["Ivory Coast", "Côte d'Ivoire"],
    "SN":  ["Senegal", "Senegalese"],
    "CM":  ["Cameroon", "Cameroonian"],
    "AO":  ["Angola", "Angolan"],
    "MZ":  ["Mozambique", "Mozambican"],
    "ZW":  ["Zimbabwe", "Zimbabwean"],
    "ZM":  ["Zambia", "Zambian"],
    "BW":  ["Botswana"],
    "NA":  ["Namibia", "Namibian"],
    "MG":  ["Madagascar"],

    "TH":  ["Thailand", "Thai"],
    "VN":  ["Vietnam", "Vietnamese"],
    "LA":  ["Laos", "Laotian"],
    "KH":  ["Cambodia", "Cambodian"],
    "MM":  ["Myanmar", "Burma", "Burmese"],
    "MY":  ["Malaysia", "Malaysian"],
    "SG":  ["Singapore"],
    "ID":  ["Indonesia", "Indonesian"],
    "PH":  ["Philippines", "Filipino"],
    "AU":  ["Australia", "Australian"],
    "NZ":  ["New Zealand"],
    "PG":  ["Papua New Guinea"],

    "CA":  ["Canada", "Canadian"],
    "MX":  ["Mexico", "Mexican"],
    "GT":  ["Guatemala", "Guatemalan"],
    "HN":  ["Honduras", "Honduran"],
    "SV":  ["El Salvador", "Salvadoran"],
    "NI":  ["Nicaragua", "Nicaraguan"],
    "CR":  ["Costa Rica"],
    "PA":  ["Panama", "Panamanian"],
    "CU":  ["Cuba", "Cuban"],
    "HT":  ["Haiti", "Haitian"],
    "DO":  ["Dominican Republic"],
    "JM":  ["Jamaica", "Jamaican"],
    "BR":  ["Brazil", "Brazilian"],
    "AR":  ["Argentina", "Argentine"],
    "CL":  ["Chile", "Chilean"],
    "PE":  ["Peru", "Peruvian"],
    "CO":  ["Colombia", "Colombian"],
    "VE":  ["Venezuela", "Venezuelan"],
    "EC":  ["Ecuador", "Ecuadorian"],
    "BO":  ["Bolivia", "Bolivian"],
    "PY":  ["Paraguay", "Paraguayan"],
    "UY":  ["Uruguay", "Uruguayan"],
    "GY":  ["Guyana"],
    "SR":  ["Suriname"],
    "TT":  ["Trinidad and Tobago"],
}


# Cities, landmarks, factions, and geopolitical terms → ISO code.
# These are merged with COUNTRIES in _compile_patterns so any hit tags the
# article with the matching country. Deliberately avoids ambiguous short words
# (e.g. "Nice", "Lima" as a name) — stick to unambiguous, high-signal terms.
LOCATIONS: dict[str, list[str]] = {
    "US": ["Washington DC", "Washington D.C.", "New York City", "New York",
           "Los Angeles", "Chicago", "San Francisco", "Houston", "Dallas",
           "Miami", "Boston", "Seattle", "Atlanta",
           "Pentagon", "White House", "Capitol Hill", "Capitol", "Congress",
           "Senate", "Wall Street", "Silicon Valley", "Hollywood",
           "CIA", "FBI", "NSA", "State Department", "USAID",
           "Federal Reserve", "Fed Reserve"],
    "GB": ["London", "Manchester", "Birmingham", "Edinburgh", "Glasgow",
           "Cardiff", "Liverpool", "Leeds", "Bristol",
           "Downing Street", "Westminster", "Buckingham",
           "MI5", "MI6", "GCHQ", "Whitehall", "NHS", "Scotland Yard"],
    "FR": ["Paris", "Lyon", "Marseille", "Bordeaux", "Toulouse",
           "Normandy", "Élysée", "Elysée", "Elysee"],
    "DE": ["Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne",
           "Stuttgart", "Bundestag", "Bundesrat", "Bavaria", "Bayern",
           "Rhineland", "Saxony"],
    "IT": ["Rome", "Milan", "Naples", "Turin", "Venice", "Florence",
           "Palermo", "Vatican", "Vatican City", "Mafia"],
    "ES": ["Madrid", "Barcelona", "Seville", "Valencia", "Bilbao",
           "Catalonia", "Catalan", "Basque"],
    "PL": ["Warsaw", "Kraków", "Krakow", "Gdansk", "Wroclaw"],
    "NL": ["Amsterdam", "Rotterdam", "The Hague", "Den Haag"],
    "BE": ["Brussels", "Ghent", "Antwerp", "Bruges"],
    "GR": ["Athens", "Thessaloniki", "Crete"],
    "SE": ["Stockholm", "Gothenburg", "Malmö", "Malmo"],
    "NO": ["Oslo", "Bergen"],
    "DK": ["Copenhagen"],
    "FI": ["Helsinki"],
    "CH": ["Zurich", "Geneva", "Bern", "Davos"],
    "PT": ["Lisbon", "Porto"],
    "AT": ["Vienna"],
    "HU": ["Budapest"],
    "CZ": ["Prague", "Praha"],
    "RO": ["Bucharest"],
    "RS": ["Belgrade"],
    "HR": ["Zagreb"],

    "RU": ["Moscow", "Saint Petersburg", "St Petersburg", "St. Petersburg",
           "Kremlin", "Siberia", "Chechen", "Chechnya", "Volga",
           "Novosibirsk", "Yekaterinburg", "Kazan", "Rostov",
           "FSB", "GRU", "SVR", "Gazprom", "Rosneft", "Lukoil",
           "Mariinka", "Bakhmut", "Kursk"],
    "UA": ["Kyiv", "Kiev", "Kharkiv", "Odessa", "Odesa", "Mariupol",
           "Zaporizhzhia", "Zaporizhia", "Kherson", "Mykolaiv", "Lviv",
           "Donbas", "Donbass", "Donetsk", "Luhansk", "Crimea",
           "Azov", "Bucha", "Irpin", "Avdiivka", "Pokrovsk", "Kurakhove"],
    "BY": ["Minsk", "Lukashenko"],
    "MD": ["Chisinau", "Transnistria"],

    "CN": ["Beijing", "Shanghai", "Hong Kong", "Shenzhen", "Guangzhou",
           "Chongqing", "Wuhan", "Chengdu", "Hangzhou", "Nanjing",
           "Tianjin", "Xi'an", "Xian",
           "Xinjiang", "Tibet", "Uyghur", "Uighur", "Tiananmen",
           "South China Sea", "PLA", "CCP", "Politburo",
           "Belt and Road", "CCTV", "Xinhua", "People's Daily",
           "Alibaba", "Huawei", "Tencent", "ByteDance"],
    "JP": ["Tokyo", "Osaka", "Kyoto", "Hiroshima", "Nagasaki",
           "Okinawa", "Yokohama", "Nagoya", "Fukushima", "Hokkaido"],
    "KR": ["Seoul", "Busan", "Incheon", "Daegu", "Gwangju"],
    "KP": ["Pyongyang", "Kim Jong", "Kim Jong-un", "DPRK"],
    "IN": ["Delhi", "New Delhi", "Mumbai", "Bombay", "Kolkata", "Calcutta",
           "Bangalore", "Bengaluru", "Chennai", "Madras", "Hyderabad",
           "Ahmedabad", "Pune", "Jaipur", "Lucknow",
           "Kashmir", "Punjab", "Assam", "Modi", "BJP", "RAW"],
    "PK": ["Islamabad", "Karachi", "Lahore", "Rawalpindi", "Peshawar",
           "Quetta", "ISI", "Balochistan", "FATA"],
    "BD": ["Dhaka", "Chittagong"],
    "LK": ["Colombo", "Kandy"],
    "NP": ["Kathmandu"],
    "AF": ["Kabul", "Kandahar", "Herat", "Mazar-i-Sharif",
           "Taliban", "al-Qaeda", "Al-Qaeda"],
    "IR": ["Tehran", "Mashhad", "Isfahan", "Shiraz", "Tabriz",
           "IRGC", "Revolutionary Guard", "Khamenei", "Rouhani", "Raisi",
           "Quds Force"],
    "IQ": ["Baghdad", "Mosul", "Basra", "Erbil", "Najaf", "Karbala",
           "Fallujah", "Tikrit", "Kurdistan"],
    "SY": ["Damascus", "Aleppo", "Idlib", "Homs", "Hama",
           "ISIS", "ISIL", "Islamic State", "Daesh",
           "HTS", "Hayat Tahrir al-Sham", "Assad"],
    "LB": ["Beirut", "Tripoli", "Hezbollah", "South Lebanon"],
    "JO": ["Amman", "Aqaba"],
    "IL": ["Tel Aviv", "Jerusalem", "Haifa", "Netanya", "Beer Sheva",
           "IDF", "Mossad", "Shin Bet", "Netanyahu", "Knesset",
           "West Bank", "Settlers", "Settlements"],
    "PS": ["Gaza", "Gaza Strip", "Rafah", "Khan Younis", "Jabalia",
           "Deir al-Balah", "Ramallah", "Jenin", "Nablus", "Hebron",
           "Hamas", "Islamic Jihad", "PLO", "PA", "Palestinian Authority"],
    "SA": ["Riyadh", "Jeddah", "Mecca", "Medina", "Neom",
           "Aramco", "Saudi Aramco", "MBS", "Crown Prince"],
    "AE": ["Dubai", "Abu Dhabi", "Sharjah"],
    "QA": ["Doha", "Al Jazeera"],
    "KW": ["Kuwait City"],
    "BH": ["Manama"],
    "OM": ["Muscat"],
    "YE": ["Sanaa", "Sana'a", "Aden", "Hudaydah", "Houthis", "Houthi",
           "Ansar Allah"],
    "EG": ["Cairo", "Alexandria", "Sinai", "Suez", "Sharm el-Sheikh",
           "Luxor", "Aswan"],
    "LY": ["Tripoli", "Benghazi", "Sirte", "Tobruk"],
    "TN": ["Tunis", "Carthage"],
    "DZ": ["Algiers", "Oran"],
    "MA": ["Rabat", "Casablanca", "Marrakesh", "Tangier", "Fez"],
    "SD": ["Khartoum", "Darfur", "RSF", "SAF", "Rapid Support Forces"],
    "SS": ["Juba", "South Sudan"],
    "ET": ["Addis Ababa", "Tigray", "Tigrayans", "Amhara", "Oromia",
           "TPLF"],
    "SO": ["Mogadishu", "Hargeisa", "al-Shabaab", "Al-Shabaab"],
    "NG": ["Lagos", "Abuja", "Boko Haram", "Kano", "Port Harcourt"],
    "KE": ["Nairobi", "Mombasa"],
    "ZA": ["Johannesburg", "Cape Town", "Pretoria", "Durban",
           "Soweto", "ANC"],
    "GH": ["Accra"],
    "CI": ["Abidjan", "Yamoussoukro"],
    "CM": ["Yaoundé", "Yaound", "Douala"],
    "SN": ["Dakar"],
    "AO": ["Luanda"],
    "MZ": ["Maputo"],
    "ZW": ["Harare", "Bulawayo"],
    "ZM": ["Lusaka"],

    "TH": ["Bangkok", "Chiang Mai", "Pattaya"],
    "VN": ["Hanoi", "Ho Chi Minh City", "Ho Chi Minh", "Saigon"],
    "MM": ["Yangon", "Rangoon", "Naypyidaw", "Myanmar junta",
           "Tatmadaw", "SAC"],
    "KH": ["Phnom Penh"],
    "LA": ["Vientiane"],
    "MY": ["Kuala Lumpur", "KL", "Putrajaya", "Sabah", "Sarawak"],
    "SG": ["Singapore"],
    "ID": ["Jakarta", "Bali", "Surabaya", "Papua"],
    "PH": ["Manila", "Davao", "Cebu", "Mindanao"],

    "AU": ["Sydney", "Melbourne", "Canberra", "Brisbane", "Perth",
           "Adelaide", "Darwin", "Queensland", "Victoria"],
    "NZ": ["Auckland", "Wellington", "Christchurch"],
    "PG": ["Port Moresby"],

    "CA": ["Ottawa", "Toronto", "Montreal", "Vancouver", "Calgary",
           "Edmonton", "Quebec City", "Quebec", "Ontario",
           "Alberta", "British Columbia"],
    "MX": ["Mexico City", "Guadalajara", "Monterrey", "Tijuana",
           "Cancún", "Cancun", "Juárez", "Chiapas", "Sinaloa",
           "Jalisco cartel", "Cartel"],
    "GT": ["Guatemala City"],
    "HN": ["Tegucigalpa"],
    "SV": ["San Salvador"],
    "NI": ["Managua"],
    "CR": ["San José", "San Jose"],
    "PA": ["Panama City"],
    "CU": ["Havana"],
    "HT": ["Port-au-Prince", "Port au Prince"],
    "DO": ["Santo Domingo"],
    "JM": ["Kingston"],
    "BR": ["Brasilia", "Brasília", "São Paulo", "Sao Paulo",
           "Rio de Janeiro", "Manaus", "Amazon", "Amazonia",
           "Bolsonaro", "Lula"],
    "AR": ["Buenos Aires", "Patagonia", "Cordoba", "Córdoba",
           "Mendoza", "Milei"],
    "CL": ["Santiago", "Valparaíso", "Valparaiso"],
    "PE": ["Lima", "Cusco", "Cuzco", "Machu Picchu"],
    "CO": ["Bogotá", "Bogota", "Medellín", "Medellin", "Cali",
           "FARC", "ELN"],
    "VE": ["Caracas", "Maracaibo", "Maduro", "Chavez", "Chávez"],
    "EC": ["Quito", "Guayaquil"],
    "BO": ["La Paz", "Santa Cruz", "Sucre"],
    "PY": ["Asunción", "Asuncion"],
    "UY": ["Montevideo"],
}


def _merge_dicts(*dicts: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for d in dicts:
        for iso, names in d.items():
            out.setdefault(iso, []).extend(names)
    return out


# Combined lookup used for pattern compilation
_ALL_TERMS = _merge_dicts(COUNTRIES, LOCATIONS)


# Pre-compile a single regex per country (whole-word, case-insensitive)
def _compile_patterns() -> dict[str, re.Pattern]:
    out: dict[str, re.Pattern] = {}
    for iso, aliases in _ALL_TERMS.items():
        # Escape, sort by length descending so longer aliases match first
        parts = sorted((re.escape(a) for a in aliases), key=len, reverse=True)
        # Whole-word boundary
        pat = r"\b(?:" + "|".join(parts) + r")\b"
        out[iso] = re.compile(pat, re.IGNORECASE)
    return out


_PATTERNS = _compile_patterns()


def detect_countries(text: str, limit: int = 6) -> list[str]:
    """
    Return up to `limit` ISO-A2 country codes mentioned in `text`,
    ordered by first-occurrence position (earlier mention = more likely
    to be the main subject).
    """
    if not text:
        return []
    hits: list[tuple[int, str]] = []
    for iso, pat in _PATTERNS.items():
        m = pat.search(text)
        if m:
            hits.append((m.start(), iso))
    hits.sort()
    out: list[str] = []
    seen: set[str] = set()
    for _, iso in hits:
        if iso not in seen:
            out.append(iso)
            seen.add(iso)
        if len(out) >= limit:
            break
    return out


def country_name(iso: str) -> str:
    """Pretty-print a country's canonical name (always uses COUNTRIES, not LOCATIONS)."""
    aliases = COUNTRIES.get(iso.upper())
    return aliases[0] if aliases else iso


# URL-slug detector — most news sites embed country names in the path,
# e.g. theguardian.com/world/germany/, bbc.co.uk/news/world-europe-ukraine-...
# These signals are often more reliable than body-text matches.
# Uses _ALL_TERMS so city slugs (kyiv, kabul, etc.) are also picked up.
_URL_COUNTRY_RE = re.compile(
    r"/(?:world|news|topics?)/[^/]*?\b("
    + "|".join(
        sorted(
            {a.lower().replace(" ", "-")
             for aliases in _ALL_TERMS.values()
             for a in aliases
             if len(a) > 3 and a.replace(" ", "").isalpha()},
            key=len, reverse=True,
        )
    )
    + r")\b",
    re.IGNORECASE,
)

# Lookup: name-slug → iso (for the URL detector to resolve back)
_SLUG_TO_ISO: dict[str, str] = {
    a.lower().replace(" ", "-"): iso
    for iso, aliases in _ALL_TERMS.items()
    for a in aliases
    if len(a) > 3 and a.replace(" ", "").isalpha()
}


def detect_from_url(url: str) -> list[str]:
    """Pull country ISO codes out of a URL's path slug. Cheap and reliable."""
    if not url:
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for m in _URL_COUNTRY_RE.finditer(url):
        slug = m.group(1).lower()
        iso = _SLUG_TO_ISO.get(slug)
        if iso and iso not in seen:
            hits.append(iso)
            seen.add(iso)
    return hits


def detect_all(*, title: str = "", summary: str = "", url: str = "",
               limit: int = 6) -> list[str]:
    """
    Combined detector: URL slugs first (highest confidence), then title,
    then summary. Returns up to `limit` ISO codes in priority order.
    """
    out: list[str] = []
    seen: set[str] = set()

    for iso in detect_from_url(url):
        if iso not in seen:
            out.append(iso); seen.add(iso)
            if len(out) >= limit: return out

    for iso in detect_countries(title, limit=limit):
        if iso not in seen:
            out.append(iso); seen.add(iso)
            if len(out) >= limit: return out

    for iso in detect_countries(summary, limit=limit):
        if iso not in seen:
            out.append(iso); seen.add(iso)
            if len(out) >= limit: return out

    return out
