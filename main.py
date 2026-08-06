"""
Kruss Real Estate Scraper
─────────────────────────
Scrapes property listings from kruss.co.ke, filters by quarter, and saves
quarterly CSV files with auto-save after every page and full resume support.

Run:  python main.py
"""

import csv
import html
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
Base_URL      = "https://kruss.co.ke"
Search_URL    = f"{Base_URL}/property-status/for-sale/"
Request_Delay = 0.4          # seconds between requests per thread
MAX_WORKERS   = 5            # concurrent detail-page fetches

STATE_FILE = "scraper_state.json"
OUTPUT_DIR = "output"

KENYA_COUNTIES = [
    "Nairobi", "Mombasa", "Kisumu", "Nakuru", "Uasin Gishu", "Kiambu",
    "Machakos", "Kajiado", "Muranga", "Nyeri", "Meru", "Embu", "Kirinyaga",
    "Nyandarua", "Laikipia", "Samburu", "Isiolo", "Marsabit", "Mandera",
    "Wajir", "Garissa", "Tana River", "Kilifi", "Kwale", "Taita Taveta",
    "Lamu", "Trans Nzoia", "West Pokot", "Elgeyo Marakwet", "Nandi",
    "Baringo", "Kericho", "Bomet", "Narok", "Kisii", "Nyamira", "Migori",
    "Homa Bay", "Siaya", "Vihiga", "Kakamega", "Bungoma", "Busia",
    "Turkana", "Kitui", "Makueni",
]

