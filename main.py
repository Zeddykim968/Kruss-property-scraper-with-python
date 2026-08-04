import requests
import time
import json
from urllib.parse import urljoin
from datetime import date, datetime, timedelta
from bs4 import BeautifulSoup
import logging
import re
import csv
from collections import defaultdict

# logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# constants
Base_URL = "https://kruss.co.ke"
Search_URL = f"{Base_URL}/property-status/for-sale/"
Date_From = date(2026, 1, 1)
Date_To = date.today()
Request_Delay = 0.5  # seconds

KENYA_COUNTIES = [
    "Nairobi", "Mombasa", "Kisumu", "Nakuru", "Uasin Gishu", "Kiambu",
    "Machakos", "Kajiado", "Muranga", "Nyeri", "Meru", "Embu", "Kirinyaga",
    "Nyandarua", "Laikipia", "Samburu", "Isiolo", "Marsabit", "Mandera",
    "Wajir", "Garissa", "Tana River", "Kilifi", "Kwale", "Taita Taveta",
    "Lamu", "Trans Nzoia", "West Pokot", "Elgeyo Marakwet", "Nandi",
    "Baringo", "Kericho", "Bomet", "Narok", "Kisii", "Nyamira", "Migori",
    "Homa Bay", "Siaya", "Vihiga", "Kakamega", "Bungoma", "Busia",
    "Turkana", "Kitui", "Makueni"
]

OUTPUT_COLUMNS = [
    "Listing_ID", "Name", "Type", "Category", "Price", "Location", "County",
    "No. of Bedrooms", "No. of Bathrooms", "No. of Ensuite Bedrooms",
    "Date", "Floor_area_sqm", "Land_Size", "Elevator", "Parking",
    "Condition", "DSQ", "Floor_Number", "URL"
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}

STATE_FILE = "scraper_state.json"

session = requests.Session()
session.headers.update(HEADERS)


# ── HTTP helpers ────────────────────────────────────────────────────────────
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
                log.warning("  -> Permanent error - skipping URL")
                return None

        except requests.RequestException as e:
            log.warning(f"Request error fetching {url}: {e} (attempt {attempt}/{retries})")

        time.sleep(2 * attempt)
    return None


# ── Helpers ──────────────────────────────────────────────────────────────────
def match_county(*texts) -> str:
    """
    Match county against one or more pieces of address text. Pass both
    streetAddress and addressLocality - Location itself (e.g. "Nyali") is
    a neighborhood, not a county, so it won't match KENYA_COUNTIES on its
    own; streetAddress (e.g. "Mombasa, Kenya") usually will.
    """
    combined = " ".join(t for t in texts if t).lower()
    for county in KENYA_COUNTIES:
        if county.lower() in combined:
            return county
    return "Unknown"


# ── Search-page badges: Listing_ID / Type / Date ────────────────────────────
# "property-breadcrumbs" is the SAME class reused on three separate badges
# per card (ID, Type, Date) - not one span holding all three. The old code
# zipped 12 titles against a flat list that should've been 36 badges, which
# silently misaligned every card past the first few. Fixed by:
#   1) scoping to each card individually - walk up from the title to the
#      smallest ancestor containing exactly 3 of these badges, and
#   2) classifying each badge by the SHAPE of its value (a date, an
#      ID-looking code, or neither) rather than trusting a text prefix that
#      may not exist as literal text (label and value can be split across
#      nested elements, which is what broke the old regex).
ID_PREFIX_RE = re.compile(r"ID:\s*(.+)", re.IGNORECASE)
ID_FORMAT_RE = re.compile(r"^[A-Za-z]{1,4}\d+[A-Za-z0-9-]*$")
DATE_PREFIX_RE = re.compile(r"Last updated:\s*(.+)", re.IGNORECASE)
DATE_FORMAT_RE = re.compile(r"^[A-Za-z]+ \d{1,2},?\s*\d{4}$")


