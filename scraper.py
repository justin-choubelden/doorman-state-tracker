#!/usr/bin/env python3
"""
Doorman State Regulation Tracker - scraper

Pulls the two source tables Doorman's team already relies on:
  - Ballotpedia: https://ballotpedia.org/State_policies_on_cellphone_use_in_K-12_public_schools
  - NCSL:        https://www.ncsl.org/education/enacted-state-legislation-cellphone-use-in-schools

...merges them per state, classifies Doorman compatibility with a documented
keyword heuristic (see CLASSIFY section below - edit these lists, not a
spreadsheet, to refine the logic), diffs against the last run, and writes
data.json + a changelog entry. Intended to run daily via GitHub Actions
(.github/workflows/update.yml) - the Ballotpedia/NCSL domains are NOT
reachable from every sandboxed environment, so if you're testing this
locally behind a restrictive proxy, run it from a normal machine or let
GitHub Actions run it.
"""
import concurrent.futures
import json
import re
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

MAX_WORKERS = 8  # modest parallelism - fast without hammering LegiScan

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
BALLOTPEDIA_URL = "https://ballotpedia.org/State_policies_on_cellphone_use_in_K-12_public_schools"
NCSL_URL = "https://www.ncsl.org/education/enacted-state-legislation-cellphone-use-in-schools"


def _get_html(url):
    """Fetch a URL and return its HTML text, printing diagnostics if the
    response looks suspicious (blocked/empty/redirected) so failures are
    debuggable from the Actions log instead of a bare parser error."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    print(f"  GET {url} -> status {resp.status_code}, {len(resp.text)} chars, "
          f"content-type={resp.headers.get('content-type')}")
    resp.raise_for_status()
    if len(resp.text) < 3000:
        print("  WARNING: response body is suspiciously short. First 500 chars:")
        print("  " + resp.text[:500].replace(chr(10), " "))
    return resp.text

DATA_FILE = Path(__file__).parent / "data.json"

# Pending/failed bill tracking uses the LegiScan API (free, 30k queries/month).
# Sign up at https://legiscan.com/user/register, generate a key, and set it as
# the LEGISCAN_API_KEY environment variable (a GitHub Actions secret in
# production). If unset, this section is skipped and In Progress/Failed stay
# empty rather than the whole script failing.
import os
LEGISCAN_API_KEY = os.environ.get("LEGISCAN_API_KEY", "")
LEGISCAN_QUERY = "cell phone OR wireless communication device OR electronic device school student"
LEGISCAN_STATUS = {1: "Introduced", 2: "Engrossed", 3: "Enrolled", 4: "Passed", 5: "Vetoed", 6: "Failed"}
MAX_BILLS_PER_STATE = 10  # keeps API usage well under the free 30k/month cap (~510 calls/day worst case, ~15k/month)

US_STATES = [
    "Alabama","Alaska","Arizona","Arkansas","California","Colorado","Connecticut","Delaware",
    "Florida","Georgia","Hawaii","Idaho","Illinois","Indiana","Iowa","Kansas","Kentucky",
    "Louisiana","Maine","Maryland","Massachusetts","Michigan","Minnesota","Mississippi","Missouri",
    "Montana","Nebraska","Nevada","New Hampshire","New Jersey","New Mexico","New York",
    "North Carolina","North Dakota","Ohio","Oklahoma","Oregon","Pennsylvania","Rhode Island",
    "South Carolina","South Dakota","Tennessee","Texas","Utah","Vermont","Virginia","Washington",
    "West Virginia","Wisconsin","Wyoming","District of Columbia",
]

STATE_ABBR = {
    "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA","Colorado":"CO",
    "Connecticut":"CT","Delaware":"DE","Florida":"FL","Georgia":"GA","Hawaii":"HI","Idaho":"ID",
    "Illinois":"IL","Indiana":"IN","Iowa":"IA","Kansas":"KS","Kentucky":"KY","Louisiana":"LA",
    "Maine":"ME","Maryland":"MD","Massachusetts":"MA","Michigan":"MI","Minnesota":"MN",
    "Mississippi":"MS","Missouri":"MO","Montana":"MT","Nebraska":"NE","Nevada":"NV",
    "New Hampshire":"NH","New Jersey":"NJ","New Mexico":"NM","New York":"NY","North Carolina":"NC",
    "North Dakota":"ND","Ohio":"OH","Oklahoma":"OK","Oregon":"OR","Pennsylvania":"PA",
    "Rhode Island":"RI","South Carolina":"SC","South Dakota":"SD","Tennessee":"TN","Texas":"TX",
    "Utah":"UT","Vermont":"VT","Virginia":"VA","Washington":"WA","West Virginia":"WV",
    "Wisconsin":"WI","Wyoming":"WY","District of Columbia":"DC",
}

# ---------------------------------------------------------------------------
# FETCH
# ---------------------------------------------------------------------------

def _find_table(tables, required_cols):
    """Return the first dataframe whose columns contain all required_cols substrings."""
    for t in tables:
        cols = [str(c).strip().lower() for c in t.columns]
        if all(any(req in c for c in cols) for req in required_cols):
            return t
    return None


def fetch_ballotpedia():
    html = _get_html(BALLOTPEDIA_URL)
    tables = pd.read_html(StringIO(html))
    table = _find_table(tables, ["state", "date enacted"])
    if table is None:
        raise RuntimeError("Ballotpedia page structure changed - could not locate the state policy table. "
                            "Open the page and update fetch_ballotpedia() column matching.")
    out = {}
    for _, row in table.iterrows():
        state = str(row.get("State", "")).strip()
        if not state or state.lower() == "nan":
            continue
        entry = {
            "date_enacted": str(row.get("Date enacted", "")).strip(),
            "bill": str(row.get("Bill/policy text", "")).strip(),
            "type": str(row.get("Type of limitation", "")).strip(),
            "details": str(row.get("Details", "")).strip(),
        }
        out.setdefault(state, []).append(entry)
    return out


def fetch_ncsl():
    html = _get_html(NCSL_URL)
    tables = pd.read_html(StringIO(html))
    table = _find_table(tables, ["jurisdiction", "bill number"])
    if table is None:
        raise RuntimeError("NCSL page structure changed - could not locate the enacted legislation table. "
                            "Open the page and update fetch_ncsl() column matching.")
    out = {}
    for _, row in table.iterrows():
        state = str(row.get("Jurisdiction", "")).strip()
        if not state or state.lower() == "nan":
            continue
        out[state] = {
            "bill_number": re.sub(r"\s+", " ", str(row.get("Bill Number", ""))).strip(),
            "category": str(row.get("Category", "")).strip(),
            "summary": str(row.get("Summary", "")).strip(),
        }
    return out

# ---------------------------------------------------------------------------
# PENDING / FAILED BILLS  (LegiScan)
# ---------------------------------------------------------------------------

def implication_for(text):
    """One simple sentence, not legal analysis, per the intern brief."""
    physical_mandatory, physical_optional = _scan_keyword_signals(text, PHYSICAL_KEYWORDS)
    software_mandatory, software_optional = _scan_keyword_signals(text, SOFTWARE_KEYWORDS)
    if physical_mandatory:
        return "Would require physical device storage if passed \u2014 incompatible with a software-only approach."
    if software_mandatory or software_optional:
        return "Would explicitly allow a technology/software approach if passed \u2014 favorable for Doorman."
    if physical_optional:
        return "Would allow physical storage as one option, not require it \u2014 worth watching, not a blocker."
    if any(k in text.lower() for k in LOCAL_DISCRETION_KEYWORDS):
        return "Would leave the method up to districts if passed \u2014 neutral, still sellable."
    return "Would require a restriction policy if passed; specific method not yet defined."


# LegiScan's status field only has three pre-law buckets (Introduced /
# Engrossed / Enrolled), and in practice the overwhelming majority of bills
# never leave "Introduced" - that's realistic, not a bug, but it's not a very
# useful signal on its own. This derives a more specific one-to-three-word
# progress label from the bill's actual action history via keyword rules -
# still pure code, no AI needed.
COMMITTEE_CLEARED_KEYWORDS = ("do pass", "passed committee", "report out of committee",
                                "reported favorably", "recommend do pass")
HEARING_KEYWORDS = ("hearing", "scheduled for hearing", "public hearing")
IN_COMMITTEE_KEYWORDS = ("referred to", "assigned to committee", "referred committee")
JUST_INTRODUCED_KEYWORDS = ("first read", "read the first time", "read in", "prefiled", "introduced")

def progress_label(status_code, history):
    if status_code == 3:
        return "Enrolled \u2014 awaiting governor"
    if status_code == 2:
        return "Passed one chamber"
    if status_code == 1:
        actions_text = " ".join((h.get("action") or "") for h in (history or [])).lower()
        if any(k in actions_text for k in COMMITTEE_CLEARED_KEYWORDS):
            return "Cleared committee"
        if any(k in actions_text for k in HEARING_KEYWORDS):
            return "Hearing scheduled"
        if any(k in actions_text for k in IN_COMMITTEE_KEYWORDS):
            return "In committee"
        if any(k in actions_text for k in JUST_INTRODUCED_KEYWORDS):
            return "Just introduced"
        return "Introduced"
    return LEGISCAN_STATUS.get(status_code, "Unknown")


MAX_BILL_TITLE_CHARS = 200  # LegiScan's "description" field is sometimes a full bill summary

def _shorten_title(text, limit=MAX_BILL_TITLE_CHARS):
    """Cap a bill title/description at ~limit chars, breaking on a word
    boundary rather than mid-word, so bill cards stay skimmable instead of
    dumping a full multi-sentence bill summary under the number."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;: ") + "\u2026"


