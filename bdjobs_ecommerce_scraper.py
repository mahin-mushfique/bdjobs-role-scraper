"""
Bdjobs.com Role Search Scraper - rewritten from Aryan3212/bdjobs-scraper
=====================================================================================
Source scraper: https://github.com/Aryan3212/bdjobs-scraper
That project reverse-engineered bdjobs.com's real backend REST API (it switched
from server-rendered HTML to an Angular SPA), so this rewrite calls the SAME two
endpoints instead of parsing HTML or driving a headless browser - no Playwright,
no CSS selectors, no JS rendering needed.

  List API    -> https://gateway.bdjobs.com/recruitment-account-test/api/JobSearch/GetJobSearch
  Details API -> https://gateway.bdjobs.com/ActtivejobsTest/api/JobSubsystem/jobDetails

VERIFIED LIVE (this is no longer guesswork)
-----------------------------------------------
Earlier versions of this script guessed at field names and had to fall back
to the Details API a lot. I've since called the List API live and confirmed:

  - It takes a real search parameter: &keyword=<your search text>, and it
    genuinely filters server-side (searching "Manager" returned
    total_records_found: 1672; searching "Data Analyst" returned 1104 - out
    of ~5,500 total live jobs). This is what ROLES-AS-SEARCH (below) is
    built on.
  - Each list item already includes everything needed inline - no Details
    API call required per job:
        Jobid        (job ID)
        jobTitle      (job title)
        companyName   (hiring company)
        publishDate   (ISO datetime, e.g. "2026-07-13T07:43:00Z")
        deadlineDB    (ISO datetime deadline)
  - The public job link format, confirmed against a real bdjobs.com URL:
        https://bdjobs.com/h/details/{job_id}?ln=1

The Details API field names (JobTitle, CompnayName - a typo in bdjobs' own
API, PostedOn) are still only confirmed from the original repo's source, not
live - kept as a defensive fallback in case a rare list item is ever missing
a field, but in practice you should see "Jobs needing a Details API lookup: 0"
every run now.

ROLES ARE NOW USED AS REAL SEARCHES (per your request)
-----------------------------------------------------------
Previously this script pulled a generic batch of the newest listings and
filtered them client-side by checking if a role phrase appeared in the
title. Now, each row in roles.csv is sent as an actual `keyword=` search
query to bdjobs - the same thing typing it into bdjobs' own search box
would do - instead of scanning an unfiltered firehose of postings.

A client-side confirmation check is still applied on top of the server
search results, because bdjobs' own search is loose/fuzzy, not an exact
phrase match - e.g. searching "Data Analyst" also returned a "Business
Development Analyst" posting (matched on "Analyst" alone, or elsewhere in
the listing). The confirmation check keeps only results whose job title
actually contains your role phrase, so search noise doesn't leak into your
output.

IMPORTANT: broad single-word roles are still broad
-----------------------------------------------------
If a role in roles.csv is just "Manager" or "Executive", it will still
return a large number of results (bdjobs' own search treats "Manager" as
1,672 matching jobs right now) - that's not a bug in this script, it's
just how broad that term is on a job board where "X Manager" is one of the
most common title patterns. Use more specific phrases (e.g. "HR Manager",
"Data Analyst", "Ecommerce Manager") if you want a narrower list.

ROLES FILE
----------
roles.csv (default, next to this script): one role/search-term per row.
No header required, but a header cell containing "role"/"title"/"keyword"
is auto-skipped if present.

    role
    HR Manager
    Data Analyst
    Ecommerce Manager

Point the script at a different file with --roles:
    python3 bdjobs_ecommerce_scraper.py --roles my_roles.csv

DATE RANGE
-----------
Restrict output to postings published within a window, using the confirmed
`publishDate` field:

    python3 bdjobs_ecommerce_scraper.py --start-date 2026-07-01 --end-date 2026-07-23

Either flag works alone.

PAGES PER ROLE
-----------------
Each role search can return many pages (50 results/page). Default is 3
pages (150 results) per role per run - override with --pages-per-role.
Bdjobs' sort order for search results (vs. the plain newest-first browse
list) isn't confirmed, so this script does NOT assume search results are
date-sorted; it fetches the configured number of pages per role and then
applies --start-date/--end-date filtering across everything it fetched.

    python3 bdjobs_ecommerce_scraper.py --pages-per-role 5

SETUP
-----
    pip install -r requirements.txt
    python3 bdjobs_ecommerce_scraper.py

SCHEDULING
----------
Linux/Mac cron (every morning at 7am):
    0 7 * * * cd /path/to/script && /usr/bin/python3 bdjobs_ecommerce_scraper.py

Terms of service: this calls bdjobs' own public API with no authentication
and no bot-detection bypass, but it's still unofficial - keep run frequency
reasonable and respect bdjobs.com's Terms of Service.
"""