def find_card_badges(name_tag, expected_count: int = 3) -> list:
    """
    Walks up from a listing's title to the smallest ancestor containing
    exactly `expected_count` property-breadcrumbs badges. A bigger wrapper
    spanning multiple cards would contain a multiple of that count instead,
    so landing on exactly 3 is a strong signal we've found the card boundary.
    """
    for ancestor in name_tag.parents:
        found = ancestor.find_all("span", class_="property-breadcrumbs")
        if len(found) == expected_count:
            return found
        if len(found) > expected_count:
            break  # already past the card boundary, into a multi-card wrapper
    return []


def classify_badge(text: str) -> tuple[str, str]:
    """Returns (field_name, value) for a single badge's text."""
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

    return "Type", text


def extract_card_fields(name_tag) -> dict:
    """Returns {"Listing_ID": ..., "Type": ..., "Date": ...} for one listing card."""
    fields = {"Listing_ID": "", "Type": "", "Date": ""}
    title = name_tag.get_text(strip=True) if name_tag else "<unknown>"
    try:
        badges = find_card_badges(name_tag)
        if not badges:
            log.error(f"No 3-badge card boundary found for {title!r} - "
                      f"Listing_ID/Type/Date left blank for this listing.")
            return fields

        for badge in badges:
            text = badge.get_text(" ", strip=True)
            if not text:
                continue
            field, value = classify_badge(text)
            fields[field] = value

    except Exception:
        log.exception(f"Error extracting card badges for {title!r}")

    return fields


def parse_kruss_date(text: str):
    if not text:
        return None
    try:
        return datetime.strptime(text, "%B %d, %Y").date()
    except ValueError:
        return None


DETAIL_PAGE_DATE_RE = re.compile(r"^[A-Za-z]+ \d{1,2},?\s*\d{4}$")


def find_detail_page_date(soup: BeautifulSoup) -> str:
    """
    Fallback source for Date, used only if the search-page badge didn't
    produce one. Confirmed to appear as a bare 'Month DD, YYYY' span near
    the top of the detail page in your pasted HTML.
    """
    for span in soup.find_all("span"):
        text = span.get_text(strip=True)
        if text and DETAIL_PAGE_DATE_RE.match(text):
            return text
    return ""


# ── Quarter / year filtering ─────────────────────────────────────────────
QUARTER_MONTHS = {
    1: (1, 3),   # Jan - Mar
    2: (4, 6),   # Apr - Jun
    3: (7, 9),   # Jul - Sep
    4: (10, 12),  # Oct - Dec
}


def quarter_date_range(year: int, quarter: int) -> tuple[date, date]:
    """Returns (date_from, date_to) covering the given quarter (1-4) of a year."""
    if quarter not in QUARTER_MONTHS:
        raise ValueError("Quarter must be 1, 2, 3, or 4")
    start_month, end_month = QUARTER_MONTHS[quarter]
    date_from = date(year, start_month, 1)
    if end_month == 12:
        date_to = date(year, 12, 31)
    else:
        date_to = date(year, end_month + 1, 1) - timedelta(days=1)
    return date_from, date_to


def prompt_quarter_selection() -> tuple[date, date]:
    """Interactively asks for year + quarter and returns the matching date range."""
    year = int(input("Year to scrape (e.g. 2026): ").strip())
    quarter = int(input("Quarter to scrape (1=Jan-Mar, 2=Apr-Jun, 3=Jul-Sep, 4=Oct-Dec): ").strip())
    date_from, date_to = quarter_date_range(year, quarter)
    log.info(f"Scraping Q{quarter} {year}: {date_from} to {date_to}")
    return date_from, date_to


# ── Search page: collect URL + card-scoped fields together ─────────────────
def get_listing_summaries_from_search_page(page_num: int) -> list[dict]:
    """
    Returns one dict per listing card: URL, Listing_ID, Type, Date.
    These three fields only exist on the search page, not the JSON-LD on
    the detail page, so they need to be carried forward and merged in later.
    """
    url = Search_URL if page_num == 1 else f"{Search_URL}page/{page_num}/"
    body = fetch(url)
    if not body:
        return []

    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        log.exception(f"Failed to parse HTML for search page {page_num}")
        return []

    names = soup.find_all("h3", class_="rh-ultra-property-title")
    summaries = []
    for name_tag in names:
        a = name_tag.find("a")
        if not a or not a.get("href"):
            log.warning(f"Listing card missing a link - skipping: {name_tag.get_text(strip=True)!r}")
            continue

        card_fields = extract_card_fields(name_tag)
        summaries.append({
            "URL": urljoin(Base_URL, a["href"]),
            "Listing_ID": card_fields["Listing_ID"],
            "Type": card_fields["Type"],
            "Date": card_fields["Date"],
        })

    log.info(f"  Search page {page_num}: {len(summaries)} listings")
    return summaries