def _fetch_legiscan_bills_for_state(state):
    """Worker for one state - runs on its own thread with its own session,
    so a slow/failing state can't block the rest. Returns (state, data_or_None)."""
    abbr = STATE_ABBR[state]
    session = requests.Session()
    try:
        search = session.get(
            "https://api.legiscan.com/",
            params={"key": LEGISCAN_API_KEY, "op": "getSearch", "state": abbr, "query": LEGISCAN_QUERY},
            timeout=20,
        ).json()
    except Exception as e:
        return state, None, f"search failed: {e}"

    hits = (search.get("searchresult") or {})
    bill_ids = [v["bill_id"] for k, v in hits.items() if k.isdigit()][:MAX_BILLS_PER_STATE]

    in_progress, failed, passed = [], [], []
    for bill_id in bill_ids:
        try:
            detail = session.get(
                "https://api.legiscan.com/",
                params={"key": LEGISCAN_API_KEY, "op": "getBill", "id": bill_id},
                timeout=20,
            ).json()
        except Exception:
            continue

        bill = detail.get("bill")
        if not bill:
            continue
        status_code = bill.get("status")
        status_label = LEGISCAN_STATUS.get(status_code, "Unknown")
        history = bill.get("history") or []
        title = bill.get("title", "")
        description = bill.get("description", "")
        # LegiScan's "title" is often a terse internal legislative caption
        # (e.g. Alaska's "Educ:enroll;charter Schools;bsa;telecomm") - the
        # "description" field is a fuller plain-language line and is far
        # more useful to display, when LegiScan has one.
        display_title = _shorten_title(description.strip() or title.strip())
        entry = {
            "bill": bill.get("bill_number", ""),
            "title": display_title,
            "status": status_label,
            "progress": progress_label(status_code, history),
            "lastAction": (history or [{}])[-1].get("action", ""),
            "lastActionDate": (history or [{}])[-1].get("date", ""),
            "implication": implication_for(f"{title} {description}"),
            "url": bill.get("url", ""),
        }
        if status_code in (1, 2, 3):
            in_progress.append(entry)
        elif status_code in (5, 6):
            failed.append(entry)
        elif status_code == 4:
            # LegiScan independently considers this bill finally passed.
            # Previously discarded and left entirely to the NCSL/Ballotpedia
            # enacted-law path - now captured so it can be cross-checked
            # against those sources and used to fill gaps where they're
            # stale or missing (see find_legiscan_enacted_gaps below).
            passed.append(entry)

    if in_progress or failed or passed:
        return state, {"in_progress": in_progress, "failed": failed, "passed": passed}, None
    return state, None, None


def fetch_legiscan_bills():
    """Returns {state_name: {"in_progress": [...], "failed": [...]}}. Runs
    across states in parallel (modest worker count - stays well within
    LegiScan's free-tier limits while cutting a ~51-state sequential loop
    down substantially) and prints progress as each state finishes, so the
    Actions log shows liveness instead of going silent for minutes."""
    if not LEGISCAN_API_KEY:
        print("LEGISCAN_API_KEY not set - skipping pending/failed bill lookup.")
        return {}

    results = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_legiscan_bills_for_state, s): s for s in US_STATES}
        for future in concurrent.futures.as_completed(futures):
            state = futures[future]
            done += 1
            try:
                _, data, err = future.result()
            except Exception as e:
                data, err = None, str(e)
            if err:
                print(f"  [{done}/{len(US_STATES)}] {state}: {err}")
            elif data:
                results[state] = data
                print(f"  [{done}/{len(US_STATES)}] {state}: {len(data['in_progress'])} in progress, {len(data['failed'])} failed, {len(data.get('passed', []))} passed (LegiScan)")
            else:
                print(f"  [{done}/{len(US_STATES)}] {state}: none found")

    # Merge in manually-verified bills LegiScan's search didn't surface,
    # de-duplicating by bill number so re-running never doubles them up.
    for state, known in KNOWN_BILLS.items():
        existing = results.setdefault(state, {"in_progress": [], "failed": [], "passed": []})
        for bucket in ("in_progress", "failed", "passed"):
            existing_numbers = {b.get("bill") for b in existing.get(bucket, [])}
            for entry in known.get(bucket, []):
                if entry["bill"] not in existing_numbers:
                    existing.setdefault(bucket, []).append(entry)

    return results


# ---------------------------------------------------------------------------
# LEGISCAN GAP-FILL  (independent detector for enacted legislation)
#
# NCSL and Ballotpedia are the primary sources for "which states currently
# have a cell phone law" - but they're periodic snapshots (NCSL in
# particular can go a while between updates) and can miss a bill entirely,
# or lag behind its passage. LegiScan's own search independently finds
# bills and reports when one has reached status 4 ("Passed"), regardless of
# whether NCSL or Ballotpedia have caught up yet. This cross-checks those
# LegiScan "Passed" bills against what NCSL/Ballotpedia already report for
# each state, and for any bill neither source has, synthesizes an
# NCSL-shaped entry so it flows through the exact same classification and
# full-bill-text pipeline as everything else - no separate code path to
# maintain, and no gap in coverage just because one source is stale.
# ---------------------------------------------------------------------------

def find_legiscan_enacted_gaps(legiscan_data, bp_data, ncsl_data):
    """Returns {state_name: synthetic_ncsl_entry} for states where LegiScan
    found a "Passed" bill and the state has NO existing NCSL or Ballotpedia
    entry at all. Deliberately state-level (not per-bill) rather than just
    bill-number-level: the data model only holds one enacted-law summary per
    state, so this only ever fills a genuinely empty slot - it will never
    overwrite a state NCSL/Ballotpedia already have real data for, even if
    they're tracking a different bill than the one LegiScan flagged. If a
    state already has coverage but the details are stale, that's a case for
    a manual COMPATIBILITY_OVERRIDES entry instead (see above), not this
    automated gap-fill."""
    gaps = {}
    for state, data in (legiscan_data or {}).items():
        passed_bills = data.get("passed") or []
        if not passed_bills:
            continue

        ncsl_entry = ncsl_data.get(state)
        bp_entries = bp_data.get(state) or []
        if ncsl_entry or bp_entries:
            continue  # state already has real coverage - never overwrite it here

        bill = passed_bills[0]  # one gap-fill bill per state is enough to close the hole
        gaps[state] = {
            "bill_number": bill.get("bill", ""),
            "summary": bill.get("title", ""),
            "category": "Enacted (LegiScan - not yet in NCSL/Ballotpedia)",
            "synthetic_source": "LegiScan",
        }
        print(f"  GAP FOUND: {state} {bill.get('bill')} - LegiScan reports this as passed, "
              f"but the state has no NCSL or Ballotpedia entry yet")

    return gaps


# ---------------------------------------------------------------------------
# FULL BILL TEXT  (LegiScan - pure code, no AI required)
#
# NCSL and Ballotpedia only give a short summary paragraph per bill, which
# can miss real implementation detail (e.g. Louisiana's "must be stowed in a
# locker/bag/purse, not a pocket" requirement wasn't in NCSL's summary, but
# IS in the actual statute text). Rather than needing a human or an AI to
# read the full bill, this pulls the real text via LegiScan (which you
# already have a free API key for) and runs it through the SAME keyword
# classifier used everywhere else in this file. This is what should catch
# the next Louisiana automatically, on its own, forever - no AI dependency.
# ---------------------------------------------------------------------------

import base64

def _extract_pdf_text(pdf_bytes):
    try:
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        print(f"    PDF text extraction failed: {e}")
        return ""


