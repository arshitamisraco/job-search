#!/usr/bin/env python3
"""Job posting monitor: fetch, filter, diff, email. See companies.json / filters.json for config."""
import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
COMPANIES_FILE = BASE_DIR / "companies.json"
FILTERS_FILE = BASE_DIR / "filters.json"
SEEN_FILE = BASE_DIR / "seen.json"
FAILURES_FILE = BASE_DIR / "failures.json"

REQUEST_TIMEOUT = 20
USER_AGENT = "job-posting-monitor/1.0 (+personal use)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

# SmartRecruiters returns ISO country codes; map the ones relevant to our blocklist.
ISO_COUNTRY_MAP = {
    "us": "United States", "in": "India", "pl": "Poland", "ar": "Argentina",
    "mx": "Mexico", "co": "Colombia", "br": "Brazil", "cr": "Costa Rica",
    "ro": "Romania", "hu": "Hungary", "ua": "Ukraine", "ph": "Philippines",
    "ca": "Canada", "gb": "UK", "ie": "Ireland", "es": "Spain", "de": "Germany",
    "nl": "Netherlands", "au": "Australia", "sg": "Singapore", "jp": "Japan",
    "cn": "China",
}

NUMBER_WORD_MAP = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_NUM = r"(\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)"
YEAR_PATTERNS = [
    re.compile(_NUM + r"\s*\+\s*years?", re.I),
    re.compile(_NUM + r"\s*-\s*" + _NUM + r"\s*years?", re.I),
    re.compile(_NUM + r"\s+to\s+" + _NUM + r"\s*years?", re.I),
    re.compile(r"minimum of\s+" + _NUM + r"\s*years?", re.I),
    re.compile(r"at least\s+" + _NUM + r"\s*years?", re.I),
    re.compile(_NUM + r"\s*years?", re.I),
]

TAG_RE = re.compile(r"<[^>]+>")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def load_json(path, default):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def strip_html(raw):
    if not raw:
        return ""
    text = html.unescape(raw)
    text = TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def word_bounded_search(text, term):
    if not text or not term:
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def title_passes(title, filters):
    titles_cfg = filters["titles"]
    if not any(word_bounded_search(title, t) for t in titles_cfg["include"]):
        return False
    if any(word_bounded_search(title, t) for t in titles_cfg["exclude"]):
        return False
    return True


def classify_location(location_text, filters):
    """Returns (location_or_None, unconfirmed_bool). None means dropped."""
    loc = (location_text or "").strip()
    if not loc:
        return loc, False
    loc_cfg = filters["locations"]
    for term in loc_cfg["block_countries"]:
        if word_bounded_search(loc, term):
            return None, False
    if "remote" not in loc.lower():
        return loc, False
    us_signals = (
        loc_cfg.get("us_signals", [])
        + loc_cfg.get("us_state_names", [])
        + loc_cfg.get("us_state_abbreviations", [])
    )
    confirmed = any(word_bounded_search(loc, s) for s in us_signals)
    if confirmed:
        return loc, False
    if loc_cfg.get("strict_us_only", False):
        return None, False
    return loc, True


def word_to_num(w):
    if w.isdigit():
        return int(w)
    return NUMBER_WORD_MAP.get(w.lower())


def extract_min_years(description_text):
    """Returns (min_years_or_None, stated_bool)."""
    numbers = []
    for pattern in YEAR_PATTERNS:
        for match in pattern.finditer(description_text):
            for g in match.groups():
                if g is None:
                    continue
                n = word_to_num(g)
                if n is not None:
                    numbers.append(n)
    if not numbers:
        return None, False
    return min(numbers), True


# ---------------------------------------------------------------------------
# ATS adapters: each fetch_* returns a list of
# {company, job_id, title, location, url, _description(optional)}
# ---------------------------------------------------------------------------

