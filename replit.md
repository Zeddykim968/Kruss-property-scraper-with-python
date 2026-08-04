# Kruss Real Estate Scraper

A Python web scraper that collects property listings from [kruss.co.ke](https://kruss.co.ke) and saves them to quarterly CSV files.

## How to Run

Press **Run** (or start the **Run Scraper** workflow).

The scraper is interactive — it will guide you through:
1. **Resume or start new** — if a previous session was interrupted, you can pick up where it left off.
2. **Year** — e.g. `2026`
3. **Quarter** — choose 1–4:
   - `1` → Q1 (Jan – Mar)
   - `2` → Q2 (Apr – Jun)
   - `3` → Q3 (Jul – Sep)
   - `4` → Q4 (Oct – Dec)

## Output

CSV files are saved to the `output/` folder, named `kruss_Q<N>_<YEAR>.csv` (e.g. `kruss_Q2_2026.csv`).

The scraper **auto-saves after every page**, so you never lose progress if it's stopped.

## Columns Collected

| Column | Description |
|--------|-------------|
| Listing_ID | Unique property ID from the site |
| Name | Property name/title |
| Type | Residential / Land / Commercial |
| Category | Studio, Apartment, Bungalow, Mansionette, Townhouse, Villa, Land, Office, Retail, Industrial, Warehousing, Social Amenity |
| Price | Asking price |
| Location | Neighbourhood / area |
| County | Kenya county |
| No. of Bedrooms | |
| No. of Bathrooms | |
| No. of Ensuite Bedrooms | |
| Date | Listing date |
| Floor_area_sqm | Floor area (residential/commercial) |
| Land_Size | Plot size (land listings) |
| Elevator | Yes if present |
| Parking | Number of spaces or Yes |
| Condition | Property condition |
| DSQ | Yes if servant quarters present |
| Floor_Number | Floor level (apartments) |
| URL | Source listing URL |

## Resume / Skip Logic

- The scraper saves its position to `scraper_state.json` after each page.
- It loads already-scraped URLs from the output CSV to skip duplicates.
- On next run, choose **R** to resume exactly where it stopped.

## Stack

- Python 3.12
- `requests`, `beautifulsoup4`, `lxml`

## User preferences

- "House" listings are classified as **Townhouse** (Residential category).
- Quarter outputs go to separate CSV files in `output/`.