def _resolve_bill_id(session, state_abbr, bill_number):
    """Resolve a bill number like 'SB 207' (possibly with a trailing year,
    e.g. 'SB 207 (2024)') to a LegiScan bill_id via search."""
    clean_number = bill_number.split("(")[0].strip()
    if not clean_number:
        return None
    try:
        search = session.get(
            "https://api.legiscan.com/",
            params={"key": LEGISCAN_API_KEY, "op": "getSearch", "state": state_abbr, "query": clean_number},
            timeout=20,
        ).json()
        hits = search.get("searchresult") or {}
        for k, v in hits.items():
            if k.isdigit():
                return v.get("bill_id")
    except Exception as e:
        print(f"    Bill ID lookup failed for {state_abbr} {bill_number}: {e}")
    return None


def fetch_full_bill_text(session, bill_id):
    """Given a LegiScan bill_id, fetch its most recent text document and
    return plain text (decoding PDF if needed). Best-effort: returns "" on
    any failure so callers just fall back to the shorter summary text."""
    try:
        detail = session.get(
            "https://api.legiscan.com/",
            params={"key": LEGISCAN_API_KEY, "op": "getBill", "id": bill_id},
            timeout=20,
        ).json()
        bill = detail.get("bill") or {}
        texts = bill.get("texts") or []
        if not texts:
            return ""
        doc_id = texts[-1].get("doc_id")  # most recent version
        if not doc_id:
            return ""
        text_resp = session.get(
            "https://api.legiscan.com/",
            params={"key": LEGISCAN_API_KEY, "op": "getBillText", "id": doc_id},
            timeout=20,
        ).json()
        text_obj = text_resp.get("text") or {}
        doc_b64 = text_obj.get("doc")
        mime = (text_obj.get("mime") or "").lower()
        if not doc_b64:
            return ""
        raw = base64.b64decode(doc_b64)
        if "pdf" in mime:
            return _extract_pdf_text(raw)
        text = raw.decode("utf-8", errors="ignore")
        return re.sub(r"<[^>]+>", " ", text)  # crude tag strip if HTML
    except Exception as e:
        print(f"    Full bill text fetch failed for bill_id {bill_id}: {e}")
        return ""


def _fetch_full_text_for_state(state, entry):
    abbr = STATE_ABBR.get(state)
    bill_number = (entry.get("bill_number") or "").split(";")[0].split(",")[0].strip()
    if not abbr or not bill_number:
        return state, ""
    session = requests.Session()
    bill_id = _resolve_bill_id(session, abbr, bill_number)
    if not bill_id:
        return state, ""
    return state, fetch_full_bill_text(session, bill_id)


def fetch_full_texts_for_enacted(ncsl_data):
    """For every state with enacted legislation (per NCSL), try to pull the
    real statute text instead of relying on NCSL's short summary. Returns
    {state_name: full_text_string}. Skips entirely if no LegiScan key is
    set - same opt-in behavior as the pending/failed bill feature. Runs in
    parallel across states with progress printed as each finishes."""
    if not LEGISCAN_API_KEY:
        print("LEGISCAN_API_KEY not set - skipping full bill text lookup, using summaries only.")
        return {}

    results = {}
    items = list(ncsl_data.items())
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_full_text_for_state, s, e): s for s, e in items}
        for future in concurrent.futures.as_completed(futures):
            state = futures[future]
            done += 1
            try:
                _, text = future.result()
            except Exception as e:
                text = ""
                print(f"  [{done}/{len(items)}] {state}: ERROR {e}")
                continue
            if text:
                results[state] = text
                print(f"  [{done}/{len(items)}] {state}: full text retrieved ({len(text)} chars)")
            else:
                print(f"  [{done}/{len(items)}] {state}: not found, falling back to summary")
    return results


def _fetch_full_text_for_pending(state, bill_entry):
    return _fetch_full_text_for_state(state, {"bill_number": bill_entry.get("bill", "")})


def fetch_full_texts_for_pending(legiscan_data):
    """Same idea as fetch_full_texts_for_enacted(), but for each state's
    most-advanced pending bill (LegiScan's in_progress bucket, index 0)
    instead of an enacted one. This is what lets the Target States ranking
    read WHICH WAY a pending bill leans (see _direction_score) instead of
    just counting how many bills are moving - a state with several bills
    trending toward a hard physical mandate is a warning, not an
    opportunity, and bill count alone can't tell those apart. Best-effort
    and opt-in, same as the enacted-text fetch - skipped without a
    LegiScan key, and any state where the text can't be resolved just
    reads as 'unclear' downstream rather than failing."""
    if not LEGISCAN_API_KEY:
        return {}
    candidates = {
        state: data["in_progress"][0]
        for state, data in (legiscan_data or {}).items()
        if data.get("in_progress")
    }
    results = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_full_text_for_pending, s, e): s for s, e in candidates.items()}
        for future in concurrent.futures.as_completed(futures):
            state = futures[future]
            done += 1
            try:
                _, text = future.result()
            except Exception as e:
                text = ""
                print(f"  [{done}/{len(candidates)}] {state}: ERROR {e}")
                continue
            if text:
                results[state] = text
                print(f"  [{done}/{len(candidates)}] {state}: pending bill text retrieved ({len(text)} chars)")
            else:
                print(f"  [{done}/{len(candidates)}] {state}: pending bill text not found, direction will read as 'unclear'")
    return results


# ---------------------------------------------------------------------------
# CLASSIFY  (edit these lists to refine logic - this is the "rules", not raw data)
# ---------------------------------------------------------------------------

PHYSICAL_KEYWORDS = ["pouch", "locked bag", "lockable", "off their person", "stored off",
                      "yondr", "locker", "collect", "secure storage"]
SOFTWARE_KEYWORDS = ["app", "software", "technology solution", "digital tool", "management platform",
                      "focus mode", "device management"]
LOCAL_DISCRETION_KEYWORDS = ["encourag", "may adopt", "recommend", "local discretion", "district guidance"]
# Word-boundary keywords for funding, to avoid false hits like "grants permission" (contains "grant")
# or "fundamental" (contains "fund"). "$" is checked separately since \b doesn't apply to it.
FUNDING_WORD_KEYWORDS = ["grant program", "grant funding", "funding", "appropriation", "appropriated"]

def _contains_any(text, keywords):
    return any(k in text for k in keywords)

def _contains_funding(text):
    if "$" in text:
        return True
    return any(re.search(r"\b" + re.escape(k) + r"\b", text) for k in FUNDING_WORD_KEYWORDS)

# ---------------------------------------------------------------------------
# NEGATION/OPTIONALITY-AWARE KEYWORD SCANNING
#
# A flat "is this keyword anywhere in the text" check misreads phrasing like
# "lockers are NOT permitted" as a hard physical-storage mandate, or
# "students MAY use a locker" (optional) as if storage were required. This
# walks the text sentence-by-sentence and only counts a keyword hit as a
# real signal when it isn't immediately negated nearby, and separately flags
# hits that are explicitly optional rather than mandatory, so those two
# cases can be classified differently instead of both landing on "Hard"/
# "Restricted". Still pure keyword/regex logic - no AI involved - just a
# tighter scope (sentence + a small word-window) than a whole-document
# substring check.
# ---------------------------------------------------------------------------

NEGATION_CUES = ("not ", "n't ", " no ", "without ", "excluding ", "except ",
                  "prohibited from", "rather than", "instead of", "cannot ")
OPTIONAL_CUES = ("may ", "option", "optional", "elect to", "choice",
                  "at its discretion", "at their discretion", "if the district chooses",
                  "permitted but not required")
# Cues that introduce a non-exhaustive list of examples - e.g. "acceptable
# methods include lockers, pouches, or backpacks" or "such as a locked
# pouch". This is distinct from OPTIONAL_CUES because the cue can sit much
# earlier in the sentence than the keyword it's introducing ("may include X,
# Y, or Z" - by the time you reach "pouch" you can be 40+ chars past "may"),
# so it's checked against the whole sentence up to the keyword rather than a
# fixed window. Missing this was a real bug: statutory text almost always
# lists example compliance methods this way, and reading that as a mandate
# is exactly what caused California, Texas, Florida, and others to be
# misclassified as Restricted before this fix.
EXAMPLE_CUES = ("such as", "for example", "for instance", "e.g.", "including",
                 "methods include", "methods may include", "acceptable methods")

def _split_sentences(text):
    parts = re.split(r"(?<=[.;])\s+", text)
    return [p.strip() for p in parts if p.strip()]

