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
from pathlib import Path

import pandas as pd
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DoormanTrackerBot/1.0; +https://ballotpedia.org)"}
BALLOTPEDIA_URL = "https://ballotpedia.org/State_policies_on_cellphone_use_in_K-12_public_schools"
NCSL_URL = "https://www.ncsl.org/education/enacted-state-legislation-cellphone-use-in-schools"

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
MAX_BILLS_PER_STATE = 4  # keeps API usage well under the free 30k/month cap

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
    resp = requests.get(BALLOTPEDIA_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    tables = pd.read_html(resp.text)
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
    resp = requests.get(NCSL_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    tables = pd.read_html(resp.text)
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

def classify(state, ballotpedia_entries, ncsl_entry):
    bp_text = " ".join(f"{e.get('type','')} {e.get('details','')}" for e in ballotpedia_entries).lower()
    ncsl_text = f"{ncsl_entry.get('category','')} {ncsl_entry.get('summary','')}".lower() if ncsl_entry else ""
    combined = f"{bp_text} {ncsl_text}"

    if not ballotpedia_entries and not ncsl_entry:
        return {
            "LegislationStatus": "No statewide policy",
            "BanType": "None",
            "DoormanCompatibility": "Silent",
            "Funding": "Unfunded",
        }

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
        compatibility = "Silent"
    else:
        # Soft, statewide "limit/restrict" language with no explicit method named -
        # genuinely ambiguous until district policy is written. Flag for follow-up
        # rather than guessing permitted vs restricted.
        compatibility = "Possible"

    funding = "Funded" if _contains_funding(combined) else "Unfunded"

    status = "Enacted" if (ballotpedia_entries or ncsl_entry) else "No statewide policy"
    if "encourag" in combined and "requir" not in combined and "shall" not in combined:
        status = "Encouraged (non-binding)"

    return {
        "LegislationStatus": status,
        "BanType": ban_type,
        "DoormanCompatibility": compatibility,
        "Funding": funding,
    }


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
    bp_data = fetch_ballotpedia()
    print(f"  {len(bp_data)} states with entries")
    print("Fetching NCSL...")
    ncsl_data = fetch_ncsl()
    print(f"  {len(ncsl_data)} states with entries")
    print("Fetching in-progress/failed bills (LegiScan)...")
    legiscan_data = fetch_legiscan_bills()
    print(f"  {len(legiscan_data)} states with pending/failed activity")

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
