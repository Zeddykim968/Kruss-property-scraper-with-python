import requests
import time
import json
import os
import csv
from urllib.parse import urljoin
from datetime import date, datetime, timedelta
from bs4 import BeautifulSoup
import logging
import re
from collections import defaultdict

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
Base_URL      = "https://kruss.co.ke"
Search_URL    = f"{Base_URL}/property-status/for-sale/"
Request_Delay = 0.5   # seconds between requests

STATE_FILE    = "scraper_state.json"   # tracks resume position + scraped URLs
OUTPUT_DIR    = "output"               # folder for all CSV files

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

session = requests.Session()
session.headers.update(HEADERS)

os.makedirs(OUTPUT_DIR, exist_ok=True)


# ── Quarter helpers ──────────────────────────────────────────────────────────
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
    if end_month == 12:
        date_to = date(year, 12, 31)
    else:
        date_to = date(year, end_month + 1, 1) - timedelta(days=1)
    return date_from, date_to


def csv_filename(year: int, quarter: int) -> str:
    return os.path.join(OUTPUT_DIR, f"kruss_Q{quarter}_{year}.csv")


# ── State (resume) management ─────────────────────────────────────────────────
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
    """Return the set of URLs already saved in a CSV file."""
    urls: set[str] = set()
    if not os.path.exists(filepath):
        return urls
    try:
        with open(filepath, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("URL"):
                    urls.add(row["URL"])
    except IOError:
        pass
    return urls


def ensure_csv_header(filepath: str) -> None:
    """Write CSV header if the file doesn't exist yet."""
    if not os.path.exists(filepath):
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
            writer.writeheader()


def append_records_to_csv(records: list[dict], filepath: str) -> None:
    """Append rows to an existing CSV (header must already exist)."""
    if not records:
        return
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        for r in records:
            writer.writerow({col: r.get(col, "") for col in OUTPUT_COLUMNS})
    log.info(f"  Auto-saved {len(records)} record(s) → {filepath}")


# ── Quarter / session selection ───────────────────────────────────────────────
def prompt_session_choice() -> tuple[int, int, int]:
    """
    Asks the user whether to resume the last session or start a new one.
    Returns (year, quarter, start_page).
    """
    state = load_state()

    if state:
        y   = state.get("year",     "?")
        q   = state.get("quarter",  "?")
        pg  = state.get("last_completed_page", 0)
        lbl = QUARTER_LABELS.get(q, f"Q{q}")
        print()
        print("=" * 60)
        print("  PREVIOUS SESSION FOUND")
        print(f"  Year    : {y}")
        print(f"  Quarter : {lbl}")
        print(f"  Last completed page : {pg}")
        print("=" * 60)
        while True:
            choice = input("\n  [R] Resume last session   [N] Start new session : ").strip().upper()
            if choice in ("R", "N"):
                break
            print("  Please enter R or N.")

        if choice == "R":
            log.info(f"Resuming {lbl} {y} from page {pg + 1}")
            return int(y), int(q), int(pg) + 1

    # ── New session: pick year ────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  NEW SCRAPING SESSION")
    print("=" * 60)

    while True:
        raw = input("\n  Enter the YEAR to scrape (e.g. 2026) : ").strip()
        if raw.isdigit() and 2000 <= int(raw) <= 2100:
            year = int(raw)
            break
        print("  Please enter a valid 4-digit year between 2000 and 2100.")

    # ── Pick quarter ──────────────────────────────────────────────────────────
    print()
    print("  Which quarter would you like to scrape?")
    for num, label in QUARTER_LABELS.items():
        print(f"    {num} → {label}")

    while True:
        raw = input("\n  Enter quarter number (1, 2, 3 or 4) : ").strip()
        if raw.isdigit() and int(raw) in QUARTER_LABELS:
            quarter = int(raw)
            break
        print("  Please enter 1, 2, 3, or 4.")

    log.info(f"Starting new session: {QUARTER_LABELS[quarter]} {year}")
    return year, quarter, 1


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def fetch(url: str, retries: int = 3) -> str | None:
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
            time.sleep(Request_Delay)
            return resp.text

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            log.warning(f"HTTP {status} fetching {url} (attempt {attempt}/{retries})")
            if status in (403, 404, 410):
                return None

        except requests.RequestException as e:
            log.warning(f"Request error: {e} (attempt {attempt}/{retries})")

        time.sleep(2 * attempt)
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────
def match_county(*texts: str) -> str:
    combined = " ".join(t for t in texts if t).lower()
    for county in KENYA_COUNTIES:
        if county.lower() in combined:
            return county
    return "Unknown"


# ── Search-page badge parsing (Listing_ID / raw-Type / Date) ─────────────────
ID_PREFIX_RE   = re.compile(r"ID:\s*(.+)",           re.IGNORECASE)
ID_FORMAT_RE   = re.compile(r"^[A-Za-z]{1,4}\d+[A-Za-z0-9-]*$")
DATE_PREFIX_RE = re.compile(r"Last updated:\s*(.+)", re.IGNORECASE)
DATE_FORMAT_RE = re.compile(r"^[A-Za-z]+ \d{1,2},?\s*\d{4}$")


def find_card_badges(name_tag, expected_count: int = 3) -> list:
    for ancestor in name_tag.parents:
        found = ancestor.find_all("span", class_="property-breadcrumbs")
        if len(found) == expected_count:
            return found
        if len(found) > expected_count:
            break
    return []


def classify_badge(text: str) -> tuple[str, str]:
    id_match = ID_PREFIX_RE.search(text)
    if id_match:
        return "Listing_ID", id_match.group(1).strip()
    if ID_FORMAT_RE.match(text):
        return "Listing_ID", text

    date_match = DATE_PREFIX_RE.search(text)
    if date_match:
        return "Date", date_match.group(1).strip()
    if DATE_FORMAT_RE.match(text):
        return "Date", text

    return "raw_type", text


def extract_card_fields(name_tag) -> dict:
    fields = {"Listing_ID": "", "raw_type": "", "Date": ""}
    title  = name_tag.get_text(strip=True) if name_tag else "<unknown>"
    try:
        badges = find_card_badges(name_tag)
        if not badges:
            log.warning(f"No badge boundary for {title!r}")
            return fields
        for badge in badges:
            text = badge.get_text(" ", strip=True)
            if not text:
                continue
            field, value = classify_badge(text)
            fields[field] = value
    except Exception:
        log.exception(f"Badge error for {title!r}")
    return fields


def parse_kruss_date(text: str) -> date | None:
    if not text:
        return None
    for fmt in ("%B %d, %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None


DETAIL_DATE_RE = re.compile(r"^[A-Za-z]+ \d{1,2},?\s*\d{4}$")


def find_detail_page_date(soup: BeautifulSoup) -> str:
    for span in soup.find_all("span"):
        text = span.get_text(strip=True)
        if text and DETAIL_DATE_RE.match(text):
            return text
    return ""


# ── Type / Category classification ───────────────────────────────────────────
# Type   → Residential | Land | Commercial
# Category → Studio | Bungalow | Apartment | Mansionette | Townhouse | Villa |
#            House | Land | Office | Retail | Industrial | Warehousing | Social Amenity
#
# "House" (incl. bare "house" matches) → Townhouse per user request

CATEGORY_KEYWORDS = [
    # ── Residential ──────────────────────────────────────────────────────────
    (r"\bstudios?\b",                                 "Residential",  "Studio"),
    (r"\bbungalows?\b",                               "Residential",  "Bungalow"),
    (r"\bmansionettes?\b",                            "Residential",  "Mansionette"),
    (r"\btown[\s-]?houses?\b",                        "Residential",  "Townhouse"),
    (r"\bvillas?\b",                                  "Residential",  "Villa"),
    (r"\b(apartments?|flats?)\b",                     "Residential",  "Apartment"),
    # "house" alone → Townhouse as requested
    (r"\bhouses?\b",                                  "Residential",  "Townhouse"),
    # ── Commercial ───────────────────────────────────────────────────────────
    (r"\boffices?\b",                                 "Commercial",   "Office"),
    (r"\bretail\b|\bshops?\b",                        "Commercial",   "Retail"),
    (r"\bindustrial\b",                               "Commercial",   "Industrial"),
    (r"\bwarehous\w*\b",                              "Commercial",   "Warehousing"),
    (r"\b(social\s+amenity|school|hospital|church|clinic)\b",
                                                      "Commercial",   "Social Amenity"),
    # ── Land ─────────────────────────────────────────────────────────────────
    (r"\b(land|plots?|acres?|hectares?)\b",           "Land",         "Land"),
]


def classify_property(name: str, raw_type_hint: str = "") -> tuple[str, str]:
    """
    Returns (Type, Category).
    Type is one of: Residential | Land | Commercial
    Category is the specific sub-type.
    """
    combined = f"{name} {raw_type_hint}".lower()
    for pattern, prop_type, category in CATEGORY_KEYWORDS:
        if re.search(pattern, combined, re.IGNORECASE):
            return prop_type, category
    log.warning(f"Unclassified property: name={name!r} hint={raw_type_hint!r}")
    return "", ""


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
    listing = extract_json_ld_listing(soup)
    if not listing:
        log.warning("No RealEstateListing JSON-LD found")
        return

    record["Name"] = listing.get("name", "") or ""

    offers = listing.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    record["Price"] = offers.get("price", "") if isinstance(offers, dict) else ""

    address = listing.get("address", {})
    if isinstance(address, list):
        address = address[0] if address else {}
    street = address.get("streetAddress", "") if isinstance(address, dict) else ""
    locality = address.get("addressLocality", "") if isinstance(address, dict) else ""
    record["Location"] = locality or street
    record["County"]   = match_county(street, locality)


# ── Meta-card row ─────────────────────────────────────────────────────────────
META_LABEL_MAP = {
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
AREA_LABELS = {"lot size", "floor size", "area size", "size"}


def apply_meta_cards(soup: BeautifulSoup, record: dict) -> None:
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
            prop_type = (record.get("Type") or "").lower()
            if "land" in prop_type or "plot" in prop_type:
                record["Land_Size"] = value
            else:
                record["Floor_area_sqm"] = value
            continue

        mapped = META_LABEL_MAP.get(label_lower)
        if mapped:
            record[mapped] = value


# ── Features / amenities ──────────────────────────────────────────────────────
FEATURE_FLAG_PATTERNS = {
    "Elevator": r"elevator|\blift\b",
    "DSQ":      r"\bdsq\b|servant|domestic\s+staff",
    "Parking":  r"\bparking\b",
}


def apply_features(soup: BeautifulSoup, record: dict) -> None:
    feature_texts = [
        a.get_text(strip=True)
        for a in soup.select("ul.rh_property__features li.rh_property__feature a")
    ]
    for field, pattern in FEATURE_FLAG_PATTERNS.items():
        if record.get(field):
            continue
        for text in feature_texts:
            if re.search(pattern, text, re.IGNORECASE):
                record[field] = "Yes"
                break


# ── Search page: collect listing summaries ────────────────────────────────────
def get_listing_summaries_from_page(page_num: int) -> list[dict]:
    url  = Search_URL if page_num == 1 else f"{Search_URL}page/{page_num}/"
    body = fetch(url)
    if not body:
        return []

    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        log.exception(f"HTML parse failed for search page {page_num}")
        return []

    names     = soup.find_all("h3", class_="rh-ultra-property-title")
    summaries = []
    for name_tag in names:
        a = name_tag.find("a")
        if not a or not a.get("href"):
            continue
        card = extract_card_fields(name_tag)
        summaries.append({
            "URL":        urljoin(Base_URL, a["href"]),
            "Listing_ID": card["Listing_ID"],
            "raw_type":   card["raw_type"],
            "Date":       card["Date"],
        })

    log.info(f"  Page {page_num}: {len(summaries)} listings found")
    return summaries


# ── Detail page scraper ───────────────────────────────────────────────────────
def scrape_property(url: str, summary: dict) -> dict:
    record        = {col: "" for col in OUTPUT_COLUMNS}
    record["URL"] = url

    # Carry over fields from the search-page summary
    record["Listing_ID"] = summary.get("Listing_ID", "")
    record["Date"]       = summary.get("Date", "")
    raw_type_hint        = summary.get("raw_type", "")

    body = fetch(url)
    if not body:
        log.warning(f"Failed to fetch: {url}")
        return record

    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        log.exception(f"HTML parse failed: {url}")
        return record

    try:
        apply_json_ld(soup, record)
    except Exception:
        log.exception(f"JSON-LD failed: {url}")

    try:
        apply_meta_cards(soup, record)
    except Exception:
        log.exception(f"Meta-card failed: {url}")

    try:
        apply_features(soup, record)
    except Exception:
        log.exception(f"Features failed: {url}")

    # Date fallback
    if not record.get("Date"):
        try:
            record["Date"] = find_detail_page_date(soup)
        except Exception:
            pass

    # Type + Category classification
    try:
        prop_type, category = classify_property(record.get("Name", ""), raw_type_hint)
        record["Type"]     = prop_type
        record["Category"] = category
    except Exception:
        log.exception(f"Classification failed: {url}")

    return record


# ── Main scraping loop ────────────────────────────────────────────────────────
def run_scraper() -> None:
    # ── 1. Session / quarter selection ───────────────────────────────────────
    year, quarter, start_page = prompt_session_choice()

    date_from, date_to = quarter_date_range(year, quarter)
    lbl                = QUARTER_LABELS[quarter]
    output_csv         = csv_filename(year, quarter)

    print()
    print("=" * 60)
    print(f"  Session  : {lbl} {year}")
    print(f"  Date range: {date_from}  →  {date_to}")
    print(f"  Output   : {output_csv}")
    print(f"  Starting at page {start_page}")
    print("=" * 60)
    print()

    # ── 2. Load already-scraped URLs ──────────────────────────────────────────
    ensure_csv_header(output_csv)
    already_scraped = load_scraped_urls_from_csv(output_csv)
    if already_scraped:
        log.info(f"  Loaded {len(already_scraped)} already-scraped URLs from {output_csv}")

    # ── 3. Initialise / update state ──────────────────────────────────────────
    state = {
        "year":                 year,
        "quarter":              quarter,
        "last_completed_page":  start_page - 1,
    }
    save_state(state)

    # ── 4. Page-by-page loop ──────────────────────────────────────────────────
    page_num         = start_page
    total_saved      = len(already_scraped)
    total_skipped    = 0
    consecutive_empty = 0

    while True:
        log.info(f"Fetching page {page_num} …")
        summaries = get_listing_summaries_from_page(page_num)

        if not summaries:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                log.info("Two consecutive empty pages — reached the end of results.")
                break
            page_num += 1
            continue

        consecutive_empty = 0
        page_records      = []

        for summary in summaries:
            url = summary["URL"]

            # ── Skip already-scraped listings ─────────────────────────────────
            if url in already_scraped:
                log.info(f"  Skipping (already scraped): {url}")
                total_skipped += 1
                continue

            # ── Date filter ───────────────────────────────────────────────────
            listing_date = parse_kruss_date(summary["Date"])
            if listing_date and not (date_from <= listing_date <= date_to):
                log.info(f"  Out of range ({listing_date}): {url}")
                continue
            if not listing_date:
                log.warning(f"  No parseable date for {url} — including anyway.")

            # ── Scrape detail page ────────────────────────────────────────────
            try:
                record = scrape_property(url, summary)
                already_scraped.add(url)
                page_records.append(record)
                log.info(
                    f"  ✓ {record.get('Listing_ID','?')} | "
                    f"{record.get('Name','?')} | "
                    f"{record.get('Type','?')} – {record.get('Category','?')}"
                )
            except Exception:
                log.exception(f"  Unexpected error for {url} — skipping.")

        # ── Auto-save after every page ────────────────────────────────────────
        if page_records:
            append_records_to_csv(page_records, output_csv)
            total_saved += len(page_records)
            log.info(
                f"  Page {page_num} done: {len(page_records)} new record(s) saved "
                f"({total_saved} total, {total_skipped} skipped)"
            )
        else:
            log.info(f"  Page {page_num}: nothing new to save.")

        # ── Persist resume state ───────────────────────────────────────────────
        state["last_completed_page"] = page_num
        save_state(state)

        page_num += 1

    # ── 5. Summary ────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  SCRAPING COMPLETE")
    print(f"  Quarter    : {lbl} {year}")
    print(f"  Output file: {output_csv}")
    print(f"  Records saved (this run): {total_saved - len(already_scraped) + len(page_records) if page_records else total_saved}")
    print(f"  Total in file: {total_saved}")
    print(f"  Listings skipped (already done): {total_skipped}")
    print("=" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_scraper()