def _has_cue_near(sentence_lower, idx, kw_len, cues, window=40):
    """Only looks at a small window immediately around the keyword match -
    not the whole sentence - so an unrelated cue elsewhere in a long
    sentence (e.g. 'No student MAY possess a device ... unless stored in a
    locker', where 'may' has nothing to do with the locker mention) doesn't
    wrongly tag an unrelated keyword hit."""
    start = max(0, idx - window)
    end = min(len(sentence_lower), idx + kw_len + window)
    context = sentence_lower[start:end]
    return any(cue in context for cue in cues)

def _scan_keyword_signals(text, keywords):
    """Returns (mandatory_hit, optional_hit) for a set of keywords across
    the whole text. mandatory_hit means at least one mention that isn't
    negated and isn't framed as optional/example nearby. optional_hit means
    a mention explicitly framed as optional ("may use a locker") or as one
    example among several ("methods include lockers, pouches, or
    backpacks") rather than negated outright - a genuinely different case
    from both "required" and "not allowed" that deserves its own
    classification path."""
    mandatory_hit = False
    optional_hit = False
    for sentence in _split_sentences(text):
        low = sentence.lower()
        for kw in keywords:
            idx = low.find(kw)
            if idx == -1:
                continue
            if _has_cue_near(low, idx, len(kw), NEGATION_CUES):
                continue  # explicitly ruled out - not a signal either way
            # Example-listing cues are checked against everything earlier in
            # the sentence (not a fixed window) since "methods may include
            # lockers, pouches, or backpacks" can put real distance between
            # the cue and the keyword it's introducing.
            preceding = low[:idx]
            if any(cue in preceding for cue in EXAMPLE_CUES):
                optional_hit = True
            elif _has_cue_near(low, idx, len(kw), OPTIONAL_CUES):
                optional_hit = True
            else:
                mandatory_hit = True
    return mandatory_hit, optional_hit

# Manual overrides for states where ground-truth research contradicts what the
# automated NCSL/Ballotpedia summary text implies. Legislative summaries very
# often describe *that* districts must adopt a restriction policy without
# describing *how* (pouches vs. software) - the keyword heuristic below can
# only classify what the summary text actually says, so a state's real-world
# implementation sometimes needs a human-verified correction here. Add an
# entry any time you (or your team) confirm a state's actual compliance
# mechanism from a direct source (news coverage, state DOE guidance, district
# policy docs) - cite it in "note" so the next person knows why it's here and
# can re-check it later. This dict always wins over the automated guess.
COMPATIBILITY_OVERRIDES = {
    "California": {
        "DoormanCompatibility": "Ambiguous",
        "BanType": "Local discretion",
        "LegislationStatus": "Enacted",
        "verified_against_bill": "AB 3216",
        "note": "AB 3216 (the Phone-Free Schools Act, signed Sept 2024, "
                 "compliance deadline July 1, 2026) requires every district, "
                 "county office of education, and charter school to adopt a "
                 "policy limiting or prohibiting student smartphone use - but "
                 "the law explicitly leaves the compliance METHOD to local "
                 "school boards, ranging from Yondr-style pouches to simply "
                 "requiring phones be off and zipped in a backpack. Because "
                 "software solutions aren't precluded, this doesn't meet the "
                 "bar for a hard physical-storage mandate. The keyword "
                 "classifier previously misread this as Restricted, almost "
                 "certainly because the actual bill text lists a locked pouch "
                 "as one EXAMPLE compliant method rather than a requirement - "
                 "exactly the 'optional, not mandatory' case the classifier's "
                 "negation/optionality scan is meant to catch. Overriding "
                 "directly here in case that scan still doesn't fully resolve "
                 "it against this bill's specific phrasing. "
                 "Source: Governor of California press release (gov.ca.gov, "
                 "Sept 2024); Fox 11 LA and KTLA coverage of the July 2026 "
                 "compliance deadline.",
    },
    "Colorado": {
        "DoormanCompatibility": "Ambiguous",
        "BanType": "Local discretion",
        "LegislationStatus": "Enacted",
        "verified_against_bill": "HB25-1135",
        "note": "HB25-1135 (compliance deadline July 1, 2026) requires every "
                 "local board of education and charter school to adopt, "
                 "implement, and post a communication-device policy - but the "
                 "bill text does not mandate a specific storage method; that's "
                 "left to each district (the Colorado Department of Education "
                 "publishes resources to help districts write their own "
                 "policy, it doesn't prescribe pouches or lockers itself). Not "
                 "a hard physical-storage mandate, so software solutions "
                 "aren't precluded. Same likely root cause as California: "
                 "actual bill/summary text probably lists physical storage as "
                 "an example a district could choose, which the keyword "
                 "classifier previously misread as a requirement. "
                 "Source: Colorado Department of Education "
                 "(ed.cde.state.co.us/communication-devices-in-schools); The "
                 "Prowers Journal coverage of district implementation, "
                 "June 2026.",
    },
    "Texas": {
        "DoormanCompatibility": "Ambiguous",
        "BanType": "Local discretion",
        "LegislationStatus": "Enacted",
        "verified_against_bill": "HB 1481",
        "note": "HB 1481 (effective 2025-26 school year) requires every district "
                 "to adopt a policy prohibiting personal device use, but leaves "
                 "the storage method to each district - Dallas ISD chose "
                 "magnetic pouches, Ector County ISD provides no storage at all "
                 "(students responsible for keeping devices out of sight), and "
                 "some elementary campuses use teacher collection. Not a "
                 "statewide physical-storage mandate. "
                 "Source: Click2Houston, Fox 4 Dallas-Fort Worth, Texas "
                 "Tribune coverage of district rollout, 2025.",
    },
    "Florida": {
        "DoormanCompatibility": "Ambiguous",
        "BanType": "Local discretion",
        "LegislationStatus": "Enacted",
        "verified_against_bill": "HB 1105",
        "note": "HB 1105 (effective 2025-26 school year) bans phone use K-8 "
                 "bell-to-bell and during HS instructional time, but doesn't "
                 "mandate a storage method - Lee County requires phones zipped "
                 "in backpacks (no locked container), while Escambia County "
                 "evaluated and rejected Yondr pouches as cost-prohibitive. "
                 "Source: NorthEscambia.com, WINK News coverage, 2025-2026.",
    },
    "Illinois": {
        "DoormanCompatibility": "Ambiguous",
        "BanType": "Local discretion",
        "LegislationStatus": "Enacted",
        "verified_against_bill": "SB 2427",
        "note": "SB 2427, signed by Gov. Pritzker July 28, 2026, requires a "
                 "bell-to-bell policy for grades K-8 (high schools have the "
                 "option to restrict instructional-time use). ISBE's template "
                 "policy is due Sept 1, 2026 and full implementation isn't "
                 "required until the 2027-28 school year - no statewide storage "
                 "method specified. This is a very recent law that may not yet "
                 "be reflected in NCSL/Ballotpedia; added as an override so it "
                 "isn't missed or misclassified in the meantime. "
                 "Source: Fox2Now, CBS Chicago, Capitol News Illinois, "
                 "July 2026.",
    },
    "Ohio": {
        "DoormanCompatibility": "Ambiguous",
        "BanType": "Local discretion",
        "LegislationStatus": "Enacted",
        "note": "Ohio law required every district to adopt a policy prohibiting "
                 "phone use for the entire school day by Jan 1, 2026, but "
                 "storage method is left to districts - most use lockers or "
                 "backpacks, some use pouches, and at least one district "
                 "(Garfield Heights) is moving away from Yondr toward cheaper "
                 "alternatives. Not a statewide physical-storage mandate. "
                 "Source: Ohio Dept. of Education (education.ohio.gov), Ohio "
                 "Capital Journal, 2026.",
    },
    "North Carolina": {
        "DoormanCompatibility": "Ambiguous",
        "BanType": "Local discretion",
        "LegislationStatus": "Enacted",
        "verified_against_bill": "HB 959",
        "note": "HB 959 (Session Law 2025-38) required districts to set a "
                 "policy by Jan 1, 2026 prohibiting device use/display during "
                 "instructional time - the law's only requirement is the "
                 "prohibition itself; storage method (backpacks, lockers, "
                 "pouches, or none) is explicitly left to each district. "
                 "Source: Wake Forest Law Review, Axios Raleigh, ABC11, "
                 "2025-2026.",
    },
    "Michigan": {
        "DoormanCompatibility": "Ambiguous",
        "BanType": "Local discretion",
        "LegislationStatus": "Enacted",
        "verified_against_bill": "HB 4141",
        "note": "HB 4141, signed by Gov. Whitmer Feb 2026, effective the "
                 "2026-27 school year, requires districts to adopt a wireless "
                 "communications device policy - method is not specified in "
                 "the law itself. "
                 "Source: CBS Detroit, Bridge Michigan, Michigan Public, 2026.",
    },
    "Virginia": {
        "DoormanCompatibility": "Ambiguous",
        "BanType": "Soft",
        "LegislationStatus": "Enacted",
        "verified_against_bill": "SB108",
        "note": "HB1961/SB738 (2025) and SB108 (effective July 1, 2026) codify "
                 "a bell-to-bell policy requiring phones \"off and stored away\" "
                 "for the full school day. This is less clearly method-open "
                 "than most other 2025-26 bell-to-bell states - \"stored away\" "
                 "leans toward physical removal from the student's access, and "
                 "at least some districts (e.g. parts of Arlington Public "
                 "Schools) are implementing it with pouches. Other districts "
                 "appear to be using simple backpack storage rather than a "
                 "locked container, so a network-level approach may or may not "
                 "satisfy a given district's literal reading - worth validating "
                 "directly with a target district before investing heavily "
                 "here. Classified Ambiguous rather than Restricted since nothing "
                 "found specifies a locked/physical container as a requirement. "
                 "Source: FFXnow, K-12 Dive, Fairfax County Public Schools, "
                 "Arlington Public Schools, 2025-2026.",
    },
    "Georgia": {
        "DoormanCompatibility": "Restricted",
        "BanType": "Hard",
        "LegislationStatus": "Enacted",
        "verified_against_bill": "HB 340",
        "note": "HB 340 (K-8, effective July 2026) explicitly requires devices be "
                 "\"powered off and stored in lockers, locked pouches, or designated "
                 "areas\" - not just prohibited from use, an actual physical-storage "
                 "method requirement. The Georgia Department of Education has also "
                 "confirmed lockable phone storage solutions are specifically "
                 "eligible for state safety grant funding, reinforcing that the "
                 "state expects physical storage rather than leaving the method "
                 "open. HB 1009 extends the same framework to high schools starting "
                 "2027-28. This is materially more storage-specific than the "
                 "Local-discretion states above (e.g. Texas, Ohio, Michigan) and is "
                 "close enough to New York/Louisiana's fact pattern to classify as "
                 "Restricted rather than Ambiguous. "
                 "Source: GovTech, Georgia Recorder, Atlanta News First, 13WMAZ, "
                 "Capitol Beat, 2025-2026.",
    },
    "New York": {
        "DoormanCompatibility": "Restricted",
        "BanType": "Hard",
        "LegislationStatus": "Enacted",
        "note": "New York's statewide policy (announced May 2025, effective "
                 "2025-26 school year) requires phones to be stored away/off for "
                 "the entire school day - schools are implementing this with "
                 "physical pouches or lockers. Confirmed incompatible with a "
                 "software-only approach. The NCSL/Ballotpedia summary text alone "
                 "doesn't spell this out, which is why this state needed a manual "
                 "override rather than relying on the keyword heuristic.",
    },
    "Louisiana": {
        "DoormanCompatibility": "Restricted",
        "BanType": "Hard",
        "verified_against_bill": "SB 207",
        "note": "SB 207 (2024) is more explicit than the NCSL/Ballotpedia summary "
                 "text captures: the full statute requires phones be \"turned off "
                 "and properly stowed away\" in the student's locker, school bag, "
                 "or purse for the entire instructional day - explicitly NOT in a "
                 "pocket, since that counts as \"on their person.\" That's "
                 "functionally incompatible with Doorman's keep-possession, "
                 "tap-to-activate model, even though it doesn't use the word "
                 "\"pouch.\" Source: KATC/Fox8/WBRZ coverage of SB 207 "
                 "implementation, 2024.",
    },
    "Hawaii": {
        "DoormanCompatibility": "Ambiguous",
        "BanType": "Soft",
        "LegislationStatus": "Enacted (Board Policy)",
        "note": "Hawaii's restriction came from a Board of Education policy "
                 "(Policy 301-11, adopted Feb 12, 2026), not a bill - so it's "
                 "invisible to NCSL and LegiScan, which only track legislation. "
                 "Effective for the 2026-27 school year: cell phone use is "
                 "prohibited during school hours (elementary/middle) or "
                 "instructional time (high school), with exceptions for "
                 "emergencies, health, and IEP needs. HIDOE has not yet published "
                 "implementation guidance specifying a storage method (pouches vs. "
                 "software), so compatibility genuinely is undetermined right now "
                 "- worth re-checking once that guidance comes out. "
                 "Source: hawaiipublicschools.org, Feb 2026.",
    },
}