def fetch_greenhouse(company_cfg):
    resp = SESSION.get(company_cfg["endpoint"], timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    postings = []
    for job in data.get("jobs", []):
        postings.append({
            "company": company_cfg["name"],
            "job_id": str(job.get("id")),
            "title": job.get("title", ""),
            "location": (job.get("location") or {}).get("name", ""),
            "url": job.get("absolute_url", ""),
            "_description": None,
        })
    return postings


def fetch_greenhouse_description(company_cfg, posting):
    url = f"{company_cfg['endpoint']}/{posting['job_id']}"
    resp = SESSION.get(url, params={"content": "true"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return strip_html(data.get("content", ""))


def fetch_lever(company_cfg):
    slug = company_cfg["slug"]
    url = f"https://api.lever.co/v0/postings/{slug}"
    resp = SESSION.get(url, params={"mode": "json"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    postings = []
    for job in data:
        description = job.get("descriptionPlain") or strip_html(job.get("description", ""))
        lists_text = " ".join(
            strip_html(section.get("content", ""))
            for section in job.get("lists", []) or []
        )
        postings.append({
            "company": company_cfg["name"],
            "job_id": str(job.get("id")),
            "title": job.get("text", ""),
            "location": (job.get("categories") or {}).get("location", ""),
            "url": job.get("hostedUrl") or job.get("applyUrl", ""),
            "_description": f"{description} {lists_text}".strip(),
        })
    return postings


def fetch_ashby(company_cfg):
    slug = company_cfg["slug"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    resp = SESSION.get(url, params={"includeCompensation": "false"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    postings = []
    for job in data.get("jobs", []):
        description = job.get("descriptionPlain") or strip_html(job.get("descriptionHtml", ""))
        postings.append({
            "company": company_cfg["name"],
            "job_id": str(job.get("id")),
            "title": job.get("title", ""),
            "location": job.get("location", ""),
            "url": job.get("jobUrl") or job.get("applyUrl", ""),
            "_description": description,
        })
    return postings


def fetch_smartrecruiters(company_cfg, filters):
    page_size = filters["smartrecruiters"]["page_size"]
    endpoint = company_cfg["endpoint"]
    slug = company_cfg["slug"]
    postings = []
    offset = 0
    while True:
        resp = SESSION.get(
            endpoint, params={"limit": page_size, "offset": offset}, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", [])
        for job in content:
            loc = job.get("location") or {}
            country_code = (loc.get("country") or "").lower()
            country_name = ISO_COUNTRY_MAP.get(country_code, loc.get("country", ""))
            parts = [loc.get("city", ""), loc.get("region", ""), country_name]
            location_text = ", ".join(p for p in parts if p)
            if loc.get("remote"):
                location_text = f"Remote - {location_text}" if location_text else "Remote"
            job_id = str(job.get("id"))
            postings.append({
                "company": company_cfg["name"],
                "job_id": job_id,
                "title": job.get("name", ""),
                "location": location_text,
                "url": f"https://jobs.smartrecruiters.com/{slug}/{job_id}",
                "_description": None,
            })
        total = data.get("totalFound", len(content))
        offset += page_size
        if offset >= total or not content:
            break
    return postings


def fetch_smartrecruiters_description(company_cfg, posting):
    url = f"{company_cfg['endpoint']}/{posting['job_id']}"
    resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    sections = (data.get("jobAd") or {}).get("sections", {}) or {}
    parts = [s.get("text", "") for s in sections.values() if isinstance(s, dict)]
    return strip_html(" ".join(parts))


def fetch_workday(company_cfg, filters):
    wd_cfg = filters["workday"]
    page_size = wd_cfg["page_size"]
    delay = wd_cfg["page_delay_seconds"]
    search_text = company_cfg.get("searchText", wd_cfg["default_search_text"])
    endpoint = company_cfg["endpoint"]
    tenant = company_cfg["tenant"]
    wd_num = company_cfg["wdNum"]
    site = company_cfg["site"]

    postings = []
    offset = 0
    total = None
    while total is None or offset < total:
        body = {
            "appliedFacets": {},
            "limit": page_size,
            "offset": offset,
            "searchText": search_text,
        }
        resp = SESSION.post(endpoint, json=body, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        total = data.get("total", 0)
        job_postings = data.get("jobPostings", [])
        for job in job_postings:
            external_path = job.get("externalPath", "")
            job_id = external_path.rstrip("/").split("/")[-1] or external_path
            postings.append({
                "company": company_cfg["name"],
                "job_id": job_id,
                "title": job.get("title", ""),
                "location": job.get("locationsText", ""),
                "url": f"https://{tenant}.wd{wd_num}.myworkdayjobs.com/{site}{external_path}",
                "_description": None,
                "_external_path": external_path,
            })
        if not job_postings:
            break
        offset += page_size
        if offset < total:
            time.sleep(delay)
    return postings


def fetch_workday_description(company_cfg, posting):
    tenant = company_cfg["tenant"]
    wd_num = company_cfg["wdNum"]
    site = company_cfg["site"]
    external_path = posting.get("_external_path", "")
    url = f"https://{tenant}.wd{wd_num}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{external_path}"
    resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    description = (data.get("jobPostingInfo") or {}).get("jobDescription", "")
    return strip_html(description)


def fetch_icims(company_cfg):
    # iCIMS has no documented public JSON API; this assumes the tenant's search
    # results endpoint returns a shape like {"searchResults": [...]}. Verify
    # against the real tenant before relying on it -- unused until a companies.json
    # entry sets ats="icims".
    resp = SESSION.get(company_cfg["endpoint"], timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("searchResults") or data.get("jobs") or []
    postings = []
    for job in results:
        job_id = str(job.get("id") or job.get("job_id") or job.get("normalizedTitle", ""))
        url_field = job.get("url")
        url = url_field.get("long", "") if isinstance(url_field, dict) else (url_field or "")
        postings.append({
            "company": company_cfg["name"],
            "job_id": job_id,
            "title": job.get("title") or job.get("job_title", ""),
            "location": job.get("normalizedLocation") or job.get("location", ""),
            "url": url,
            "_description": None,
        })
    return postings


def fetch_icims_description(posting):
    resp = SESSION.get(posting["url"], params={"mode": "json"}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    text = data.get("description") or data.get("job_description", "")
    return strip_html(text)


def fetch_postings(company_cfg, filters):
    ats = company_cfg.get("ats")
    if ats == "greenhouse":
        return fetch_greenhouse(company_cfg)
    if ats == "lever":
        return fetch_lever(company_cfg)
    if ats == "ashby":
        return fetch_ashby(company_cfg)
    if ats == "smartrecruiters":
        return fetch_smartrecruiters(company_cfg, filters)
    if ats == "workday":
        return fetch_workday(company_cfg, filters)
    if ats == "icims":
        return fetch_icims(company_cfg)
    raise ValueError(f"unknown ats type: {ats}")


def get_description(company_cfg, posting, filters):
    if posting.get("_description") is not None:
        return posting["_description"]
    ats = company_cfg["ats"]
    if ats == "greenhouse":
        return fetch_greenhouse_description(company_cfg, posting)
    if ats == "smartrecruiters":
        time.sleep(filters["smartrecruiters"]["detail_delay_seconds"])
        return fetch_smartrecruiters_description(company_cfg, posting)
    if ats == "workday":
        time.sleep(filters["workday"]["detail_delay_seconds"])
        return fetch_workday_description(company_cfg, posting)
    if ats == "icims":
        time.sleep(filters["icims"]["detail_delay_seconds"])
        return fetch_icims_description(posting)
    raise ValueError(f"no description strategy for {ats}")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def process_company(company_cfg, filters, seen):
    """Returns (matches, error_or_None)."""
    name = company_cfg["name"]
    try:
        raw_postings = fetch_postings(company_cfg, filters)
    except Exception as e:
        return None, str(e)

    filtered = []
    for p in raw_postings:
        if not title_passes(p["title"], filters):
            continue
        location, unconfirmed = classify_location(p["location"], filters)
        if location is None:
            continue
        p["location"] = location
        p["location_unconfirmed"] = unconfirmed
        filtered.append(p)

    seen_ids = set(seen.get(name, []))
    candidates = [p for p in filtered if p["job_id"] not in seen_ids]

    matches = []
    for p in candidates:
        try:
            description = get_description(company_cfg, p, filters)
        except Exception as e:
            log(f"  description fetch failed for {name} / {p['job_id']}: {e}")
            continue
        min_years, stated = extract_min_years(description)
        max_years = filters["experience"]["max_years"]
        if stated and min_years > max_years:
            continue
        p["yoe_not_stated"] = not stated
        matches.append(p)

    return matches, None


def build_email(results_by_company, unreachable, loud_failures, filters):
    total = sum(len(v) for v in results_by_company.values())
    company_names = list(results_by_company.keys())
    if total:
        noun = "posting" if total == 1 else "postings"
        subject = f"{total} new {noun} — {', '.join(company_names)}"
    else:
        subject = "Job monitor alert"

    parts = ["<html><body>"]
    if total:
        parts.append(f"<h2>{total} new {'posting' if total == 1 else 'postings'}</h2>")
        for company, jobs in results_by_company.items():
            if not jobs:
                continue
            parts.append(f"<h3>{html.escape(company)}</h3><ul>")
            for j in jobs:
                tags = []
                if j.get("location_unconfirmed"):
                    tags.append(filters["locations"]["unconfirmed_tag"])
                if j.get("yoe_not_stated"):
                    tags.append(filters["experience"]["not_stated_tag"])
                tag_str = f" <em>({', '.join(tags)})</em>" if tags else ""
                parts.append(
                    f"<li><a href=\"{html.escape(j['url'])}\">{html.escape(j['title'])}</a>"
                    f" — {html.escape(j['location'])}{tag_str}</li>"
                )
            parts.append("</ul>")

    if loud_failures:
        parts.append("<h3 style=\"color:red\">⚠ Repeated failures</h3><ul>")
        for name, count in loud_failures:
            parts.append(f"<li>{html.escape(name)} has failed {count} consecutive runs — re-run discovery.</li>")
        parts.append("</ul>")

    if unreachable:
        parts.append(f"<p><small>couldn't reach: {html.escape(', '.join(unreachable))}</small></p>")

    parts.append("</body></html>")
    return subject, "\n".join(parts)


def send_email(subject, html_body, gmail_user, gmail_app_password, alert_to):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = alert_to
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, [alert_to], msg.as_string())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print the email instead of sending; don't update state")
    args = parser.parse_args()

    companies = load_json(COMPANIES_FILE, [])
    filters = load_json(FILTERS_FILE, {})
    seen = load_json(SEEN_FILE, {})
    failures = load_json(FAILURES_FILE, {})

    threshold = filters["error_handling"]["consecutive_failure_alert_threshold"]

    results_by_company = {}
    unreachable = []
    loud_failures = []

    for company_cfg in companies:
        name = company_cfg["name"]
        if not company_cfg.get("ats"):
            continue

        log(f"fetching {name}...")
        matches, error = process_company(company_cfg, filters, seen)

        if error is not None:
            log(f"  FAILED: {error}")
            unreachable.append(name)
            failures[name] = failures.get(name, 0) + 1
            if failures[name] >= threshold:
                loud_failures.append((name, failures[name]))
            continue

        failures[name] = 0
        if matches:
            results_by_company[name] = matches
            seen.setdefault(name, [])
            for m in matches:
                seen[name].append(m["job_id"])

    total_new = sum(len(v) for v in results_by_company.values())

    if total_new == 0 and not loud_failures:
        log("no new postings")
        if not args.dry_run:
            save_json(FAILURES_FILE, failures)
        return

    subject, body = build_email(results_by_company, unreachable, loud_failures, filters)

    if args.dry_run:
        print(f"SUBJECT: {subject}\n")
        print(body)
        return

    gmail_user = os.environ.get("GMAIL_USER")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    alert_to = os.environ.get("ALERT_TO")
    if not all([gmail_user, gmail_app_password, alert_to]):
        log("missing GMAIL_USER / GMAIL_APP_PASSWORD / ALERT_TO env vars; not sending")
        sys.exit(1)

    send_email(subject, body, gmail_user, gmail_app_password, alert_to)
    log(f"sent: {subject}")

    save_json(SEEN_FILE, seen)
    save_json(FAILURES_FILE, failures)


if __name__ == "__main__":
    main()
