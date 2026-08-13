# Doorman State Regulation Tracker

A self-updating 50-state map of K-12 cell phone legislation and Doorman
compatibility. No spreadsheet to maintain — a scraper re-reads Ballotpedia
and NCSL daily, classifies each state, and the page just displays whatever
`data.json` currently says.

## How it works

- `scraper.py` fetches the NCSL tracker table (primary source) and
  Ballotpedia (best-effort supplement — sits behind bot-detection some
  days) for **enacted** legislation, merges them by state, and classifies
  each state as **Permitted / Ambiguous / No Law / Restricted** using a
  sentence-level keyword scan (see the `CLASSIFY` section in the file —
  edit those keyword lists to refine the logic, not a spreadsheet). The
  scan is negation- and optionality-aware, so phrasing like "lockers are
  *not* permitted" or "students *may* use a locker as one option" doesn't
  get misread as a hard mandate.
- It also queries the [LegiScan API](https://legiscan.com/legiscan) (free,
  30k queries/month) per state for bills still moving through the
  legislature (**In Progress**), bills that **Failed** (vetoed or died),
  and bills LegiScan considers **Passed**. If a passed bill isn't yet
  reflected in NCSL or Ballotpedia for that state, it's used to
  automatically fill the gap (flagged on the site as auto-detected until
  the other sources catch up) rather than waiting on them. Each bill also
  gets a plain-English implication sentence and a short progress label
  (e.g. "Cleared committee").