# Bills known from direct research that LegiScan's per-state search may not
# surface in its top MAX_BILLS_PER_STATE results (e.g. a state with many
# competing bills, or a bill that ranked outside the relevance cutoff).
# These are merged into the LegiScan results regardless of search ranking.
# Add an entry here any time you manually confirm a bill LegiScan's search
# missed - include a source URL so it can be re-verified later.
KNOWN_BILLS = {
    "South Dakota": {
        "failed": [
            {
                "bill": "SB 198",
                "title": "Restrict the use of a cell phone by a student during the school day",
                "status": "Failed",
                "lastAction": "House Do Pass Amended, Failed, YEAS 28, NAYS 39",
                "lastActionDate": "2026-03-05",
                "implication": "Passed the Senate but failed in the House after "
                                 "passing committee - a real signal of how close "
                                 "this got, and worth watching if it's "
                                 "reintroduced next session. Bill text didn't "
                                 "specify a storage method either way.",
                "url": "https://legiscan.com/SD/bill/SB198/2026",
            }
        ],
    },
}


def _normalize_bill_number(raw):
    """Normalize a bill-number string for comparison across runs - e.g.
    'AB 3216', 'AB3216', and 'AB 3216 (2024)' should all compare equal.
    Used to detect when an override's cited bill has changed since it was
    written, which is the tripwire for a stale override (see classify())."""
    if not raw:
        return ""
    cleaned = re.sub(r"\(.*?\)", "", raw)  # drop a trailing "(2025)" year
    cleaned = re.sub(r"[^A-Za-z0-9;,]", "", cleaned).upper()
    return cleaned


def _combined_text(ballotpedia_entries, ncsl_entry, full_text=""):
    """Shared by classify() and compute_target_states() so both look at the
    exact same bp/ncsl/full-text blob - full_text (the actual statute, via
    LegiScan) is far more reliable than either summary when available, e.g.
    Louisiana's "locker/bag/purse, not a pocket" requirement that neither
    summary mentions on its own."""
    bp_text = " ".join(f"{e.get('type','')} {e.get('details','')}" for e in ballotpedia_entries).lower()
    ncsl_text = f"{ncsl_entry.get('category','')} {ncsl_entry.get('summary','')}".lower() if ncsl_entry else ""
    return f"{bp_text} {ncsl_text} {full_text.lower()}"


