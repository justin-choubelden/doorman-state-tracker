# Doorman State Regulation Tracker

A self-updating 50-state map of K-12 cell phone legislation and Doorman
compatibility. No spreadsheet to maintain — a scraper re-reads Ballotpedia
and NCSL daily, classifies each state, and the page just displays whatever
`data.json` currently says.

## How it works

- `scraper.py` fetches the Ballotpedia and NCSL tracker tables for
  **enacted** legislation, merges them by state, classifies each state as
  **Permitted / Possible / Silent / Restricted** using a keyword heuristic
  (see the `CLASSIFY` section at the top of the file — edit those keyword
  lists to refine the logic, not a spreadsheet).
- It also queries the [LegiScan API](https://legiscan.com/legiscan) (free,
  30k queries/month) per state for bills still moving through the
  legislature (**In Progress**: introduced/engrossed/enrolled) and bills
  that **Failed** (vetoed or died). Each bill gets one plain-English
  implication sentence — e.g. "Would require physical device storage if
  passed — incompatible with a software-only approach" — generated from
  the same keyword rules, deliberately kept shallow since these haven't
  become law yet.
- Everything gets written to `data.json`, including a running changelog of
  what changed since the last run.
- `index.html` is a static page that reads `data.json` and renders the map,
  state detail panel (with In Progress / Failed bill cards), sortable
  table, and a "Recent Changes" feed built from the changelog.
- `.github/workflows/update.yml` runs `scraper.py` once a day on GitHub's
  own servers (free), and commits `data.json` back to the repo if anything
  changed. You can also trigger it manually from the Actions tab.

Because the update runs on GitHub's infrastructure, this keeps working
indefinitely with zero maintenance — open it in three months and it'll
reflect whatever the two source sites say at that point.

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

- `data.json` — the current dataset. Seeded from Doorman's existing
  tracker; the scraper's first live run will reconcile it against
  Ballotpedia/NCSL and may reclassify a handful of ambiguous states — worth
  a quick human spot-check after that first run given this feeds an
  investment decision.
- `scraper.py` — fetch + classify + diff + write.
- `index.html` — the site.
- `.github/workflows/update.yml` — the daily cron job.

## Known limitations

- Ballotpedia and NCSL cover *enacted* legislation well; bills that are
  still moving through a legislature (not yet passed or failed) are not
  reliably captured by either source, so "in progress" status will lag
  reality for active sessions. If that matters, the cleanest addition is a
  third scheduled step that runs a web search for `"[state] cell phone
  school bill 2026"` per state and appends anything new to a `pending`
  section — happy to build that next if useful.
- The Permitted/Possible/Silent/Restricted classification is a keyword
  heuristic against bill summary text, not a lawyer's read of statute
  language. Treat it as a first pass, not a final compatibility
  determination, especially for the states in "Possible."
- If Ballotpedia or NCSL changes their page/table structure, `scraper.py`
  will fail loudly (the GitHub Action will show a red X) rather than
  silently writing bad data — check the Actions tab occasionally.


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