- For states with enacted legislation, `scraper.py` separately pulls the
  actual full bill text via LegiScan (not just NCSL's short summary) and
  runs it through the same classifier — this is what catches
  implementation detail a summary alone would miss.
- Everything gets written to `data.json`, including a running changelog of
  what changed since the last run (kept for reference/debugging, not
  currently displayed on the site itself).
- Four static pages read `data.json`: `index.html` (the choropleth map +
  a live snapshot sidebar), `states.html` (the full sortable table),
  `target-states.html` (an auto-ranked expansion-priority list, see below),
  and `how-it-works.html` (a plain-language explanation of all of the
  above for a non-technical reader). Every state's full detail — including
  In Progress / Failed bill cards — opens in a modal on click; hovering a
  state on the map shows a quick-glance tooltip first.
- `scraper.py` also scores every non-Restricted, non-existing-customer
  state on five weighted factors — Compatibility (30%), Decision-Window
  Timing (25%), Market Concentration (20%), Legislative Direction (15%),
  and Market Size/TAM (10%) — and writes the ranked result to `data.json`
  as `targetStates`, which `target-states.html` ("State Expansion
  Rankings") displays. All five are fully automatic; see "State Expansion
  Rankings" below for how each is computed.
- `.github/workflows/update.yml` runs `scraper.py` once a day on GitHub's
  own servers (free), and commits `data.json` back to the repo if anything
  changed. You can also trigger it manually from the Actions tab.

Because the update runs on GitHub's infrastructure, this keeps working
indefinitely with zero maintenance — open it in three months and it'll
reflect whatever the source sites say at that point.

## One-time setup

See **DEPLOY.md** for the full click-by-click walkthrough (GitHub repo,
LegiScan key, Pages, permissions). Short version:

1. Get a free LegiScan API key.
2. Create a GitHub repo and push this folder.
3. Add the key as a repo secret named `LEGISCAN_API_KEY`.
4. Turn on "Read and write permissions" for Actions.
5. Turn on GitHub Pages.
6. Manually trigger the workflow once so `data.json` reflects a live
   scrape instead of the seeded baseline.

From then on, the page updates itself every day — nobody has to open a
spreadsheet, and nobody has to touch the code unless the classification
logic itself needs adjusting.

## Files

- `data.json` — the current dataset. Regenerated daily by the scraper;
  don't hand-edit it, changes will be overwritten on the next run.
- `scraper.py` — fetch + classify + diff + write. All the actual logic
  lives here — keyword lists, manual overrides, bill-text fetching, and
  the LegiScan gap-fill.
- `index.html` — the National Landscape map page.
- `states.html` — the full sortable state-by-state table.
- `target-states.html` — auto-ranked expansion-priority list, "State
  Expansion Rankings" in the nav (see below).
- `how-it-works.html` — plain-language explanation of the data sources,
  update cadence, and classification logic, for a non-technical reader.
- `shared.js` — logic shared by all four pages (data loading, the detail
  modal, hover tooltip, search autocomplete).
- `style.css` — shared styling for all four pages.
- `us-states-10m.json` — the US state boundary data the map is drawn from
  (a local copy, so the map doesn't depend on an external CDN staying up).
- `.github/workflows/update.yml` — the daily cron job.
- `OPERATIONS.md` — step-by-step reference for common tasks (replacing a
  file, running the scraper manually, adding a correction, etc.).

## State Expansion Rankings

`target-states.html` ("State Expansion Rankings" in the nav) turns the
compatibility classification into a directional "where to prioritize
next" list, outside Massachusetts and New Jersey (Doorman's existing
markets). All five factors below are fully automatic and recompute
nightly — an earlier version included a hand-maintained Competition
factor, but it required someone to periodically research things (like
which state grant programs fund pouches instead of software) that no
scrapable source states anywhere. It was removed rather than faked with a
shallow automated stand-in; see "Known limitations" below for what that
means in practice.

- **Compatibility (30%)** — automatic, derived from `DoormanCompatibility`/
  `BanType`. A state marked Restricted is excluded from the ranking
  entirely, same as the main tracker — no score can buy it back in.
- **Timing (25%)** — automatic but best-effort: a regex scan of *enacted*
  bill text for an effective-date signal ("2027-28 school year",
  "effective ... 2026"). Directionally useful, not a legal read.
- **Market Concentration (20%)** — automatic. Doorman sells school by
  school, not district by district, to individual high schools typically
  in the 200–2,000 student range. This factor compares each state's
  average high-school size (`NCES_HS_STATS`, NCES Common Core of Data)
  against that 200–2,000 band — a state full of large schools near the
  top of the band needs far fewer individual deals to cover the same
  enrollment than a state full of small, fragmented ones.
- **Legislative Direction (15%)** — automatic but best-effort. Reads each
  state's most-advanced *pending* bill (LegiScan's in_progress bucket)
  through the same physical-storage/software-friendly keyword scan
  `classify()` runs on enacted law (`_direction_score` in `scraper.py`),
  instead of just counting how many bills are moving. Raw bill-count
  "momentum" can't distinguish a state trending toward something Doorman
  can work with from one trending toward a hard pouch mandate — this can.
  Same caveat as Timing: pending-bill text is thinner than enacted
  statute and bills change during markup, so treat it as a more uncertain
  signal than the enacted-law Compatibility score.
- **Market Size / TAM (10%)** — automatic, a static NCES enrollment table
  in `NCES_ENROLLMENT`. Update it if a newer finalized NCES table comes
  out; state enrollment doesn't move enough year to year to need it more
  often than that.

## Known limitations

- NCSL and Ballotpedia are periodic snapshots, not real-time — a state can
  pass a law before either source lists it. The LegiScan gap-fill (see
  above) catches the case where a state has *no* NCSL/Ballotpedia entry at
  all; it won't catch a state that has an entry but for a different,
  now-outdated bill. `COMPATIBILITY_OVERRIDES` in `scraper.py` is the place
  to correct that by hand once you know about it.
- The Permitted/Ambiguous/No Law/Restricted classification is a keyword
  heuristic against bill/statute text, not a lawyer's read of the law.
  Treat it as a well-informed first pass, not a final compatibility
  determination, especially for states marked "Ambiguous."
- If NCSL or Ballotpedia changes their page/table structure, `scraper.py`
  will fail loudly (the GitHub Action will show a red X) rather than
  silently writing bad data — check the Actions tab occasionally.
- The State Expansion Rankings' Timing and Legislative Direction factors
  are both regex/keyword heuristics, not a read of actual implementation
  guidance or legal text — either can miss a real deadline, misread a
  pending bill's direction, or latch onto an unrelated year mentioned in
  the text. There is deliberately no automated stand-in for competitive
  intelligence (grant-funding fine print, incumbent contracts, new
  entrants) — that factor was removed rather than faked, since nothing in
  NCSL/Ballotpedia/LegiScan can tell you who a state's implementation
  grant actually favors. Treat the ranking as a starting point for
  prioritization, not a finished answer — the same caution that applies
  to compatibility classification applies here too, plus whatever isn't
  captured by any of these five factors at all (a competitor's new
  product launch, a district pilot already underway, a sales conversation
  in progress).


## For your employer (or Reach Capital)

**Viewing it:** once you've deployed it (Part 5 in DEPLOY.md), it's a
normal public webpage at a URL like
`https://your-username.github.io/doorman-state-tracker/`. Anyone can open
that in a browser — no GitHub account, no login, nothing to set up. Send
them the link.

**Editing data:** never necessary — it refreshes itself daily. Nobody,
including you, should need to manually change a state's status.

**Editing code/logic** (e.g. tweaking the compatibility keyword rules, the
one-line implication text, or the update time): this does require a
GitHub account and access to the repo, but not git or a terminal. On
GitHub.com, open the file (e.g. `scraper.py`), click the pencil "Edit"
icon in the top right, make the change in the browser, and click "Commit
changes" at the bottom — that's the entire workflow, and GitHub's Actions
job picks up the change on its next scheduled run automatically. To give
your employer that ability, add them as a collaborator under
**Settings → Collaborators** in the repo, or transfer the repo to their
GitHub account/organization outright if they'll own it long-term.
