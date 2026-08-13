# Operating the Doorman State Tracker — a plain reference

Everything you'll ever need to do to this project, step by step. Bookmark
this page. None of it requires installing anything or using a terminal —
it's all done in the browser on github.com.

## 1. Replacing a file that already exists (e.g. a new scraper.py)

Use this when I (or you) hand you an updated version of a file that's
already in the repo, like `scraper.py`, `index.html`, or `data.json`.

1. On your repo's main page, click **Add file** (top right of the file
   list) → **Upload files**.
2. Drag the new file into the upload area.
3. In the upload preview, click into the filename box and make sure it
   matches the existing file's name **exactly** (e.g. `scraper.py`, not
   `scraper-fixed.py`). This is what tells GitHub to replace the old one
   instead of adding a second file.
4. Scroll down, click **Commit changes**.
5. GitHub will show you a diff of what changed on the commit page — worth
   a glance to confirm it's not empty.

## 2. Adding a file that's brand new

Use this for a file that's never existed in the repo before (like when
`style.css` or `states.html` were introduced).

1. **Add file → Upload files**, drag it in, **Commit changes**. No
   renaming needed since there's nothing to conflict with.
2. Exception: files whose name starts with a dot (like anything under
   `.github/`) won't show up to drag from Finder on a Mac unless you first
   reveal hidden files (Cmd+Shift+. in Finder). If that's ever an issue
   again, the simpller path is: **Add file → Create new file**, then type
   the full path (e.g. `.github/workflows/update.yml`) into the filename
   box — GitHub creates the folders for you as you type the slashes.

## 3. Making a small edit directly on GitHub (no download needed)

For a one- or two-line change — like adding a state to the
`COMPATIBILITY_OVERRIDES` dict in `scraper.py`, or tweaking a color in
`style.css`.

1. Click the file in your repo's file list.
2. Click the pencil (Edit) icon, top right of the file view.
3. Make your change directly in the text box.
4. Scroll down, click **Commit changes**.

## 4. Running the scraper manually (don't wait for the daily schedule)

1. Click the **Actions** tab (top nav, not inside Settings).
2. Click **Update Doorman state tracker data** in the left sidebar.
3. Click **Run workflow** (dropdown, right side) → **Run workflow** again
   to confirm.
4. Wait ~30-60 seconds, click the refresh icon. A yellow dot means it's
   running; green check means it succeeded; red X means it failed.
5. Click into any run to see its log. If it fails, expand **Run scraper**
   and copy the red error text back to me — that's exactly how we found
   and fixed the last several issues.

## 5. Viewing the current data

Two ways:

- **The live site** — your `github.io` URL. Always shows whatever's
  currently in the repo's `data.json`. Hard refresh (Cmd+Shift+R) if it
  looks stale.
- **The raw file** — click `data.json` in your repo's file list to see it
  formatted, or click **Raw** on that page for the plain text. This is
  useful for spot-checking a specific state's data without clicking
  through the UI.

## 6. Downloading a file from GitHub to your computer

1. Click the file in the repo.
2. Click the **...** (or the download icon, depending on file type) →
   **Download raw file**. For `data.json` and code files, the **Raw**
   button followed by Cmd+S (Save Page As) in your browser also works.

## 7. Checking whether today's automatic run happened

1. **Actions** tab → **Update Doorman state tracker data**.
2. The list shows every run, newest first, with its trigger ("schedule"
   for the automatic daily one, "workflow_dispatch" for manual runs you
   triggered) and a green check or red X.
3. If you don't see a run for today yet, it hasn't fired at 11:00 UTC yet
   (check the current UTC time) — or check Settings → Actions → General
   to make sure Actions aren't paused for the repo.

## 8. When something looks wrong on the site

Rough decision tree:

- **Whole page blank/broken** → hard refresh first (Cmd+Shift+R). Still
  broken → open the browser console (F12 or right-click → Inspect →
  Console tab) and send me any red text.
- **Data looks stale** → check Actions tab for a recent green run. No
  recent runs → check Settings → Actions → General → Workflow permissions
  is set to "Read and write."
- **A specific state looks wrong** → this is a judgment call the
  automated sources can't always get right (see `scraper.py`'s
  `COMPATIBILITY_OVERRIDES` and `KNOWN_BILLS` dicts) — tell me what you
  found and I'll help you add or fix an override, same as we did for New
  York, Hawaii, and South Dakota.
- **A scheduled run failed (red X)** → click in, expand "Run scraper",
  send me the error text.