def classify(state, ballotpedia_entries, ncsl_entry, full_text=""):
    combined = _combined_text(ballotpedia_entries, ncsl_entry, full_text)

    if not ballotpedia_entries and not ncsl_entry:
        result = {
            "LegislationStatus": "No statewide policy",
            "BanType": "None",
            "DoormanCompatibility": "No Law",
            "Funding": "Unfunded",
        }
    else:
        # Sentence-level, negation/optionality-aware scan rather than a flat
        # substring check - see _scan_keyword_signals(). This is what tells
        # "lockers are not permitted" (negated - no signal) and "lockers can
        # be used as an option" (optional - not a mandate) apart from an
        # actual hard requirement like Alabama's "must be stored ... in a
        # locker".
        physical_mandatory, physical_optional = _scan_keyword_signals(combined, PHYSICAL_KEYWORDS)
        software_mandatory, software_optional = _scan_keyword_signals(combined, SOFTWARE_KEYWORDS)
        software_signal = software_mandatory or software_optional

        if any(k in combined for k in LOCAL_DISCRETION_KEYWORDS) and "statewide ban" not in combined:
            ban_type = "Local discretion"
        elif physical_mandatory:
            ban_type = "Hard"
        else:
            # Covers both "no physical-storage language at all" and "physical
            # storage mentioned but only as an optional method" - neither is
            # a hard mandate.
            ban_type = "Soft"

        if ban_type == "Hard":
            compatibility = "Restricted"
        elif software_signal:
            compatibility = "Permitted"
        elif ban_type == "Local discretion":
            compatibility = "No Law"
        elif physical_optional:
            # Physical storage is offered as one option, not required - a
            # real ambiguity for a software-only vendor, distinct from a
            # law that's silent on method entirely, but NOT a hard block.
            compatibility = "Ambiguous"
        else:
            # Soft, statewide "limit/restrict" language with no explicit method
            # named - genuinely ambiguous until district policy is written.
            # Flag for follow-up rather than guessing permitted vs restricted.
            compatibility = "Ambiguous"

        funding = "Funded" if _contains_funding(combined) else "Unfunded"

        status = "Enacted" if (ballotpedia_entries or ncsl_entry) else "No statewide policy"
        if "encourag" in combined and "requir" not in combined and "shall" not in combined:
            status = "Encouraged (non-binding)"

        result = {
            "LegislationStatus": status,
            "BanType": ban_type,
            "DoormanCompatibility": compatibility,
            "Funding": funding,
        }

    if state in COMPATIBILITY_OVERRIDES:
        override = COMPATIBILITY_OVERRIDES[state]
        result["DoormanCompatibility"] = override["DoormanCompatibility"]
        result["BanType"] = override["BanType"]
        if "LegislationStatus" in override:
            result["LegislationStatus"] = override["LegislationStatus"]
        result["ManuallyVerified"] = True
        result["VerificationNote"] = override["note"]

        # Staleness tripwire: an override is a snapshot of manual research
        # against a specific bill - it does NOT get re-verified automatically
        # just because the scraper runs daily. If that state's current bill
        # number (from live NCSL/Ballotpedia data) no longer matches what the
        # override was written against, something legislative has changed
        # since the override was researched. Rather than silently keep
        # trusting a possibly-outdated conclusion, flag it loudly - the
        # override still applies (a manually-verified-but-possibly-stale
        # read is still better than reverting to the raw keyword guess), but
        # both the log and the site surface that it needs a human look.
        verified_against = override.get("verified_against_bill", "")
        current_bill = ncsl_entry.get("bill_number", "") if ncsl_entry else ""
        needs_review = bool(verified_against) and bool(current_bill) and (
            _normalize_bill_number(verified_against) != _normalize_bill_number(current_bill)
        )
        result["OverrideNeedsReview"] = needs_review
        if needs_review:
            print(f"  WARNING: {state}'s override was verified against bill "
                  f"'{verified_against}' but current data shows '{current_bill}' - "
                  f"legislation may have changed. Override still applied; "
                  f"re-check COMPATIBILITY_OVERRIDES for {state}.")
    else:
        result["ManuallyVerified"] = False
        result["VerificationNote"] = ""
        result["OverrideNeedsReview"] = False

    return result


def build_records(bp_data, ncsl_data, legiscan_data=None, full_texts=None):
    legiscan_data = legiscan_data or {}
    full_texts = full_texts or {}
    records = []
    for state in US_STATES:
        bp_entries = bp_data.get(state, [])
        ncsl_entry = ncsl_data.get(state)
        full_text = full_texts.get(state, "")
        cls = classify(state, bp_entries, ncsl_entry, full_text)
        bills = ncsl_entry["bill_number"] if ncsl_entry else "; ".join(e["bill"] for e in bp_entries if e["bill"])
        details = ncsl_entry["summary"] if ncsl_entry else " | ".join(e["details"] for e in bp_entries if e["details"])
        is_gap_fill = bool(ncsl_entry and ncsl_entry.get("synthetic_source") == "LegiScan")
        sources = []
        if bp_entries:
            sources.append({"name": "Ballotpedia", "url": BALLOTPEDIA_URL})
        if ncsl_entry and not is_gap_fill:
            sources.append({"name": "NCSL", "url": NCSL_URL})
        elif is_gap_fill:
            # Don't misattribute this to NCSL - it's a bill LegiScan found as
            # passed that NCSL/Ballotpedia haven't listed yet, so it should
            # read as LegiScan-sourced, not NCSL-sourced.
            sources.append({"name": "LegiScan (auto-detected - not yet in NCSL/Ballotpedia)",
                             "url": f"https://legiscan.com/{STATE_ABBR.get(state,'')}"})
        if full_text:
            sources.append({"name": "Full bill text (LegiScan)", "url": f"https://legiscan.com/{STATE_ABBR.get(state,'')}"})
        lg = legiscan_data.get(state, {})
        records.append({
            "State": state,
            "DoormanCompatibility": cls["DoormanCompatibility"],
            "LegislationStatus": cls["LegislationStatus"],
            "BillNumbers": bills or "—",
            "BanType": cls["BanType"],
            "Funding": cls["Funding"],
            "ComplianceGuidance": "Yes" if "guidance" in (ncsl_entry.get("category","").lower() if ncsl_entry else "") else "No",
            "Details": details or "No statewide legislation found in current sources.",
            "Sources": sources,
            "LastChecked": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "InProgress": lg.get("in_progress", []),
            "Failed": lg.get("failed", []),
            "ManuallyVerified": cls.get("ManuallyVerified", False),
            "VerificationNote": cls.get("VerificationNote", ""),
            "FullTextChecked": bool(full_text),
            "LegiScanGapFill": is_gap_fill,
            "OverrideNeedsReview": cls.get("OverrideNeedsReview", False),
        })
    return records

# ---------------------------------------------------------------------------
# TARGET STATES SCORING (auto-generated, not a replacement for hand research)
#
# Turns the compatibility classification above into a directional "where to
# prioritize next" ranking, on five weighted factors: Feasibility (30%),
# Decision-Window Timing (30% -> 25%), Go-to-Market Concentration (20%),
# Legislative Direction (15%), and Market Size / TAM (15% -> 10%).
#
# All five factors are fully automatic and refresh nightly with the rest of
# the tracker - there is deliberately NO hand-maintained factor in this
# version. An earlier version included a Competitive Openness factor, but
# it required a person to read things like grant-program fine print
# ("this state's implementation grant only funds physical pouches, not
# software") that no scrapable source states anywhere - it was removed
# rather than faked with a shallow automated proxy. If a future revision
# wants that signal back, it needs a human, on a cadence, the same way
# COMPATIBILITY_OVERRIDES does.
#
# Feasibility is derived straight from the compatibility classification.
# TAM is a static NCES enrollment table. Timing is a best-effort regex scan
# of enacted bill text for an effective-date signal. Go-to-Market
# Concentration and Legislative Direction are new in this version:
#
#   - Go-to-Market Concentration reflects how Doorman actually sells, per
#     the investment memo: school by school (ICP = high schools of
#     200-2,000 students), not district by district, even though a
#     district "champion" often pushes the paperwork through. A state
#     full of large high schools near the top of that ICP band needs far
#     fewer individual deals to cover the same enrollment than a state
#     full of small, fragmented ones - see NCES_HS_STATS / _gtm_score.
#   - Legislative Direction reads the state's most-advanced pending bill
#     (if any) through the SAME physical/software/local-discretion keyword
#     scan used on enacted law (see _direction_score), rather than just
#     counting how many bills are moving. Momentum alone is directionless:
#     a state with three bills trending toward a hard pouch mandate is a
#     warning, not an opportunity. This is what tells those apart.
#
# Feasibility still acts as a hard gate underneath the weighting: a state
# classified "Restricted" is excluded from this ranking entirely, the same
# as the manual report - no amount of good timing, an efficient sales
# motion, favorable pending legislation, or TAM buys a genuinely
# incompatible state back onto the list.
# ---------------------------------------------------------------------------

EXISTING_CUSTOMER_STATES = {"Massachusetts", "New Jersey"}  # already Doorman markets, not expansion targets

# Fall 2023 public K-12 enrollment (NCES Digest of Education Statistics,
# Table 203.20 - the most recent finalized state-level table available as of
# this writing). A directional TAM proxy, not Doorman's actual addressable
# count. Left static on purpose - state enrollment moves by low single
# digits percent year over year, so this is safe to leave untouched for a
# year or more. Re-pull from https://nces.ed.gov/programs/digest/ when a
# newer finalized table is out.
NCES_ENROLLMENT = {
    "Alabama": 748650, "Alaska": 131243, "Arizona": 1117630, "Arkansas": 484978,
    "California": 5924113, "Colorado": 865661, "Connecticut": 512652, "Delaware": 141842,
    "District of Columbia": 92794, "Florida": 2872335, "Georgia": 1749701, "Hawaii": 169308,
    "Idaho": 316414, "Illinois": 1846264, "Indiana": 1032723, "Iowa": 508112, "Kansas": 483505,
    "Kentucky": 657520, "Louisiana": 708190, "Maine": 172545, "Maryland": 890122,
    "Massachusetts": 914958, "Michigan": 1426491, "Minnesota": 869967, "Mississippi": 436523,
    "Missouri": 891248, "Montana": 149291, "Nebraska": 329162, "Nevada": 479574,
    "New Hampshire": 166594, "New Jersey": 1392567, "New Mexico": 311719, "New York": 2533449,
    "North Carolina": 1544289, "North Dakota": 119033, "Ohio": 1675300, "Oklahoma": 698761,
    "Oregon": 572624, "Pennsylvania": 1692829, "Rhode Island": 136154, "South Carolina": 793860,
    "South Dakota": 141467, "Tennessee": 1004625, "Texas": 5532518, "Utah": 689883,
    "Vermont": 82455, "Virginia": 1258852, "Washington": 1093745, "West Virginia": 246883,
    "Wisconsin": 814202, "Wyoming": 91036,
}