OUTPUT_COLUMNS = [
    "Listing_ID", "Name", "Type", "Category", "Price", "Location", "County",
    "No. of Bedrooms", "No. of Bathrooms", "No. of Ensuite Bedrooms",
    "Date", "Floor_area_sqm", "Land_Size", "Elevator", "Parking",
    "Condition", "DSQ", "Floor_Number", "URL",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ── Thread-local HTTP sessions (one per worker thread) ────────────────────────
_thread_local = threading.local()


def get_session() -> requests.Session:
    """Return a per-thread requests.Session, creating it if needed."""
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _thread_local.session = s
    return _thread_local.session


# ── Quarter helpers ───────────────────────────────────────────────────────────
QUARTER_LABELS = {
    1: "Q1 (Jan – Mar)",
    2: "Q2 (Apr – Jun)",
    3: "Q3 (Jul – Sep)",
    4: "Q4 (Oct – Dec)",
}

QUARTER_MONTHS = {
    1: (1,  3),
    2: (4,  6),
    3: (7,  9),
    4: (10, 12),
}


def quarter_date_range(year: int, quarter: int) -> tuple[date, date]:
    start_month, end_month = QUARTER_MONTHS[quarter]
    date_from = date(year, start_month, 1)
    date_to   = (
        date(year, 12, 31)
        if end_month == 12
        else date(year, end_month + 1, 1) - timedelta(days=1)
    )
    return date_from, date_to


def csv_filename(year: int, quarter: int) -> str:
    return os.path.join(OUTPUT_DIR, f"kruss_Q{quarter}_{year}.csv")


# ── State / resume management ─────────────────────────────────────────────────
def load_state() -> dict | None:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return None


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_scraped_urls_from_csv(filepath: str) -> set[str]:
    urls: set[str] = set()
    if not os.path.exists(filepath):
        return urls
    try:
        with open(filepath, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("URL"):
                    urls.add(row["URL"])
    except IOError:
        pass
    return urls


def ensure_csv_header(filepath: str) -> None:
    if not os.path.exists(filepath):
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS).writeheader()


def append_records_to_csv(records: list[dict], filepath: str) -> None:
    if not records:
        return
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        for r in records:
            w.writerow({col: r.get(col, "") for col in OUTPUT_COLUMNS})
    log.info(f"  💾 Auto-saved {len(records)} record(s) → {filepath}")


# ── Session / quarter selection prompt ────────────────────────────────────────
def prompt_session_choice() -> tuple[int, int, int]:
    """Return (year, quarter, start_page)."""
    state = load_state()

    if state:
        y   = state.get("year",    "?")
        q   = state.get("quarter", "?")
        pg  = state.get("last_completed_page", 0)
        lbl = QUARTER_LABELS.get(q, f"Q{q}")
        print()
        print("=" * 60)
        print("  PREVIOUS SESSION FOUND")
        print(f"  Year                : {y}")
        print(f"  Quarter             : {lbl}")
        print(f"  Last completed page : {pg}")
        print("=" * 60)
        while True:
            choice = input(
                "\n  [R] Resume last session   [N] Start new session : "
            ).strip().upper()
            if choice in ("R", "N"):
                break
            print("  Please enter R or N.")

        if choice == "R":
            log.info(f"Resuming {lbl} {y} from page {pg + 1}")
            return int(y), int(q), int(pg) + 1

    print()
    print("=" * 60)
    print("  NEW SCRAPING SESSION")
    print("=" * 60)

    while True:
        raw = input("\n  Enter the YEAR to scrape (e.g. 2026) : ").strip()
        if raw.isdigit() and 2000 <= int(raw) <= 2100:
            year = int(raw)
            break
        print("  Please enter a valid 4-digit year (2000 – 2100).")

    print()
    print("  Which quarter would you like to scrape?")
    for num, label in QUARTER_LABELS.items():
        print(f"    {num}  →  {label}")

    while True:
        raw = input("\n  Enter quarter number (1, 2, 3 or 4) : ").strip()
        if raw.isdigit() and int(raw) in QUARTER_LABELS:
            quarter = int(raw)
            break
        print("  Please enter 1, 2, 3, or 4.")

    log.info(f"Starting new session: {QUARTER_LABELS[quarter]} {year}")
    return year, quarter, 1


# ── HTTP fetch (thread-safe) ──────────────────────────────────────────────────
def fetch(url: str, retries: int = 3) -> str | None:
    sess = get_session()
    for attempt in range(1, retries + 1):
        try:
            resp = sess.get(url, timeout=30)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
            time.sleep(Request_Delay)
            return resp.text

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            log.warning(f"HTTP {status} on {url} (attempt {attempt}/{retries})")
            if status in (403, 404, 410):
                return None  # permanent – don't retry

        except requests.RequestException as e:
            log.warning(f"Request error on {url}: {e} (attempt {attempt}/{retries})")

        time.sleep(2 * attempt)
    return None


# ── County matching ───────────────────────────────────────────────────────────
def match_county(*texts: str) -> str:
    combined = " ".join(t for t in texts if t).lower()
    for county in KENYA_COUNTIES:
        if county.lower() in combined:
            return county
    return "Unknown"


# ── Type / Category classification ───────────────────────────────────────────
# Type     → Residential | Land | Commercial
# Category → Studio | Bungalow | Apartment | Mansionette | Townhouse | Villa |
#            Land | Office | Retail | Industrial | Warehousing | Social Amenity
#
# "house" alone (without townhouse/bungalow/etc.) → Townhouse per user request.
# Checked from most-specific to least-specific so short keywords don't shadow
# longer ones (e.g. "townhouse" is checked before bare "house").

CATEGORY_KEYWORDS: list[tuple[str, str, str]] = [
    # ── Residential ──────────────────────────────────────────────────────────
    (r"\bstudios?\b",                                          "Residential", "Studio"),
    (r"\bbungalows?\b",                                        "Residential", "Bungalow"),
    (r"\bmansionettes?\b",                                     "Residential", "Mansionette"),
    (r"\btown[\s-]?houses?\b",                                 "Residential", "Townhouse"),
    (r"\bvillas?\b",                                           "Residential", "Villa"),
    (r"\b(apartments?|flats?)\b",                              "Residential", "Apartment"),
    (r"\bhouses?\b",                                           "Residential", "Townhouse"),  # user: "House" → Townhouse
    # ── Commercial ───────────────────────────────────────────────────────────
    (r"\boffices?\b",                                          "Commercial",  "Office"),
    (r"\bretail\b|\bshops?\b",                                 "Commercial",  "Retail"),
    (r"\bindustrial\b",                                        "Commercial",  "Industrial"),
    (r"\bwarehous\w*\b",                                       "Commercial",  "Warehousing"),
    (r"\b(social\s+amenity|school|hospital|church|clinic)\b",  "Commercial",  "Social Amenity"),
    # ── Land ─────────────────────────────────────────────────────────────────
    (r"\b(land|plots?|acres?|hectares?)\b",                    "Land",        "Land"),
]

# Pre-compile for speed
_COMPILED_KEYWORDS = [
    (re.compile(pat, re.IGNORECASE), t, c)
    for pat, t, c in CATEGORY_KEYWORDS
]


def classify_property(name: str, raw_type_hint: str = "") -> tuple[str, str]:
    """
    Return (Type, Category) from listing name and optional raw site-badge hint.
    First match wins; most-specific patterns are listed first.
    """
    combined = f"{name} {raw_type_hint}"
    for pat, prop_type, category in _COMPILED_KEYWORDS:
        if pat.search(combined):
            return prop_type, category
    log.warning(f"Unclassified property – name={name!r} hint={raw_type_hint!r}")
    return "", ""


def is_land_type(name: str, raw_type_hint: str) -> bool:
    """Quick check used to route area-size labels before Type is fully classified."""
    combined = f"{name} {raw_type_hint}".lower()
    return bool(re.search(r"\b(land|plots?|acres?|hectares?)\b", combined))


# ── Search-page badge parsing ─────────────────────────────────────────────────
# The site uses `property-breadcrumbs` spans.  On the current theme there are
# FIVE spans per card:
#   "Property ID :"  |  "LS219"  |  ""  |  "Land/Plots"  |  "Last updated: …"
# (The ID is split across a label span and a value span.)
# We accept any ancestor whose badge count is in VALID_BADGE_COUNTS so the
# parser stays correct even if the site ever changes to 3-badge layout.

VALID_BADGE_COUNTS = frozenset(range(3, 11))   # 3–10 badges; handles layout variants

ID_PREFIX_RE   = re.compile(r"\bID\s*:\s*(.+)",       re.IGNORECASE)
ID_FORMAT_RE   = re.compile(r"^[A-Za-z]{1,4}\d+[A-Za-z0-9-]*$")
DATE_PREFIX_RE = re.compile(r"Last\s+updated\s*:\s*(.+)", re.IGNORECASE)
# Date formats: month-first ("August 6, 2026", "Aug 6, 2026") or
# day-first ("6 August, 2026", "6 Aug, 2026").
_DATE_CORE = r"(?:[A-Za-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]+\.?,?\s+\d{4})"
DATE_FORMAT_RE   = re.compile(rf"^{_DATE_CORE}$")
DATE_TOKEN_RE    = re.compile(_DATE_CORE)
LAST_UPDATED_RE  = re.compile(rf"Last\s+updated\s*:?\s*(?P<d>{_DATE_CORE})", re.IGNORECASE)
# Label-only fragments to skip (they contain no useful value)
LABEL_ONLY_RE  = re.compile(r"^(Property\s+)?ID\s*:?\s*$", re.IGNORECASE)


def find_card_badges(name_tag) -> list:
    """
    Walk up the DOM from a listing title tag and return the badge spans
    belonging to that single card (the tightest ancestor whose badge count
    is in VALID_BADGE_COUNTS).
    """
    for ancestor in name_tag.parents:
        found = ancestor.find_all("span", class_="property-breadcrumbs")
        n = len(found)
        if n in VALID_BADGE_COUNTS:
            return found
        if n > max(VALID_BADGE_COUNTS):
            break   # crossed into a multi-card wrapper (12+ badges = multiple cards)
    return []


def classify_badge(text: str) -> tuple[str, str]:
    """Return (field_name, value) for one badge's text content."""
    # Skip pure label fragments like "Property ID :"
    if LABEL_ONLY_RE.match(text):
        return "", ""

    m = ID_PREFIX_RE.search(text)
    if m and m.group(1).strip():
        return "Listing_ID", m.group(1).strip()

    if ID_FORMAT_RE.match(text):
        return "Listing_ID", text

    m = DATE_PREFIX_RE.search(text)
    if m:
        return "Date", m.group(1).strip()
    if DATE_FORMAT_RE.match(text):
        return "Date", text

    return "raw_type", text


def fallback_card_fields(name_tag) -> tuple[str, str]:
    """
    Best-effort Listing_ID / Date extraction straight from the card block,
    used when the breadcrumb-badge layout can't be parsed at all.

    A bare date is only trusted when the same block also contains an
    ID-format token, so dates from neighboring cards or the page footer
    can't leak into this card and skew the quarter filter.
    """
    listing_id, listing_date = "", ""
    block = name_tag
    for _ in range(6):
        block = block.parent
        if block is None:
            break
        text = block.get_text(" ", strip=True)
        if len(text) > 4000:
            break                      # wandered outside the card block
        block_id, block_date = "", ""
        for token in text.split():
            if ID_FORMAT_RE.match(token) and len(token) <= 15:
                block_id = token
                break
        m = LAST_UPDATED_RE.search(text)
        if m:
            block_date = m.group("d").strip().rstrip(",")
        elif block_id:                 # bare date only if an ID is here too
            m = DATE_TOKEN_RE.search(text)
            if m:
                block_date = m.group(0).strip().rstrip(",")
        listing_id = listing_id or block_id
        listing_date = listing_date or block_date
        if listing_id and listing_date:
            break
    return listing_id, listing_date


def extract_card_fields(name_tag) -> dict:
    """
    Return {"Listing_ID": …, "raw_type": …, "Date": …} for one listing card.
    Fields are set first-wins per field (except raw_type uses last-wins so that
    a label fragment like "Property ID :" is replaced by the actual type badge).
    """
    fields = {"Listing_ID": "", "raw_type": "", "Date": ""}
    title  = name_tag.get_text(strip=True) if name_tag else "<unknown>"
    try:
        badges = find_card_badges(name_tag)
        if not badges:
            # Fallback: scan the card block directly for an ID / date
            lid, ldate = fallback_card_fields(name_tag)
            if lid:
                fields["Listing_ID"] = lid
            if ldate:
                fields["Date"] = ldate
            if not (lid or ldate):
                log.warning(
                    f"No card boundary found for {title!r} — "
                    "ID/Type/Date left blank."
                )
            return fields

        raw_type_candidates: list[str] = []
        for badge in badges:
            text = badge.get_text(" ", strip=True)
            if not text:
                continue
            field, value = classify_badge(text)
            if not field or not value:
                continue
            if field == "raw_type":
                raw_type_candidates.append(value)
            elif not fields[field]:          # first-wins for ID and Date
                fields[field] = value

        # Pick the best raw_type: prefer entries that don't look like ID labels
        for candidate in raw_type_candidates:
            if not LABEL_ONLY_RE.match(candidate):
                fields["raw_type"] = candidate
                break

    except Exception:
        log.exception(f"Badge parse error for {title!r}")
    return fields


# ── Date helpers ──────────────────────────────────────────────────────────────
def parse_kruss_date(text: str) -> date | None:
    if not text:
        return None
    for fmt in (
        "%B %d, %Y", "%B %d %Y", "%B %d,%Y",          # month-first
        "%d %B, %Y", "%d %B %Y",                      # day-first
        "%b %d, %Y", "%b %d %Y", "%b. %d, %Y",        # abbreviated month
        "%d %b, %Y", "%d %b %Y", "%d %b., %Y",
    ):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None


def listing_in_range(
    record: dict, date_from: date | None, date_to: date | None
) -> bool:
    """
    Strict quarter/year check: True only when the listing's date is known
    and falls inside [date_from, date_to]. Keeps out-of-quarter and undated
    listings out of the output CSV.
    """
    if date_from is None or date_to is None:
        return True
    listing_date = parse_kruss_date(record.get("Date", ""))
    if not listing_date:
        log.warning(
            f"  No date found for {record.get('URL', '?')} — excluded "
            f"(outside {date_from} → {date_to} filter)."
        )
        return False
    if not (date_from <= listing_date <= date_to):
        log.info(
            f"  Skipping {record.get('URL', '?')}: dated {listing_date} "
            f"outside {date_from} → {date_to}."
        )
        return False
    return True


def find_detail_page_date(soup: BeautifulSoup) -> str:
    """Fallback: find a 'Last updated' label or a bare date string."""
    # 1) "Last updated: <date>" – label may be split across elements
    for string in soup.find_all(string=LAST_UPDATED_RE):
        parent = string.parent
        for _ in range(3):
            if parent is None:
                break
            m = LAST_UPDATED_RE.search(parent.get_text(" ", strip=True))
            if m:
                return m.group("d").strip().rstrip(",")
            parent = parent.parent
    # 2) bare date string inside a small element
    for tag in soup.find_all(["span", "li", "div", "p"]):
        text = tag.get_text(strip=True)
        if text and DATE_FORMAT_RE.match(text):
            return text
    return ""


# ── Listing-ID fallback (detail page) ─────────────────────────────────────────
def find_detail_page_listing_id(soup: BeautifulSoup) -> str:
    """Fallback: locate a property/listing ID somewhere on the detail page."""
    id_label_re = re.compile(
        r"(?:property|listing|reference|ref)\s*(?:id\s*)?:?", re.IGNORECASE
    )
    id_value_re = re.compile(
        r"(?:property|listing|reference|ref)\s*(?:id\s*)?:?\s*"
        r"\b([A-Za-z]{1,4}\d+[A-Za-z0-9-]*)\b",
        re.IGNORECASE,
    )
    # 1) Label + value in the same element, or a few levels up
    for string in soup.find_all(string=id_label_re):
        parent = string.parent
        for _ in range(3):
            if parent is None:
                break
            m = id_value_re.search(parent.get_text(" ", strip=True))
            if m:
                return m.group(1)
            parent = parent.parent
    # 2) Last resort: a bare ID-format token in a small element
    for tag in soup.find_all(["span", "strong", "b", "li", "td"]):
        for token in tag.get_text(" ", strip=True).split():
            token = token.strip(".,;:")
            if ID_FORMAT_RE.match(token) and len(token) <= 15:
                return token
    return ""


# ── JSON-LD extraction ────────────────────────────────────────────────────────
def extract_json_ld_listing(soup: BeautifulSoup) -> dict | None:
    ld_graph: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "@graph" in data:
            ld_graph.extend(data["@graph"])
        elif isinstance(data, list):
            ld_graph.extend(data)
        else:
            ld_graph.append(data)

    for node in ld_graph:
        if isinstance(node, dict) and node.get("@type") == "RealEstateListing":
            return node
    return None


def apply_json_ld(soup: BeautifulSoup, record: dict) -> None:
    """Fill Name / Price / Location / County from JSON-LD structured data."""
    listing = extract_json_ld_listing(soup)
    if not listing:
        log.warning("No RealEstateListing JSON-LD found")
        return

    # Decode HTML entities that sometimes appear inside JSON-LD strings
    raw_name = listing.get("name", "") or ""
    record["Name"] = html.unescape(raw_name)

    offers = listing.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    record["Price"] = (
        str(offers.get("price", "")) if isinstance(offers, dict) else ""
    )

    address = listing.get("address", {})
    if isinstance(address, list):
        address = address[0] if address else {}
    street   = address.get("streetAddress", "") if isinstance(address, dict) else ""
    locality = address.get("addressLocality", "") if isinstance(address, dict) else ""
    record["Location"] = locality or street
    record["County"]   = match_county(street, locality)


# ── Meta-card row (Bedrooms / Bathrooms / Area / etc.) ───────────────────────
META_LABEL_MAP: dict[str, str] = {
    "bedrooms":     "No. of Bedrooms",
    "bathrooms":    "No. of Bathrooms",
    "garage":       "Parking",
    "garages":      "Parking",
    "parking":      "Parking",
    "ensuite":      "No. of Ensuite Bedrooms",
    "en suite":     "No. of Ensuite Bedrooms",
    "en-suite":     "No. of Ensuite Bedrooms",
    "floor number": "Floor_Number",
    "floor":        "Floor_Number",
}
AREA_LABELS: frozenset[str] = frozenset({"lot size", "floor size", "area size", "size"})


def apply_meta_cards(soup: BeautifulSoup, record: dict, is_land: bool) -> None:
    """
    Parse the meta-card row.
    `is_land` is passed in (derived from name + raw_type_hint) so that area
    labels are routed to the correct column even before Type is classified.
    """
    for meta in soup.find_all("div", class_="rh_ultra_prop_card__meta"):
        label_tag = meta.find("span", class_="rh-ultra-meta-label")
        if not label_tag:
            continue
        label       = label_tag.get_text(strip=True)
        label_lower = label.lower()

        figure_tag = meta.find("span", class_="figure")
        unit_tag   = meta.select_one(".rh_ultra_meta_box .label")
        value      = figure_tag.get_text(strip=True) if figure_tag else ""
        if unit_tag:
            value = f"{value} {unit_tag.get_text(strip=True)}".strip()
        if not value:
            continue

        if label_lower in AREA_LABELS:
            if is_land:
                record["Land_Size"] = value
            else:
                record["Floor_area_sqm"] = value
            continue

        mapped = META_LABEL_MAP.get(label_lower)
        if mapped:
            record[mapped] = value


# ── Features / amenities list ─────────────────────────────────────────────────
FEATURE_FLAG_PATTERNS: dict[str, re.Pattern] = {
    "Elevator": re.compile(r"elevator|\blift\b",             re.IGNORECASE),
    "DSQ":      re.compile(r"\bdsq\b|servant|domestic\s+staff", re.IGNORECASE),
    "Parking":  re.compile(r"\bparking\b",                   re.IGNORECASE),
}


def feature_present(value) -> bool:
    """True when an already-extracted value indicates the feature exists."""
    if not value:
        return False
    return str(value).strip().lower() not in ("0", "no", "none", "-", "n/a")


def apply_features(soup: BeautifulSoup, record: dict) -> None:
    """
    Binary feature flags: "1" when the feature is present, else "0".

    Sources: the site's feature/amenities list, plus any meta-card value
    already captured (e.g. Parking from a "Garage: 2" row).
    """
    feature_texts = [
        a.get_text(strip=True)
        for a in soup.select(
            "ul.rh_property__features li.rh_property__feature a"
        )
    ]
    for field, pat in FEATURE_FLAG_PATTERNS.items():
        present = feature_present(record.get(field))  # e.g. Parking from meta-card
        if not present:
            for text in feature_texts:
                if pat.search(text):
                    present = True
                    break
        record[field] = "1" if present else "0"


# ── Search page ───────────────────────────────────────────────────────────────
def get_listing_summaries_from_page(page_num: int) -> list[dict]:
    """
    Returns one dict per card: URL, Listing_ID, raw_type, Date.
    Uses the main thread's session (search pages are fetched sequentially).
    """
    url  = Search_URL if page_num == 1 else f"{Search_URL}page/{page_num}/"
    body = fetch(url)
    if not body:
        return []

    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        log.exception(f"HTML parse failed for search page {page_num}")
        return []

    summaries = []
    for name_tag in soup.find_all("h3", class_="rh-ultra-property-title"):
        a = name_tag.find("a")
        if not a or not a.get("href"):
            continue
        card = extract_card_fields(name_tag)
        summaries.append(
            {
                "URL":        urljoin(Base_URL, a["href"]),
                "Listing_ID": card["Listing_ID"],
                "raw_type":   card["raw_type"],
                "Date":       card["Date"],
            }
        )

    log.info(f"  Page {page_num}: {len(summaries)} listings found")
    return summaries


# ── Detail-page scraper ───────────────────────────────────────────────────────
def scrape_property(
    summary: dict,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict | None:
    """
    Fetch and parse one listing detail page. Safe to call from any thread.

    When date_from/date_to are given the record is validated against the
    listing's best-known date (card badge, else detail page) and None is
    returned when it can't be confirmed to fall inside that quarter/year.
    """
    url    = summary["URL"]
    record = {col: "" for col in OUTPUT_COLUMNS}
    for f in FEATURE_FLAG_PATTERNS:
        record[f] = "0"                   # feature flags default to absent (0)
    record["URL"]        = url        # -> URL
    record["Listing_ID"] = summary.get("Listing_ID", "")     # -> Listing ID
    record["Date"]       = summary.get("Date", "")    # -> Date
    raw_type_hint        = summary.get("raw_type", "")   

    body = fetch(url)
    if not body:
        log.warning(f"Failed to fetch: {url}")
        return record if listing_in_range(record, date_from, date_to) else None

    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        log.exception(f"HTML parse failed: {url}")
        return record

    # ── JSON-LD: Name / Price / Location / County ─────────────────────────────
    try:
        apply_json_ld(soup, record)
    except Exception:
        log.exception(f"JSON-LD failed: {url}")

    # ── Determine land type early so area labels route correctly ──────────────
    is_land = is_land_type(record.get("Name", ""), raw_type_hint)

    # ── Meta-cards: Bedrooms / Bathrooms / Area / etc. ───────────────────────
    try:
        apply_meta_cards(soup, record, is_land=is_land)
    except Exception:
        log.exception(f"Meta-card failed: {url}")

    # ── Feature list: Elevator / DSQ / Parking ───────────────────────────────
    try:
        apply_features(soup, record)
    except Exception:
        log.exception(f"Features failed: {url}")

    # ── Date fallback ─────────────────────────────────────────────────────────
    if not record.get("Date"):
        try:
            record["Date"] = find_detail_page_date(soup)
        except Exception:
            pass

    # ── Listing ID fallback ───────────────────────────────────────────────────
    if not record.get("Listing_ID"):
        try:
            record["Listing_ID"] = find_detail_page_listing_id(soup)
        except Exception:
            log.exception(f"Listing-ID fallback failed: {url}")

    # ── Strict quarter/year date filter ───────────────────────────────────────
    if not listing_in_range(record, date_from, date_to):
        return None

    # ── Type + Category ───────────────────────────────────────────────────────
    try:
        prop_type, category = classify_property(
            record.get("Name", ""), raw_type_hint
        )
        record["Type"]     = prop_type     # -> Type
        record["Category"] = category     # -> Category
    except Exception:
        log.exception(f"Classification failed: {url}")

    return record


# ── Concurrent page scraper ───────────────────────────────────────────────────
def scrape_page_concurrent(
    summaries: list[dict],
    already_scraped: set[str],
    date_from: date,
    date_to: date,
) -> list[dict]:
    """
    Scrape all eligible listings on a page using a thread pool.

    Strict date filtering: a listing is scraped when its card date already
    falls inside [date_from, date_to], or – when the card has no date – it
    is fetched so the detail page can be checked, then dropped if it still
    can't be confirmed within the chosen quarter/year.
    Returns records in the same order as the input summaries.
    """
    to_fetch: list[dict] = []
    skipped       = 0
    out_of_range  = 0

    for summary in summaries:
        url = summary["URL"]

        if url in already_scraped:
            skipped += 1
            continue

        listing_date = parse_kruss_date(summary.get("Date", ""))
        if listing_date:
            if date_from <= listing_date <= date_to:
                to_fetch.append(summary)
            else:
                out_of_range += 1   # dated, but outside this quarter/year
        else:
            # No card date — scrape_property validates against the detail
            # page's date and returns None when out of range.
            to_fetch.append(summary)

    if skipped:
        log.info(f"  Skipped {skipped} already-scraped listing(s) on this page.")
    if out_of_range:
        log.info(
            f"  Skipped {out_of_range} listing(s) dated outside "
            f"{date_from} → {date_to}."
        )

    if not to_fetch:
        return []

    # Submit all detail fetches concurrently, preserve order with a dict
    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_idx = {
            executor.submit(scrape_property, summary, date_from, date_to): idx
            for idx, summary in enumerate(to_fetch)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception:
                log.exception(f"Worker error for {to_fetch[idx]['URL']}")

    # Return records in original order, dropping out-of-range results (None)
    return [results[i] for i in sorted(results) if results[i] is not None]


# ── Main loop ─────────────────────────────────────────────────────────────────
def run_scraper() -> None:

    # ── 1. Session choice ─────────────────────────────────────────────────────
    year, quarter, start_page = prompt_session_choice()

    date_from, date_to = quarter_date_range(year, quarter)
    lbl        = QUARTER_LABELS[quarter]
    output_csv = csv_filename(year, quarter)

    print()
    print("=" * 60)
    print(f"  Session    : {lbl} {year}")
    print(f"  Date range : {date_from}  →  {date_to}")
    print(f"  Output     : {output_csv}")
    print(f"  Workers    : {MAX_WORKERS} concurrent detail fetches")
    print(f"  Start page : {start_page}")
    print("=" * 60)
    print()

    # ── 2. Load already-scraped URLs ──────────────────────────────────────────
    ensure_csv_header(output_csv)
    already_scraped = load_scraped_urls_from_csv(output_csv)
    if already_scraped:
        log.info(f"Loaded {len(already_scraped)} already-scraped URL(s) from CSV.")

    # ── 3. Initialise state ───────────────────────────────────────────────────
    state: dict = {
        "year":                year,
        "quarter":             quarter,
        "last_completed_page": start_page - 1,
    }
    save_state(state)

    # ── 4. Page loop ──────────────────────────────────────────────────────────
    page_num          = start_page
    total_new         = 0
    consecutive_empty = 0
    run_start         = time.time()

    while True:
        log.info(f"─── Page {page_num} ────")
        summaries = get_listing_summaries_from_page(page_num)

        if not summaries:
            consecutive_empty += 1
            if consecutive_empty >= 4:
                log.info("end of results.")
                break
            page_num += 1
            continue

        consecutive_empty = 0

        # ── Concurrent detail-page fetches ────────────────────────────────────
        page_records = scrape_page_concurrent(
            summaries, already_scraped, date_from, date_to
        )

        # ── Log each result ───────────────────────────────────────────────────
        for r in page_records:
            already_scraped.add(r["URL"])
            log.info(
                f"  ✓ {r.get('Listing_ID','?'):15s} | "
                f"{r.get('Type','?'):13s} › {r.get('Category','?'):15s} | "
                f"{r.get('Name','?')[:50]}"
            )

        # ── Auto-save ─────────────────────────────────────────────────────────
        if page_records:
            append_records_to_csv(page_records, output_csv)
            total_new += len(page_records)

        elapsed = time.time() - run_start
        log.info(
            f"  Page {page_num} complete │ {len(page_records)} new │ "
            f"{total_new} total this run │ {elapsed:.0f}s elapsed"
        )

        # ── Persist resume state ──────────────────────────────────────────────
        state["last_completed_page"] = page_num
        save_state(state)

        page_num += 1

    # ── 5. Final summary ──────────────────────────────────────────────────────
    elapsed = time.time() - run_start
    print()
    print("=" * 60)
    print("  SCRAPING COMPLETE")
    print(f"  Quarter     : {lbl} {year}")
    print(f"  Output file : {output_csv}")
    print(f"  New records : {total_new}")
    print(f"  Total in file : {len(already_scraped)}")
    print(f"  Time elapsed : {elapsed:.1f}s")
    print("=" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_scraper()