# ── JSON-LD extraction ───────────────────────────────────────────────────────
# JSON-LD is reliable for Name / Price / Location / County. Its
# additionalProperty array is NOT a reliable source for the rest - it only
# ever exposed Bedrooms / Bathrooms / Area Size across a diverse sample.
# The real HTML page has two better sources for everything else (see below).
def extract_json_ld_listing(soup: BeautifulSoup) -> dict | None:
    """Find the RealEstateListing node among a page's JSON-LD scripts."""
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

    listing = None
    for node in ld_graph:
        if isinstance(node, dict) and node.get("@type") == "RealEstateListing":
            listing = node
            break

    return listing


def apply_json_ld(soup: BeautifulSoup, record: dict) -> None:
    """Fill Name / Price / Location / County from JSON-LD."""
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
    street_address = address.get("streetAddress", "") if isinstance(address, dict) else ""
    locality = address.get("addressLocality", "") if isinstance(address, dict) else ""
    record["Location"] = locality or street_address
    record["County"] = match_county(street_address, locality)


# ── Visible meta-card row: Bedrooms / Bathrooms / Garage / Lot Size / etc. ──
# Confirmed from real HTML: <div class="rh_ultra_prop_card__meta"> blocks,
# each with a <span class="rh-ultra-meta-label"> and a
# <span class="rh_ultra_meta_box"><span class="figure">value</span></span>.
# "Lot Size" is ambiguous (floor area for buildings, parcel size for land) -
# disambiguated the same way "Area Size" was in JSON-LD.
META_LABEL_MAP = {
    "bedrooms": "No. of Bedrooms",
    "bathrooms": "No. of Bathrooms",
    "garage": "Parking",
    "garages": "Parking",
    "parking": "Parking",
    "ensuite": "No. of Ensuite Bedrooms",
    "en suite": "No. of Ensuite Bedrooms",
    "en-suite": "No. of Ensuite Bedrooms",
    "floor number": "Floor_Number",
    "floor": "Floor_Number",
}
AREA_LABELS = {"lot size", "floor size", "area size", "size"}


def apply_meta_cards(soup: BeautifulSoup, record: dict) -> set:
    """Parses the meta-card row. Returns the set of raw labels seen (for diagnostics)."""
    seen_labels = set()
    for meta in soup.find_all("div", class_="rh_ultra_prop_card__meta"):
        label_tag = meta.find("span", class_="rh-ultra-meta-label")
        if not label_tag:
            continue
        label = label_tag.get_text(strip=True)
        seen_labels.add(label)
        label_lower = label.lower()

        figure_tag = meta.find("span", class_="figure")
        unit_tag = meta.select_one(".rh_ultra_meta_box .label")
        value = figure_tag.get_text(strip=True) if figure_tag else ""
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

    return seen_labels


# ── Features list: Elevator / DSQ / Parking presence flags ─────────────────
# Confirmed from real HTML: "Elevator (lift)" lives here as an amenity tag,
# not a numeric meta field - which is exactly why it never showed up in the
# JSON-LD diagnostic. DSQ is very likely the same story; not yet confirmed
# with a real example, so add its actual wording here once you see one.
# Condition deliberately left out - no confirmed source for it yet, better
# to leave it blank than guess and risk wrong data.
FEATURE_FLAG_PATTERNS = {
    "Elevator": r"elevator|\blift\b",
    "DSQ": r"\bdsq\b|servant|domestic staff",
    "Parking": r"\bparking\b",
}