# Number of regular operating public high schools and their average student
# membership, School Year 2022-23 (NCES Common Core of Data, Table 4:
# https://nces.ed.gov/ccd/tables/202223_summary_4.asp). "High School" here
# is NCES's own level classification, not a size filter - avgMembership is
# what lets _gtm_score compare each state's typical high school size against
# Doorman's stated ICP band (200-2,000 students per the investment memo).
# Static on purpose, same reasoning as NCES_ENROLLMENT - re-pull when NCES
# publishes a newer finalized edition of this table.
NCES_HS_STATS = {
    "Alabama": {"count": 302, "avgMembership": 705}, "Alaska": {"count": 55, "avgMembership": 487},
    "Arizona": {"count": 304, "avgMembership": 1083}, "Arkansas": {"count": 284, "avgMembership": 505},
    "California": {"count": 1381, "avgMembership": 1264}, "Colorado": {"count": 321, "avgMembership": 763},
    "Connecticut": {"count": 180, "avgMembership": 827}, "Delaware": {"count": 33, "avgMembership": 1073},
    "District of Columbia": {"count": 36, "avgMembership": 600},
    "Florida": {"count": 573, "avgMembership": 1495}, "Georgia": {"count": 441, "avgMembership": 1211},
    "Hawaii": {"count": 42, "avgMembership": 1207}, "Idaho": {"count": 128, "avgMembership": 665},
    "Illinois": {"count": 705, "avgMembership": 853}, "Indiana": {"count": 380, "avgMembership": 850},
    "Iowa": {"count": 324, "avgMembership": 487}, "Kansas": {"count": 333, "avgMembership": 448},
    "Kentucky": {"count": 218, "avgMembership": 888}, "Louisiana": {"count": 247, "avgMembership": 813},
    "Maine": {"count": 115, "avgMembership": 449}, "Maryland": {"count": 197, "avgMembership": 1339},
    "Massachusetts": {"count": 316, "avgMembership": 824}, "Michigan": {"count": 622, "avgMembership": 665},
    "Minnesota": {"count": 455, "avgMembership": 611}, "Mississippi": {"count": 200, "avgMembership": 659},
    "Missouri": {"count": 547, "avgMembership": 514}, "Montana": {"count": 171, "avgMembership": 265},
    "Nebraska": {"count": 267, "avgMembership": 393}, "Nevada": {"count": 119, "avgMembership": 1246},
    "New Hampshire": {"count": 98, "avgMembership": 541}, "New Jersey": {"count": 357, "avgMembership": 1071},
    "New Mexico": {"count": 178, "avgMembership": 518}, "New York": {"count": 1086, "avgMembership": 703},
    "North Carolina": {"count": 525, "avgMembership": 868}, "North Dakota": {"count": 166, "avgMembership": 220},
    "Ohio": {"count": 804, "avgMembership": 613}, "Oklahoma": {"count": 456, "avgMembership": 402},
    "Oregon": {"count": 234, "avgMembership": 713}, "Pennsylvania": {"count": 621, "avgMembership": 829},
    "Rhode Island": {"count": 55, "avgMembership": 749}, "South Carolina": {"count": 227, "avgMembership": 1026},
    "South Dakota": {"count": 158, "avgMembership": 255}, "Tennessee": {"count": 357, "avgMembership": 836},
    "Texas": {"count": 1400, "avgMembership": 1130}, "Utah": {"count": 155, "avgMembership": 1108},
    "Vermont": {"count": 48, "avgMembership": 495}, "Virginia": {"count": 320, "avgMembership": 1245},
    "Washington": {"count": 390, "avgMembership": 811}, "West Virginia": {"count": 111, "avgMembership": 715},
    "Wisconsin": {"count": 475, "avgMembership": 538}, "Wyoming": {"count": 62, "avgMembership": 456},
}

# Effective-date signal extraction - a best-effort heuristic, not a legal
# read. Looks for phrasing implementation guidance actually uses ("2027-28
# school year", "effective ... 2026"). Anything it can't confidently parse
# gets a neutral middle score rather than a guess.
_YEAR_RANGE_RE = re.compile(r"(20\d{2})\s*[-–]\s*\d{2,4}\s*school year")
_YEAR_NEAR_EFFECTIVE_RE = re.compile(
    r"(?:effective|effect|beginning|begins?|starting|by|no later than|as of).{0,25}?(20\d{2})", re.IGNORECASE)

def _extract_target_year(text):
    """Best-effort: the first plausible 'this law kicks in around year X'
    signal found in the combined bill text/summary, or None."""
    if not text:
        return None
    m = _YEAR_RANGE_RE.search(text)
    if m:
        return int(m.group(1))
    m = _YEAR_NEAR_EFFECTIVE_RE.search(text)
    if m:
        return int(m.group(1))
    return None

def _timing_score(text, current_year=None):
    current_year = current_year or datetime.now(timezone.utc).year
    year = _extract_target_year(text)
    if year is None:
        return 3, None  # no confident signal - neutral default, not a guess
    delta = year - current_year
    if delta <= -1:
        return 2, year   # already settled a year or more ago
    if delta == 0:
        return 5, year   # this school year - maximally urgent
    if delta == 1:
        return 4, year   # next school year - still a live decision window
    return 2, year        # 2+ years out - too far to call "next 1-2 years"

def _feasibility_score(compatibility, ban_type):
    if compatibility == "Restricted":
        return 1
    if compatibility in ("Permitted", "No Law"):
        return 5
    if compatibility == "Ambiguous" and ban_type == "Local discretion":
        return 4  # confirmed district-choice states, e.g. CA/TX/FL/OH pattern
    return 3  # Ambiguous, no confirmed local-discretion language - genuine uncertainty

def _tam_score(enrollment):
    if enrollment is None:
        return 1
    if enrollment >= 5_000_000:
        return 5
    if enrollment >= 2_000_000:
        return 4
    if enrollment >= 1_000_000:
        return 3
    if enrollment >= 500_000:
        return 2
    return 1

def _gtm_score(avg_hs_membership):
    """Doorman's ICP is high schools with 200-2,000 students (per the
    investment memo) - deployment happens school by school, not district
    by district (a district "champion" often pushes the paperwork through,
    but the unit of sale is the individual school). A state's AVERAGE high
    school size is a proxy for how many separate school relationships are
    needed to cover a given amount of enrollment: a state full of large
    schools near the top of the ICP band needs far fewer deals to cover the
    same TAM than a state full of small, fragmented ones. Scored by how
    close the state's average sits to the top of that band (2,000) -
    closer is more efficient, near the 200-student floor is the most
    fragmented."""
    if not avg_hs_membership:
        return 1
    band_floor, band_ceiling = 200, 2000
    fraction = (avg_hs_membership - band_floor) / (band_ceiling - band_floor)
    fraction = max(0.0, min(1.0, fraction))
    return round(1 + 4 * fraction)

def _direction_score(pending_text, has_pending_activity):
    """Reads the state's most-advanced pending bill (if any) through the
    same physical/software/local-discretion keyword scan classify() runs
    on enacted law, to tell a pending bill trending toward something
    Doorman can work with apart from one trending toward a hard physical
    mandate. Bill-count "momentum" alone can't make that distinction - a
    state with three bills all pointed at a pouch requirement is a warning,
    not an opportunity, and this is what catches that. Best-effort, same
    caveat as _timing_score: pending-bill text is often thinner than
    enacted statute, and bills change during markup, so treat this as a
    live, more uncertain signal than the enacted-law feasibility score.
    Returns (score, label)."""
    if not has_pending_activity:
        return 3, "no pending activity"
    if not pending_text or not pending_text.strip():
        return 3, "pending bill(s) found, but no text available yet to read direction"

    low = pending_text.lower()
    physical_mandatory, physical_optional = _scan_keyword_signals(low, PHYSICAL_KEYWORDS)
    software_mandatory, software_optional = _scan_keyword_signals(low, SOFTWARE_KEYWORDS)
    local_discretion = any(k in low for k in LOCAL_DISCRETION_KEYWORDS)

    if physical_mandatory:
        return 1, "pending bill leans toward a physical-storage mandate"
    if software_mandatory or software_optional or local_discretion:
        return 5, "pending bill leans open (software-friendly language or local discretion)"
    if physical_optional:
        return 3, "pending bill mentions physical storage only as one option - unclear"
    return 3, "pending bill text doesn't clearly signal either direction"

