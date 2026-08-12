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
import json
import re
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

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
    t = text.lower()
    if any(k in t for k in PHYSICAL_KEYWORDS):
        return "Would require physical device storage if passed \u2014 incompatible with a software-only approach."
    if any(k in t for k in SOFTWARE_KEYWORDS):
        return "Would explicitly allow a technology/software approach if passed \u2014 favorable for Doorman."
    if any(k in t for k in LOCAL_DISCRETION_KEYWORDS):
        return "Would leave the method up to districts if passed \u2014 neutral, still sellable."
    return "Would require a restriction policy if passed; specific method not yet defined."


def fetch_legiscan_bills():
    """Returns {state_name: {"in_progress": [...], "failed": [...]}}."""
    if not LEGISCAN_API_KEY:
        print("LEGISCAN_API_KEY not set - skipping pending/failed bill lookup.")
        return {}

    results = {}
    session = requests.Session()
    for state in US_STATES:
        abbr = STATE_ABBR[state]
        try:
            search = session.get(
                "https://api.legiscan.com/",
                params={"key": LEGISCAN_API_KEY, "op": "getSearch", "state": abbr, "query": LEGISCAN_QUERY},
                timeout=20,
            ).json()
        except Exception as e:
            print(f"  LegiScan search failed for {state}: {e}")
            continue

        hits = (search.get("searchresult") or {})
        bill_ids = [v["bill_id"] for k, v in hits.items() if k.isdigit()][:MAX_BILLS_PER_STATE]

        in_progress, failed = [], []
        for bill_id in bill_ids:
            try:
                detail = session.get(
                    "https://api.legiscan.com/",
                    params={"key": LEGISCAN_API_KEY, "op": "getBill", "id": bill_id},
                    timeout=20,
                ).json()
            except Exception as e:
                print(f"  LegiScan getBill failed for {state} bill {bill_id}: {e}")
                continue

            bill = detail.get("bill")
            if not bill:
                continue
            status_code = bill.get("status")
            status_label = LEGISCAN_STATUS.get(status_code, "Unknown")
            title = bill.get("title", "")
            description = bill.get("description", "")
            entry = {
                "bill": bill.get("bill_number", ""),
                "title": title,
                "status": status_label,
                "lastAction": (bill.get("history") or [{}])[-1].get("action", ""),
                "lastActionDate": (bill.get("history") or [{}])[-1].get("date", ""),
                "implication": implication_for(f"{title} {description}"),
                "url": bill.get("url", ""),
            }
            if status_code in (1, 2, 3):
                in_progress.append(entry)
            elif status_code in (5, 6):
                failed.append(entry)
            # status 4 (Passed) is left to the Ballotpedia/NCSL enacted-law path

        if in_progress or failed:
            results[state] = {"in_progress": in_progress, "failed": failed}

    # Merge in manually-verified bills LegiScan's search didn't surface,
    # de-duplicating by bill number so re-running never doubles them up.
    for state, known in KNOWN_BILLS.items():
        existing = results.setdefault(state, {"in_progress": [], "failed": []})
        for bucket in ("in_progress", "failed"):
            existing_numbers = {b.get("bill") for b in existing.get(bucket, [])}
            for entry in known.get(bucket, []):
                if entry["bill"] not in existing_numbers:
                    existing.setdefault(bucket, []).append(entry)

    return results


# ---------------------------------------------------------------------------
# RECENT NEWS  (Google News RSS - free, no API key)
# ---------------------------------------------------------------------------

NEWS_QUERY = '"cell phone" OR smartphone school ban policy law students'
MAX_NEWS_ITEMS = 5