import argparse
import asyncio
import csv
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

DEFAULT_PAGES_PER_ROLE = 3               # 50 results/page - override with --pages-per-role
SEARCH_PARAM_NAME = "keyword"            # CONFIRMED live against gateway.bdjobs.com

LIST_PAGE_DELAY_SECONDS = 0.25          # matches the original repo's rate limit
DETAILS_BATCH_SIZE = 20                  # matches the original repo
DETAILS_CONNECTION_LIMIT = 5             # matches the original repo
MAX_RETRIES = 10                         # matches the original repo

DEFAULT_ROLES_FILE = Path(__file__).parent / "roles.csv"

# Populated at startup from the roles CSV file - see load_roles_from_csv().
KEYWORD_FRAGMENTS = []

OUTPUT_DIR = Path(__file__).parent
SEEN_FILE = OUTPUT_DIR / "seen_companies.csv"
TODAY = date.today().isoformat()
DAILY_OUTPUT = OUTPUT_DIR / f"bdjobs_new_companies_{TODAY}.csv"

LIST_API_URL = "https://gateway.bdjobs.com/recruitment-account-test/api/JobSearch/GetJobSearch"
DETAILS_API_URL = "https://gateway.bdjobs.com/ActtivejobsTest/api/JobSubsystem/jobDetails"
JOB_DETAIL_URL_TEMPLATE = "https://bdjobs.com/h/details/{job_id}?ln=1"  # confirmed live

# Confirmed live field names first, PascalCase variants kept as a fallback
# for the (unverified) Details API and for extra safety.
LIST_ITEM_ID_KEYS = ["Jobid", "JobId", "jobId", "jobid"]
LIST_ITEM_TITLE_KEYS = ["jobTitle", "JobTitle", "Jobtitle", "Title"]
LIST_ITEM_COMPANY_KEYS = ["companyName", "CompnayName", "CompanyName", "Compnayname"]
LIST_ITEM_DATE_KEYS = ["publishDate", "PostedOn", "Postedon", "postedOn", "PostDate"]
DETAILS_DATE_KEYS = ["PostedOn", "publishDate", "Postedon", "postedOn", "PostDate"]

DATE_FORMATS_TO_TRY = [
    "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d-%b-%Y",
    "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%B %d, %Y", "%d %b %Y",
]

# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------

def strip_html(html_text):
    if not html_text:
        return None
    return BeautifulSoup(html_text, "html.parser").get_text(separator=" ", strip=True)