def compute_target_states(records, combined_text_by_state, legiscan_data=None, pending_text_by_state=None):
    """records: output of build_records(). combined_text_by_state: dict of
    state -> the same bp/ncsl/full-text blob classify() scanned (see
    _combined_text). legiscan_data / pending_text_by_state: used to read
    Legislative Direction from each state's most-advanced pending bill."""
    legiscan_data = legiscan_data or {}
    pending_text_by_state = pending_text_by_state or {}
    ranked = []
    for rec in records:
        state = rec["State"]
        if state in EXISTING_CUSTOMER_STATES:
            continue
        if rec["DoormanCompatibility"] == "Restricted":
            continue  # hard gate - see module docstring above

        feasibility = _feasibility_score(rec["DoormanCompatibility"], rec["BanType"])
        timing, timing_year = _timing_score(combined_text_by_state.get(state, ""))

        hs_stats = NCES_HS_STATS.get(state)
        gtm = _gtm_score(hs_stats["avgMembership"] if hs_stats else None)

        pending_bills = (legiscan_data.get(state) or {}).get("in_progress") or []
        direction, direction_label = _direction_score(
            pending_text_by_state.get(state, ""), bool(pending_bills))

        enrollment = NCES_ENROLLMENT.get(state)
        tam = _tam_score(enrollment)

        weighted = round(
            feasibility * 0.30 + timing * 0.25 + gtm * 0.20 + direction * 0.15 + tam * 0.10, 2
        )

        ranked.append({
            "State": state,
            "WeightedScore": weighted,
            "Feasibility": {"score": feasibility, "auto": True},
            "Timing": {"score": timing, "auto": True, "detectedYear": timing_year,
                       "confidence": "detected" if timing_year else "no signal found - neutral default"},
            "GoToMarket": {"score": gtm, "auto": True,
                           "avgHighSchoolSize": hs_stats["avgMembership"] if hs_stats else None,
                           "highSchoolCount": hs_stats["count"] if hs_stats else None},
            "LegislativeDirection": {"score": direction, "auto": True, "label": direction_label,
                                      "pendingBill": pending_bills[0]["bill"] if pending_bills else None},
            "TAM": {"score": tam, "auto": True, "enrollment": enrollment},
            "DoormanCompatibility": rec["DoormanCompatibility"],
            "BanType": rec["BanType"],
            "BillNumbers": rec["BillNumbers"],
        })

    ranked.sort(key=lambda r: r["WeightedScore"], reverse=True)
    for i, r in enumerate(ranked, start=1):
        r["Rank"] = i
    return ranked


# ---------------------------------------------------------------------------
# DIFF + WRITE
# ---------------------------------------------------------------------------

TRACKED_FIELDS = ["DoormanCompatibility", "LegislationStatus", "BanType", "Funding", "BillNumbers"]

def _bill_numbers(entries):
    return sorted(e.get("bill") for e in entries)

def diff_records(old_records, new_records):
    old_by_state = {r["State"]: r for r in old_records} if old_records else {}
    changes = []
    for new in new_records:
        old = old_by_state.get(new["State"])
        if old is None:
            changes.append({"state": new["State"], "field": "new", "old": None, "new": "added to tracker"})
            continue
        for field in TRACKED_FIELDS:
            if old.get(field) != new.get(field):
                changes.append({"state": new["State"], "field": field, "old": old.get(field), "new": new.get(field)})
        # For in-progress/failed bills, only flag when the *set of bill numbers*
        # changes (new bill appears, one disappears/resolves) - not every minor
        # last-action text tweak, to keep the change log readable.
        old_ip, new_ip = _bill_numbers(old.get("InProgress", [])), _bill_numbers(new.get("InProgress", []))
        if old_ip != new_ip:
            changes.append({"state": new["State"], "field": "InProgress", "old": ", ".join(old_ip) or "none", "new": ", ".join(new_ip) or "none"})
        old_f, new_f = _bill_numbers(old.get("Failed", [])), _bill_numbers(new.get("Failed", []))
        if old_f != new_f:
            changes.append({"state": new["State"], "field": "Failed", "old": ", ".join(old_f) or "none", "new": ", ".join(new_f) or "none"})
    return changes


def main():
    print("Fetching Ballotpedia...")
    try:
        bp_data = fetch_ballotpedia()
        print(f"  {len(bp_data)} states with entries")
    except Exception as e:
        # Ballotpedia sits behind AWS WAF bot-detection that a plain HTTP
        # request can't solve (it returns a JS challenge page, not real
        # content). Treat it as a nice-to-have supplement rather than a
        # hard dependency - NCSL alone covers enacted legislation for
        # 40+ states/DC and is enough to keep the tracker useful.
        print(f"  WARNING: Ballotpedia fetch failed, continuing without it: {e}")
        bp_data = {}

    print("Fetching NCSL...")
    ncsl_data = fetch_ncsl()
    print(f"  {len(ncsl_data)} states with entries")
    print("Fetching in-progress/failed/passed bills (LegiScan)...")
    legiscan_data = fetch_legiscan_bills()
    print(f"  {len(legiscan_data)} states with pending/failed/passed activity")

    print("Cross-checking LegiScan-passed bills against NCSL/Ballotpedia for gaps...")
    gap_fills = find_legiscan_enacted_gaps(legiscan_data, bp_data, ncsl_data)
    for state, entry in gap_fills.items():
        ncsl_data[state] = entry
    print(f"  {len(gap_fills)} gap(s) filled from LegiScan" if gap_fills else "  no gaps found - NCSL/Ballotpedia already cover everything LegiScan sees as passed")

    print("Fetching full bill text for enacted legislation (LegiScan)...")
    full_texts = fetch_full_texts_for_enacted(ncsl_data)
    print(f"  {len(full_texts)} states with full text retrieved (out of {len(ncsl_data)} with a bill number)")

    new_records = build_records(bp_data, ncsl_data, legiscan_data, full_texts)

    old = {}
    if DATA_FILE.exists():
        old = json.loads(DATA_FILE.read_text())
    old_records = old.get("states", [])
    changes = diff_records(old_records, new_records)

    now = datetime.now(timezone.utc).isoformat()
    changelog = old.get("changelog", [])
    if changes:
        changelog.insert(0, {"timestamp": now, "changes": changes})
        changelog = changelog[:100]  # cap history

    sources = [BALLOTPEDIA_URL, NCSL_URL]
    if LEGISCAN_API_KEY:
        sources.append("https://legiscan.com/legiscan (pending/failed bills)")

    print("Fetching pending-bill text for Legislative Direction (LegiScan)...")
    pending_texts = fetch_full_texts_for_pending(legiscan_data)
    print(f"  {len(pending_texts)} state(s) with pending-bill text retrieved")

    print("Scoring target states (feasibility/timing/go-to-market/direction/TAM)...")
    combined_text_by_state = {
        state: _combined_text(bp_data.get(state, []), ncsl_data.get(state), full_texts.get(state, ""))
        for state in US_STATES
    }
    target_states = compute_target_states(new_records, combined_text_by_state, legiscan_data, pending_texts)

    output = {
        "meta": {
            "lastUpdated": now,
            "sources": sources,
            "changeCount": len(changes),
            "legiscanEnabled": bool(LEGISCAN_API_KEY),
        },
        "changelog": changelog,
        "states": new_records,
        "targetStates": target_states,
        "targetStatesMeta": {
            "weights": {"feasibility": 0.30, "timing": 0.25, "goToMarket": 0.20,
                        "legislativeDirection": 0.15, "tam": 0.10},
            "excludedExisting": sorted(EXISTING_CUSTOMER_STATES),
            "generatedAt": now,
            "note": "All five factors are fully automatic and refresh nightly - Feasibility and TAM from "
                    "the classification/enrollment data above, Timing from a best-effort bill-text date "
                    "scan, Go-to-Market Concentration from NCES high-school-size data (Doorman sells "
                    "school by school per the investment memo, not district by district), and "
                    "Legislative Direction from reading each state's most-advanced pending bill through "
                    "the same physical/software keyword scan used on enacted law.",
        },
    }
    DATA_FILE.write_text(json.dumps(output, indent=2))
    print(f"Wrote {DATA_FILE} - {len(changes)} field changes since last run")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
