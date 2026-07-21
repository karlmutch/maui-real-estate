# maui-real-estate

Tools for downloading, filtering, and analyzing Maui County property and sales data for selected Tax Map Key (TMK) parcels.

## Prerequisites

- Python 3.12+
- [direnv](https://direnv.net/) (optional; activates the project virtualenv automatically)

Maui country public data can be found at, https://www.mauicounty.gov/1032/Download-Public-Information.  Detailed instructions can be found in the data sections of this document.

## Setup

From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If you use direnv, `cd` into the project and allow the `.envrc` (it sources `.venv/bin/activate`).

## County data

Download and extract the county source files before running the scripts.

### Property and parcel full extract

See [data/county-property-and-parcel-full/README.md](data/county-property-and-parcel-full/README.md).

```sh
cd data/county-property-and-parcel-full
rm *.txt
curl -o full-extract.zip https://www.mauicounty.gov/DocumentCenter/View/8911/RPT-All-Full-File-Extracts-as-of-4062026
unzip full-extract.zip
```

Or use the helper script to unzip all `.zip` files in the current directory:

```sh
cd data/county-property-and-parcel-full
../../scripts/unzip-all.sh
```

### Sales data

See [data/county-sales-data/README.md](data/county-sales-data/README.md).

```sh
cd data/county-sales-data
curl --output sales.pdf https://www.mauicounty.gov/DocumentCenter/View/8069/RPT-Sales-Data-Information-File
curl --output sales.zip https://www.mauicounty.gov/DocumentCenter/View/8070/RPT-Sales-Data-File-
unzip sales.zip
rm sales.zip
```

### Ownership data

See [data/ownership-data](data/ownership-data/RPT-Ownership-Data-Description.pdf)

```sh
curl --output RPT-Ownership-Data.zip https://www.mauicounty.gov/DocumentCenter/View/8072/RPT-Ownership-Data
mv RPT-Ownership-Data RPT-Ownership-Data.zip
wget https://www.mauicounty.gov/DocumentCenter/View/8071/RPT-Ownership-Data-Description
mv RPT-Ownership-Data-Description RPT-Ownership-Data-Description.pdf
unzip RPT-Ownership-Data.zip
```

## TMK selection

Create or edit a `.tmks` file under `data/` listing the TMK keys you want to analyze (one per line). The filename stem becomes the output prefix for filtered files. For example, [data/maui-kamaole.tmks](data/maui-kamaole.tmks) contains three Maui Kamaole parcels:

```
239004082
239004143
239004144
```

Nine-digit keys use prefix matching so all CPR (condo unit) variants for that parcel are included.

## Pipeline

Run the scripts in order from the repository root.

### 1. Filter county data by TMK

`select_by_tmk.py` reads TMK keys from a `.tmks` file, scans county source files in `data/county-property-and-parcel-full/` and `data/county-sales-data/`, and writes filtered outputs next to each source file:

- Per TMK: `{tmks-stem}-{source-stem}-{tmk}{ext}` (e.g. `maui-kamaole-fullasmt26-239004082.txt`)
- Combined: `{tmks-stem}-{source-stem}-selected{ext}` (all TMKs merged in `.tmks` file order)

Each output file includes a PDF-derived CSV header row followed by sliced data rows.

```sh
python scripts/select_by_tmk.py --tmks data/maui-kamaole.tmks
```

Options:

| Flag | Default | Description |
| ---- | ------- | ----------- |
| `--tmks` | *(required)* | Path to TMK key list (one per line); output files are prefixed with this file's stem |
| `--data-root` | `data` | Root directory containing county data subdirectories |
| `--dry-run` | off | Print planned outputs and match counts without writing |
| `-v`, `--verbose` | off | Enable debug logging |

Examples:

```sh
# Maui Kamaole condos (239004082, 239004143, 239004144)
python scripts/select_by_tmk.py --tmks data/maui-kamaole.tmks

# Palms at Wailea (2100808200000)
python scripts/select_by_tmk.py --tmks data/palms-at-wailea.tmks

# Wailea Palms (221008083)
python scripts/select_by_tmk.py --tmks data/wailea-palms.tmks

# Dry run with explicit data root
python scripts/select_by_tmk.py \
  --tmks data/maui-kamaole.tmks \
  --data-root data \
  --dry-run
```

### 2. Analyze non-Hawaii ownership residency

`ownership_timeline.py` consumes the per-TMK outputs from step 1 and classifies current owners from `fullownr` mailing addresses as Hawaii, non-Hawaii, or unknown. Sales records supply **transfer dates only** — not historical owner names or addresses.

**Important:** Historical HI/non-HI mix in the annual and timeline CSVs uses a **proxy**: after each unit's first recorded transfer, the script applies that unit's **current** `fullownr` mailing residency to all later dates. Flat year-over-year percentages usually mean the proxy is static, not that ownership residency was unchanged. See [docs/ownership-residency-proxy.plan.md](docs/ownership-residency-proxy.plan.md).

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--tmks` | *(required)* | Path to TMK key list |
| `--data-root` | `data` | Root directory containing county data subdirectories |
| `--output-dir` | `data/ownership-timeline` | Directory for output CSV files |
| `--infer-coowner-residency` | off | Deprecated; TMK-level address fallback is always enabled |
| `--dry-run` | off | Print summary without writing files |
| `-v`, `--verbose` | off | Enable debug logging |

Example:

```sh
python scripts/ownership_timeline.py \
  --tmks data/maui-kamaole.tmks \
  --data-root data \
  --output-dir data/ownership-timeline
```

#### Outputs

Written to `data/ownership-timeline/`:

| File | Description |
|------|-------------|
| `non-hi-ownership-timeline.csv` | Proxy residency mix and resolution progress by period (per TMK and collective) |
| `non-hi-ownership-summary.csv` | Current proxy snapshot per TMK and collective |
| `non-hi-ownership-annual.csv` | Year-end proxy residency and resolution rollups from 2001 onward |
| `non-hi-ownership-units-{tmk}.csv` | Per-unit residency detail with confidence and entity flags |
| `non-hi-ownership-unknown-residency.csv` | Owners with unknown residency and mailing address fields |

Key annual columns:

- `proxy_resolved_pct` — share of units past first transfer (resolution progress)
- `hi_pct` / `non_hi_pct` — static proxy mix after full resolution
- `newly_resolved_units` — units entering the resolved pool that year
- `first_transfer_count` — first-time transfer events (not HI migration)

The script also prints a summary to stdout with a proxy disclaimer.

### 3. Bill 9 event study

`bill9_event_study.py` produces a full empirical report evaluating market behavior around Maui County Bill 9 milestones for the selected TMK portfolio (2019–present). It reuses county transfer and residency data from step 1 and writes outputs to `data/bill9-event-study/`.

**Interpretation:** The report presents observed correlations around policy milestones. It does **not** claim Bill 9 caused price or ownership changes. Residency uses current `fullownr` mailing addresses as a proxy — not buyer residency at each sale.

```sh
python scripts/bill9_event_study.py --tmks data/maui-kamaole.tmks

# Optional difference-in-differences controls (requires select_by_tmk first)
python scripts/bill9_event_study.py \
  --tmks data/maui-kamaole.tmks \
  --controls data/wailea-palms.tmks data/palms-at-wailea.tmks
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--tmks` | `data/maui-kamaole.tmks` | Path to TMK key list |
| `--controls` | *(none)* | Optional control complex `.tmks` files for difference-in-differences |
| `--data-root` | `data` | Root directory containing county data subdirectories |
| `--output-dir` | `data/bill9-event-study` | Directory for CSV, chart, and report outputs |
| `--policy` | *(built-in)* | Optional YAML overriding Bill 9 milestone dates |
| `--dry-run` | off | Load and analyze without writing files |
| `-v`, `--verbose` | off | Enable debug logging |

Policy milestone dates are configurable in [`maui_market/config/bill9_policy.yaml`](maui_market/config/bill9_policy.yaml).

The report centers on **counterfactual market value analysis**: it fits Maui Kamaole's pre-announcement price trend and compares expected vs observed medians, with optional DiD against control complexes.

#### Outputs

Written to `data/bill9-event-study/`:

| File | Description |
|------|-------------|
| `bill9-event-study-summary.csv` | Top-line and counterfactual headline metrics |
| `bill9-counterfactual.csv` | Monthly expected vs observed medians (linear, log-linear, optional DiD) |
| `bill9-monthly-market.csv` | Monthly transfers, sales, prices, volume, price index |
| `bill9-monthly-residency.csv` | Monthly buyer residency counts and percentages |
| `bill9-repeat-sales.csv` | Repeat-sale pairs with appreciation |
| `bill9-comparable-units.csv` | Median prices by TMK, building, and value bucket |
| `bill9-turnover.csv` | Annual and policy-window turnover and liquidity |
| `bill9-price-distribution.csv` | Price quartiles by policy period |
| `bill9-statistics.csv` | Mann-Whitney and OLS test results |
| `bill9-event-study-report.md` | Streamlined narrative report with counterfactual section |
| `charts/*.png` | Up to six charts (ITS actual vs expected, median price, volume, residency, index, optional DiD) |

### 4. Maui Kamaole market listings

`real-estate-pull.py` scrapes active Maui Kamaole condo listings from Redfin, stores snapshot history in SQLite, compares each pull to the prior snapshot, and generates a markdown report plus trend charts.

**Prerequisites:** Google Chrome installed. Selenium uses ChromeDriver via `webdriver-manager`.

```sh
pip install -r requirements.txt

# First pull: scrape listings (headed Chrome opens by default)
python scripts/real-estate-pull.py --refresh

# Later runs: report from latest snapshot (no network)
python scripts/real-estate-pull.py

# Weekly refresh + updated report/charts
python scripts/real-estate-pull.py --refresh
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--complex` | `maui_kamaole` | Complex config slug under `maui_market/config/` |
| `--refresh` | off | Scrape fresh listings and store a new snapshot |
| `--headless` | off | Run Chrome headless (default is headed browser) |
| `--db` | `maui_market/history.db` | SQLite database path |
| `--dry-run` | off | Scrape/parse and print report without writing files |
| `-v`, `--verbose` | off | Enable debug logging |

#### Market outputs

Written under `maui_market/` (generated files are gitignored):

| Path | Description |
|------|-------------|
| `history.db` | SQLite snapshot history |
| `exports/maui_kamaole_active_YYYY_MM_DD.csv` | Dated active listing export |
| `reports/maui_kamaole_report_YYYY_MM_DD.md` | Markdown market report |
| `reports/charts_YYYY_MM_DD/` | Inventory, median price, and $/sqft trend PNGs |

Each report includes inventory count, median asking price, median $/sqft, new listings, price reductions, pending sales, and removed listings since the prior snapshot.

### 5. BOC mortgage lookup

`mortgage_lookup.py` loads condo units from county `fullpardat` extracts, queries the [Hawaii Bureau of Conveyances RecordEASE](https://bocdataext.hi.wcicloud.com/login.aspx) site by formatted TMK per unit, and classifies whether recorded mortgage instruments appear open or released.

**Prerequisites:** Run `select_by_tmk.py` first. You need a BOC account (free to search; document previews are watermarked). Google Chrome is required for Selenium.

```sh
# Index units only (no network)
python scripts/mortgage_lookup.py --tmks data/maui-kamaole.tmks --dry-run

# Full lookup (~15–20 minutes for ~300 Maui Kamaole units)
export BOC_USERNAME="you@example.com"
export BOC_PASSWORD="your-password"
python scripts/mortgage_lookup.py --tmks data/maui-kamaole.tmks

# Smoke test first 5 units
python scripts/mortgage_lookup.py --tmks data/maui-kamaole.tmks --limit 5

# Resume interrupted run
python scripts/mortgage_lookup.py --tmks data/maui-kamaole.tmks --resume
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--tmks` | *(required)* | Path to TMK key list |
| `--data-root` | `data` | Root directory containing county data subdirectories |
| `--output-dir` | `data/mortgage-qualified` | Directory for output CSV files |
| `--username` / `--password` | `BOC_USERNAME` / `BOC_PASSWORD` env | BOC login credentials |
| `--headless` | off | Run Chrome headless (default is headed browser) |
| `--limit` | none | Maximum units to query |
| `--resume` | off | Skip units with cached documents; re-fetch units missing from cache |
| `--delay` | `2.0 4.0` | Min/max seconds between BOC searches |
| `--dry-run` | off | Build unit index only; no network |
| `-v`, `--verbose` | off | Enable debug logging |

BOC searches by **condominium name and unit** (for example `MAUI KAMAOLE PHASE III` + `G101`), not street address. Condominium names are parsed from county `fulllegal26` `LEGAL DESCRIPTION` text: explicit phase labels are used when they appear on multiple units (or for the same building), and remaining units on that building inherit the dominant phase. A-building units on TMK `239004143` have no phase suffix in county data and resolve to `MAUI KAMAOLE`. Street addresses from `fullpardat` are included in the output for reference.

When a Maui Kamaole search returns no documents (or errors), the scraper retries using the same name chain for every unit: the county-derived name first, then `MAUI KAMAOLE PHASE III`, `MAUI KAMAOLE PHASE II`, `MAUI KAMAOLE (LC)`, and `MAUI KAMAOLE (RS)` (skipping any name already tried). Failed searches are not written to the cache (including exceptions and empty results), so `--resume` will try those units again later. Only units that return one or more documents are cached.

`mortgage_status` is a heuristic based on instrument codes (`M`, `MFS` for mortgages; `R` for releases). Compare raw document rows before drawing conclusions.

#### Mortgage outputs

Written to `data/mortgage-qualified/`:

| File | Description |
|------|-------------|
| `{prefix}-units.csv` | Unit index with TMK, CPR, BOC TMK, and street address |
| `{prefix}-documents.csv` | All BOC instrument rows returned per unit |
| `{prefix}-mortgage-status.csv` | One summary row per unit with mortgage status |
| `{prefix}-mortgage-summary.csv` | Portfolio and per-TMK status rollups |
| `.cache/{prefix}-boc-documents.json` | Checkpoint cache for `--resume` (gitignored) |

## Project layout

```
data/
  *.tmks                            # TMK key lists (e.g. maui-kamaole.tmks)
  county-property-and-parcel-full/  # County fixed-width extracts + metadata PDFs
  county-sales-data/                # County sales CSV + metadata PDF
  ownership-timeline/               # Generated analysis outputs
  mortgage-qualified/               # Generated BOC mortgage lookup outputs
maui_market/
  config/                           # Per-complex YAML configs
  exports/                          # Dated listing CSV exports (generated)
  reports/                          # Reports and charts (generated)
  history.db                        # SQLite snapshot DB (generated)
scripts/
  county_metadata.py                # PDF schema parsing and TMK matching helpers
  select_by_tmk.py                  # TMK data filter
  ownership_timeline.py             # Non-HI residency analysis
  real-estate-pull.py               # Maui condo market intelligence CLI
  mortgage_lookup.py                # BOC recorded mortgage lookup by TMK
  unzip-all.sh                      # Unzip all .zip files in the current directory
docs/
  tmk-data-selector-script.plan.md  # Design plan for select_by_tmk.py
  unit-ownership-timeline.plan.md   # Design plan for ownership_timeline.py
  ownership-residency-proxy.plan.md   # Proxy residency model and output columns
```

## Design documentation

Implementation plans and design notes are in the [docs/](docs/) directory.