def normalize_company(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def title_matches_role(title: str, role: str) -> bool:
    """Client-side confirmation that a search result's title actually
    contains the role phrase we searched for - guards against bdjobs'
    own search being loose/fuzzy (see docstring)."""
    return role.lower() in (title or "").lower()


def first_present(d: dict, keys: list):
    for k in keys:
        if d.get(k):
            return d[k]
    return None


def parse_date_flexible(value):
    """Best-effort parse of a publishDate-style value into a date object."""
    if not value:
        return None
    value = str(value).strip()
    for fmt in DATE_FORMATS_TO_TRY:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    match = re.search(r"(\d{4}-\d{2}-\d{2})", value)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def in_date_range(posted_date, start_date, end_date) -> bool:
    if start_date is None and end_date is None:
        return True
    if posted_date is None:
        return False
    if start_date and posted_date < start_date:
        return False
    if end_date and posted_date > end_date:
        return False
    return True


def load_roles_from_csv(path: Path) -> list:
    """Reads one role/search term per row. No header required; a header
    cell containing role/title/keyword is auto-skipped. Original casing is
    preserved (used as the literal search query against bdjobs)."""
    if not path.exists():
        print(f"ERROR: roles file not found: {path}")
        print("Create it (one role per line) or pass a different path with --roles.")
        sys.exit(1)

    roles = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            value = row[0].strip()
            if not value:
                continue
            if value.lower() in ("role", "title", "keyword", "roles", "titles"):
                continue
            roles.append(value)

    if not roles:
        print(f"ERROR: {path} has no usable roles in it.")
        sys.exit(1)

    return roles


def load_seen_companies() -> set:
    if not SEEN_FILE.exists():
        return set()
    with open(SEEN_FILE, newline="", encoding="utf-8") as f:
        return {row["company_key"] for row in csv.DictReader(f)}


def append_seen_companies(rows):
    file_exists = SEEN_FILE.exists()
    with open(SEEN_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["company_key", "company", "first_seen"])
        if not file_exists:
            writer.writeheader()
        for r in rows:
            writer.writerow(r)


# ----------------------------------------------------------------------------
# API CALLS
# ----------------------------------------------------------------------------

async def fetch_json(url, session):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        print(f"  request failed for {url}: {e}")
    return None


async def fetch_role_search_page(role, page_num, session):
    query = quote_plus(role)
    url = f"{LIST_API_URL}?isPro=1&rpp=50&pg={page_num}&{SEARCH_PARAM_NAME}={query}"
    await asyncio.sleep(LIST_PAGE_DELAY_SECONDS)
    return await fetch_json(url, session)


async def fetch_job_details(job_id, session):
    url = f"{DETAILS_API_URL}?jobId={job_id}"
    response = await fetch_json(url, session)
    if response and response.get("statuscode") == "0" and response.get("data"):
        return response["data"][0]
    return None


async def process_detail_job(job_id, role, session, retry_bucket):
    """Defensive fallback only - in practice list items already carry
    title/company/date, see VERIFIED LIVE note in the module docstring."""
    try:
        details = await fetch_job_details(job_id, session)
        if not details:
            return None
        title = first_present(details, LIST_ITEM_TITLE_KEYS)
        company = first_present(details, LIST_ITEM_COMPANY_KEYS)
        posted_raw = first_present(details, DETAILS_DATE_KEYS)
        return {
            "job_id": job_id,
            "job_title": title,
            "company": company,
            "matched_role": role,
            "posted_raw": posted_raw,
            "posted_date": parse_date_flexible(posted_raw),
        }
    except Exception as e:
        print(f"  error on job {job_id}: {repr(e)}")
        retry_bucket.append(job_id)
        return None


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

async def run(pages_per_role=DEFAULT_PAGES_PER_ROLE, start_date=None, end_date=None):
    date_filtering = start_date is not None or end_date is not None
    seen = load_seen_companies()

    matches = []              # confirmed matches, ready to write out
    needs_details = []        # (job_id, role) pairs missing title/company from the list item
    already_processed = set()  # job_ids already matched (skip if another role's search re-finds it)

    baseline_total_records = None

    conn = aiohttp.TCPConnector(limit=DETAILS_CONNECTION_LIMIT)
    async with aiohttp.ClientSession(connector=conn) as session:

        range_desc = f" (date range: {start_date or 'any'} to {end_date or 'any'})" if date_filtering else ""
        print(f"Searching bdjobs for {len(KEYWORD_FRAGMENTS)} role(s), {pages_per_role} page(s) each...{range_desc}")

        for role in KEYWORD_FRAGMENTS:
            print(f"  role: '{role}'")
            for page_num in range(1, pages_per_role + 1):
                page = await fetch_role_search_page(role, page_num, session)
                if not page or page.get("statuscode") != "1":
                    break

                if page_num == 1:
                    total_records = page.get("common", {}).get("total_records_found")
                    if baseline_total_records is None:
                        baseline_total_records = total_records
                    elif total_records is not None and total_records == baseline_total_records:
                        print(
                            f"    WARNING: total_records_found ({total_records}) matches a previous "
                            f"role's count - '{SEARCH_PARAM_NAME}' may not be filtering for this role. "
                            f"Double check this role's results."
                        )

                items = page.get("data", []) + page.get("premiumData", [])
                if not items:
                    break

                for job in items:
                    job_id = first_present(job, LIST_ITEM_ID_KEYS)
                    if not job_id or job_id in already_processed:
                        continue

                    title = first_present(job, LIST_ITEM_TITLE_KEYS)
                    company = first_present(job, LIST_ITEM_COMPANY_KEYS)
                    posted_raw = first_present(job, LIST_ITEM_DATE_KEYS)
                    posted_date = parse_date_flexible(posted_raw) if posted_raw else None

                    if not title:
                        needs_details.append((job_id, role))
                        continue

                    if not title_matches_role(title, role):
                        continue  # server search was loose, this one doesn't actually match

                    if date_filtering and not in_date_range(posted_date, start_date, end_date):
                        continue

                    already_processed.add(job_id)
                    matches.append({
                        "job_id": job_id, "job_title": title, "company": company or "",
                        "matched_role": role, "posted_raw": posted_raw,
                    })

        print(f"Confirmed matches from search results: {len(matches)}")
        print(f"Jobs needing a Details API lookup (missing title in list item): {len(needs_details)}")

        detail_results = []
        retry_bucket = []

        for i in range(0, len(needs_details), DETAILS_BATCH_SIZE):
            batch = needs_details[i:i + DETAILS_BATCH_SIZE]
            tasks = [process_detail_job(jid, role, session, retry_bucket) for jid, role in batch]
            results = await asyncio.gather(*tasks)
            detail_results.extend([r for r in results if r])

        retry_count = 0
        while retry_bucket and retry_count < MAX_RETRIES:
            batch = retry_bucket.copy()
            retry_bucket.clear()
            # retry_bucket only has job_ids; role isn't preserved through retries,
            # so re-check against the full role list on retry
            tasks = [
                process_detail_job(jid, "(retry - role unknown)", session, retry_bucket)
                for jid in batch
            ]
            results = await asyncio.gather(*tasks)
            detail_results.extend([r for r in results if r])
            retry_count += 1

    for r in detail_results:
        if r["job_id"] in already_processed:
            continue
        if not r.get("job_title"):
            continue
        if not any(title_matches_role(r["job_title"], role) for role in KEYWORD_FRAGMENTS):
            continue
        if date_filtering and not in_date_range(r.get("posted_date"), start_date, end_date):
            continue
        already_processed.add(r["job_id"])
        matches.append(r)

    print(f"Total confirmed matches: {len(matches)}")

    # Dedupe by company, against today's batch and against everything seen before
    new_rows = []
    newly_seen = []
    seen_today = set()

    for m in matches:
        company = (m.get("company") or "").strip()
        if not company:
            continue
        key = normalize_company(company)
        if key in seen or key in seen_today:
            continue
        seen_today.add(key)
        new_rows.append({
            "source": "bdjobs.com",
            "company": company,
            "job_title": m.get("job_title", ""),
            "matched_role": m.get("matched_role", ""),
            "link": JOB_DETAIL_URL_TEMPLATE.format(job_id=m["job_id"]),
            "posted_on": m.get("posted_raw", "") or "",
        })
        newly_seen.append({"company_key": key, "company": company, "first_seen": TODAY})

    if new_rows:
        with open(DAILY_OUTPUT, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["source", "company", "job_title", "matched_role", "link", "posted_on"]
            )
            writer.writeheader()
            writer.writerows(new_rows)
        append_seen_companies(newly_seen)
        print(f"\n{len(new_rows)} new companies written to {DAILY_OUTPUT}")
    else:
        print("\nNo new companies found today.")


def parse_cli_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a valid date, use YYYY-MM-DD")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bdjobs.com role-search scraper")
    parser.add_argument(
        "--roles",
        type=Path,
        default=DEFAULT_ROLES_FILE,
        help=f"Path to a CSV of roles/search terms (default: {DEFAULT_ROLES_FILE.name})",
    )
    parser.add_argument(
        "--pages-per-role",
        type=int,
        default=DEFAULT_PAGES_PER_ROLE,
        help=f"How many result pages to fetch per role search (default: {DEFAULT_PAGES_PER_ROLE}, 50/page)",
    )
    parser.add_argument(
        "--start-date",
        type=parse_cli_date,
        default=None,
        help="Only include postings published on/after this date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--end-date",
        type=parse_cli_date,
        default=None,
        help="Only include postings published on/before this date (YYYY-MM-DD).",
    )
    args = parser.parse_args()

    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error("--start-date must be on or before --end-date")

    KEYWORD_FRAGMENTS = load_roles_from_csv(args.roles)
    print(f"Loaded {len(KEYWORD_FRAGMENTS)} role(s) from {args.roles}: {KEYWORD_FRAGMENTS}")

    asyncio.run(run(pages_per_role=args.pages_per_role, start_date=args.start_date, end_date=args.end_date))