def apply_features(soup: BeautifulSoup, record: dict) -> list:
    """Parses the Features/amenities list and flags Yes for matching columns."""
    feature_texts = [
        a.get_text(strip=True)
        for a in soup.select("ul.rh_property__features li.rh_property__feature a")
    ]

    for field, pattern in FEATURE_FLAG_PATTERNS.items():
        if record.get(field):
            continue  # already filled, e.g. Parking from the Garage meta card
        for text in feature_texts:
            if re.search(pattern, text, re.IGNORECASE):
                record[field] = "Yes"
                break

    return feature_texts


# ── Type / Category taxonomy ────────────────────────────────────────────────
# Type is the top-level bucket; Category is the specific sub-type within it:
#   Land        -> Category is always "Land"
#   Residential -> Category in {Studio, Bungalow, Apartment, Mansionette, Townhouse, Villa}
#   Commercial  -> Category in {Office, Retail, Industrial, Warehousing, Social amenity}
# Classified from the listing Name (most reliable signal) plus the raw site
# type badge as a secondary hint. First matching pattern wins - order
# matters, so more specific terms are checked before generic ones.
# NOTE: "house" alone (not villa/bungalow/townhouse/mansionette) doesn't map
# to any of your six residential categories - those listings will come back
# unclassified and logged, so you can decide how you want them bucketed.
CATEGORY_KEYWORDS = [
    (r"\bstudios?\b", "Residential", "Studio"),
    (r"\bbungalows?\b", "Residential", "Bungalow"),
    (r"\bmansionettes?\b", "Residential", "Mansionette"),
    (r"\btown\s?houses?\b", "Residential", "Townhouse"),
    (r"\bvillas?\b", "Residential", "Villa"),
    (r"\b(apartments?|flats?)\b", "Residential", "Apartment"),
    (r"\boffices?\b", "Commercial", "Office"),
    (r"\bretail\b|\bshops?\b", "Commercial", "Retail"),
    (r"\bindustrial\b", "Commercial", "Industrial"),
    (r"\bwarehous\w*\b", "Commercial", "Warehousing"),
    (r"\b(social amenity|school|hospital|church|clinic)\b", "Commercial", "Social amenity"),
    (r"\b(land|plots?|acres?|hectares?)\b", "Land", "Land"),
]


def classify_property_type(name: str, raw_type_hint: str = "") -> tuple[str, str]:
    """
    Determines (Type, Category) from the listing name (primary) and the raw
    site type badge as a hint (secondary). Returns ("", "") if nothing
    matches, and logs a warning so unmatched listings are easy to find and
    add a rule for, instead of silently mis-tagging them.
    """
    combined = f"{name} {raw_type_hint}".lower()
    for pattern, prop_type, category in CATEGORY_KEYWORDS:
        if re.search(pattern, combined, re.IGNORECASE):
            return prop_type, category
    return "", ""


def scrape_properties(url: str, summary: dict | None = None) -> dict:
    record = {col: "" for col in OUTPUT_COLUMNS}
    record["URL"] = url
    if summary:
        for k, v in summary.items():
            if k in OUTPUT_COLUMNS:
                record[k] = v

    body = fetch(url)
    if not body:
        log.warning(f"Failed to fetch property page: {url}")
        return record

    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        log.exception(f"Failed to parse HTML for {url}")
        return record

    try:
        apply_json_ld(soup, record)  # Name / Price / Location / County
    except Exception:
        log.exception(f"JSON-LD extraction failed for {url}")

    try:
        apply_meta_cards(soup, record)  # Bedrooms / Bathrooms / Parking / Land_Size / Floor_area_sqm / etc.
    except Exception:
        log.exception(f"Meta-card extraction failed for {url}")

    try:
        apply_features(soup, record)  # Elevator / DSQ / Parking (fallback)
    except Exception:
        log.exception(f"Feature-list extraction failed for {url}")

    if not record.get("Date"):
        try:
            fallback_date = find_detail_page_date(soup)
            if fallback_date:
                record["Date"] = fallback_date
        except Exception:
            log.exception(f"Detail-page date fallback failed for {url}")

    try:
        # record["Type"] currently holds the site's raw badge text (e.g.
        # "Land/Plots") from the search-page summary - use it as a hint,
        # then overwrite with the bucketed Type + Category.
        prop_type, category = classify_property_type(record.get("Name", ""), record.get("Type", ""))
        if prop_type:
            record["Type"] = prop_type
            record["Category"] = category
        else:
            log.warning(f"Unclassified Type/Category for {url} (Name={record.get('Name')!r})")
    except Exception:
        log.exception(f"Type/Category classification failed for {url}")

    return record