def fetch_recent_news():
    """Pull real, linked news headlines from Google News' public RSS search -
    no API key needed. Best-effort: if Google blocks/changes this (same class
    of issue as the Ballotpedia WAF block), log a warning and return an empty
    list rather than failing the whole run."""
    import urllib.parse
    import xml.etree.ElementTree as ET
    from datetime import datetime as dt

    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(NEWS_QUERY) + "&hl=en-US&gl=US&ceid=US:en"
    try:
        html = _get_html(url)
        root = ET.fromstring(html)
    except Exception as e:
        print(f"  WARNING: recent news fetch failed, continuing without it: {e}")
        return []

    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""
        if not title or not link:
            continue
        try:
            pub_date = dt.strptime(pub_date_raw, "%a, %d %b %Y %H:%M:%S %Z")
            pub_date_iso = pub_date.isoformat()
        except Exception:
            pub_date_iso = None

        domain = ""
        try:
            domain = urllib.parse.urlparse(link).netloc.replace("www.", "")
        except Exception:
            pass
        favicon = f"https://www.google.com/s2/favicons?domain={domain}&sz=64" if domain else ""

        items.append({
            "title": title,
            "link": link,
            "source": source or domain,
            "publishedAt": pub_date_iso,
            "image": favicon,
        })

    items.sort(key=lambda x: x["publishedAt"] or "", reverse=True)

    # light dedupe by domain so 5 slots aren't dominated by one wire service
    seen_domains = set()
    deduped = []
    for it in items:
        d = it["source"]
        if d in seen_domains and len(deduped) < MAX_NEWS_ITEMS:
            continue
        seen_domains.add(d)
        deduped.append(it)
        if len(deduped) >= MAX_NEWS_ITEMS:
            break

    return deduped or items[:MAX_NEWS_ITEMS]


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


def classify(state, ballotpedia_entries, ncsl_entry):
    bp_text = " ".join(f"{e.get('type','')} {e.get('details','')}" for e in ballotpedia_entries).lower()
    ncsl_text = f"{ncsl_entry.get('category','')} {ncsl_entry.get('summary','')}".lower() if ncsl_entry else ""
    combined = f"{bp_text} {ncsl_text}"

    if not ballotpedia_entries and not ncsl_entry:
        result = {
            "LegislationStatus": "No statewide policy",
            "BanType": "None",
            "DoormanCompatibility": "No Law",
            "Funding": "Unfunded",
        }
    else:
        if any(k in combined for k in LOCAL_DISCRETION_KEYWORDS) and "statewide ban" not in combined:
            ban_type = "Local discretion"
        elif any(k in combined for k in PHYSICAL_KEYWORDS):
            ban_type = "Hard"
        else:
            ban_type = "Soft"

        if ban_type == "Hard":
            compatibility = "Restricted"
        elif any(k in combined for k in SOFTWARE_KEYWORDS):
            compatibility = "Permitted"
        elif ban_type == "Local discretion":
            compatibility = "No Law"
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
    else:
        result["ManuallyVerified"] = False
        result["VerificationNote"] = ""

    return result


def build_records(bp_data, ncsl_data, legiscan_data=None):
    legiscan_data = legiscan_data or {}
    records = []
    for state in US_STATES:
        bp_entries = bp_data.get(state, [])
        ncsl_entry = ncsl_data.get(state)
        cls = classify(state, bp_entries, ncsl_entry)
        bills = ncsl_entry["bill_number"] if ncsl_entry else "; ".join(e["bill"] for e in bp_entries if e["bill"])
        details = ncsl_entry["summary"] if ncsl_entry else " | ".join(e["details"] for e in bp_entries if e["details"])
        sources = []
        if bp_entries:
            sources.append({"name": "Ballotpedia", "url": BALLOTPEDIA_URL})
        if ncsl_entry:
            sources.append({"name": "NCSL", "url": NCSL_URL})
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
        })
    return records

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
    print("Fetching in-progress/failed bills (LegiScan)...")
    legiscan_data = fetch_legiscan_bills()
    print(f"  {len(legiscan_data)} states with pending/failed activity")

    print("Fetching recent news...")
    news = fetch_recent_news()
    print(f"  {len(news)} news items")

    new_records = build_records(bp_data, ncsl_data, legiscan_data)

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

    output = {
        "meta": {
            "lastUpdated": now,
            "sources": sources,
            "changeCount": len(changes),
            "legiscanEnabled": bool(LEGISCAN_API_KEY),
        },
        "changelog": changelog,
        "news": news,
        "states": new_records,
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
