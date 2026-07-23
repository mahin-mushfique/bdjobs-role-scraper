# Bdjobs.com Role Search Scraper

Searches bdjobs.com for the roles listed in `roles.csv` — using each one as a real search query against bdjobs' own API, the same as typing it into their search box — and outputs a deduplicated list of companies hiring for those roles each time you run it.

Rewritten from [Aryan3212/bdjobs-scraper](https://github.com/Aryan3212/bdjobs-scraper), which reverse-engineered bdjobs' real backend REST API (bdjobs moved to an Angular SPA, so the old HTML-scraping approach doesn't work anymore). This version calls the same two API endpoints but only pulls what's needed for a small daily lead list instead of the original's full 31-field, ~5,500-job dump.

## Files

| File | Purpose |
|---|---|
| `bdjobs_ecommerce_scraper.py` | The scraper |
| `roles.csv` | Roles/search terms — edit this, no code changes needed |
| `requirements.txt` | Python dependencies |
| `seen_companies.csv` | Auto-created memory of companies already reported (so re-runs only show new ones) |
| `bdjobs_new_companies_<date>.csv` | Auto-created output of that run's new matches |

## Setup

```bash
pip install -r requirements.txt
python3 bdjobs_ecommerce_scraper.py
```

## Usage

**Default run** — searches bdjobs for each role in `roles.csv`, 3 pages (150 results) per role:

```bash
python3 bdjobs_ecommerce_scraper.py
```

**Custom roles file:**

```bash
python3 bdjobs_ecommerce_scraper.py --roles my_roles.csv
```

**Custom date range** — only include postings published within a window (uses bdjobs' `publishDate` field):

```bash
python3 bdjobs_ecommerce_scraper.py --start-date 2026-07-01 --end-date 2026-07-23
```

Either flag works alone. Dates use `YYYY-MM-DD`.

**More/fewer result pages per role:**

```bash
python3 bdjobs_ecommerce_scraper.py --pages-per-role 5
```

Combine any of the above as needed.

## How it works (roles = real searches)

Each row in `roles.csv` is sent to bdjobs as `keyword=<role>` — a real, live, server-side search (confirmed directly against `gateway.bdjobs.com`; searching "Manager" returns `total_records_found: 1672`, searching "Data Analyst" returns `1104`, out of ~5,500 total live jobs, so this is genuinely filtering, not just returning everything).

bdjobs' own search is loose/fuzzy though — searching "Data Analyst" also returned a "Business Development Analyst" posting. To keep results precise, the script applies a confirmation check on top: only postings whose actual job title contains your role phrase are kept. This was verified against real captured API responses before shipping.

## Editing roles.csv

One role/search term per line. No header required (a header cell containing "role"/"title"/"keyword" is skipped automatically if present):

```
role
HR Manager
Data Analyst
Ecommerce Manager
```

**Broad single-word roles will still return a lot of results.** `Manager` or `Executive` alone match a huge share of all bdjobs postings — that's not something this script can narrow further, it's genuinely how common those words are in job titles. Use more specific multi-word phrases (e.g. "HR Manager" rather than "Manager") if you want a tighter list.

## Verification status

Confirmed **live**, directly against `gateway.bdjobs.com`, not guessed:

- The list endpoint takes a real `keyword=` search parameter and filters server-side
- Every search result already includes `Jobid`, `jobTitle`, `companyName`, and `publishDate` inline — no per-job Details API call needed (you should see "Jobs needing a Details API lookup: 0" on every run)
- The public job-detail URL format: `https://bdjobs.com/h/details/{job_id}?ln=1`

Still unverified (kept as a defensive fallback only, shouldn't matter in normal use): the Details API's own field names, sourced from the original repo's code rather than a live call. If you ever see a non-zero "Details API lookup" count and results looking off, that fallback path is where to check first.

## Scheduling

Linux/Mac cron, daily at 7am:

```
0 7 * * * cd /path/to/script && /usr/bin/python3 bdjobs_ecommerce_scraper.py
```

Windows Task Scheduler: daily trigger running `python bdjobs_ecommerce_scraper.py`.

## Terms of service

This calls bdjobs' own public API with no authentication and no bot-detection bypass, but it's still unofficial. Keep run frequency reasonable (daily, not continuous polling) and respect bdjobs.com's Terms of Service.