def pick_diverse_sample(summaries: list[dict], per_type: int = 2) -> list[dict]:
    """Pick a few listings per property Type, instead of just the first N in page order."""
    by_type = defaultdict(list)
    for s in summaries:
        by_type[s["Type"]].append(s)
    sample = []
    for items in by_type.values():
        sample.extend(items[:per_type])
    return sample


# ── Diagnostic: discover the full vocabulary of meta-card labels + features ─
def diagnose_meta_and_features(urls: list[str]):
    """
    Run this across a handful of URLs spanning different listing types
    (apartment, land/plot, house) to see every meta-card label and feature
    name the site actually uses, so META_LABEL_MAP / FEATURE_FLAG_PATTERNS
    above can be completed with confirmed values instead of guesses -
    especially Ensuite, Floor Number, DSQ, and Condition, which haven't
    shown up in a real example yet.
    """
    all_labels = set()
    all_features = set()
    for url in urls:
        body = fetch(url)
        if not body:
            continue
        soup = BeautifulSoup(body, "lxml")
        for meta in soup.find_all("div", class_="rh_ultra_prop_card__meta"):
            label_tag = meta.find("span", class_="rh-ultra-meta-label")
            if label_tag:
                all_labels.add(label_tag.get_text(strip=True))
        for a in soup.select("ul.rh_property__features li.rh_property__feature a"):
            all_features.add(a.get_text(strip=True))

    log.info(f"Meta-card labels seen: {sorted(all_labels)}")
    log.info(f"Feature names seen: {sorted(all_features)}")


# ── Main pipeline ─────────────────────────────────────────────────────────
def scrape_all(
    max_pages: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    records = []
    date_from = date_from or Date_From
    date_to = date_to or Date_To
    page_num = 1

    while True:
        summaries = get_listing_summaries_from_search_page(page_num)
        if not summaries:
            log.info(f"No listings on page {page_num} - stopping.")
            break

        for summary in summaries:
            try:
                listing_date = parse_kruss_date(summary["Date"])
                if listing_date and not (date_from <= listing_date <= date_to):
                    continue
                if not listing_date:
                    log.warning(f"No usable Date for {summary['URL']} - "
                                f"including it anyway since it can't be filtered by quarter.")

                record = scrape_properties(summary["URL"], summary=summary)
                records.append(record)
                log.info(f"Parsed {record['Listing_ID']} - {record['Name']}")

            except Exception:
                log.exception(f"Unexpected error scraping {summary.get('URL')} - skipping this listing.")

        if max_pages and page_num >= max_pages:
            break
        page_num += 1

    return records


def save_csv(records: list[dict], filename: str = "kruss_listings.csv"):
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for r in records:
            writer.writerow({col: r.get(col, "") for col in OUTPUT_COLUMNS})
    log.info(f"Saved {len(records)} records to {filename}")


if __name__ == "__main__":
    # --- Step 1 (optional): explore meta-card labels + feature names across
    # listing types, to confirm Ensuite / Floor Number / DSQ / Condition
    # wording before relying on the guessed patterns above.
    all_summaries = (
        get_listing_summaries_from_search_page(1)
        + get_listing_summaries_from_search_page(2)
        + get_listing_summaries_from_search_page(3)
    )
    diverse_sample = pick_diverse_sample(all_summaries, per_type=2)
    log.info(f"Diagnosing {len(diverse_sample)} listings across types: "
             f"{set(s['Type'] for s in diverse_sample)}")
    diagnose_meta_and_features([s["URL"] for s in diverse_sample])

    # --- Step 2: pick a quarter + year, then run the real scrape
    #date_from, date_to = prompt_quarter_selection()
    #records = scrape_all(max_pages=1, date_from=date_from, date_to=date_to)  # small run first
    #for r in records:
    #    print(r)
    # save_csv(records)
