# Deploy the Doorman State Tracker — complete beginner walkthrough

You've never used GitHub before, so this assumes zero prior knowledge.
Every step is something you click. Total time: about 15 minutes.

A quick glossary so nothing is confusing later:
- **GitHub** = a free website that stores your project's files (like Google
  Drive, but built for code) and can run scheduled jobs on them.
- **Repository** ("repo") = one project's folder on GitHub.
- **Commit** = GitHub's word for "save this change."

---

## Step 0 — Unzip the file and reveal hidden files (Mac)

1. Find `doorman-tracker.zip` in your Downloads (or wherever it landed)
   and double-click it. It'll unzip into a `doorman-tracker` folder next
   to it.
2. Inside that folder is a folder named `.github` — it starts with a dot,
   which means macOS hides it by default. You need to see it before the
   upload step later, so: open the `doorman-tracker` folder in Finder,
   then press **Cmd + Shift + .** (period). Hidden files/folders will
   appear slightly greyed out. Press the same shortcut again anytime to
   hide them again — it doesn't change anything, just visibility.
3. Confirm you can now see: `scraper.py`, `index.html`, `states.html`,
   `target-states.html`, `how-it-works.html`, `shared.js`, `style.css`,
   `data.json`, `us-states-10m.json`, `README.md`, `OPERATIONS.md`,
   `requirements.txt`, `DEPLOY.md`, and `.github` (with a `workflows`
   folder inside it containing `update.yml`). All 14 items need to make
   it to GitHub in Step 3 — the `.github` one is the easy one to
   accidentally miss.

## Step 1 — Get a free LegiScan API key

This powers the "In Progress" and "Failed" bill sections. You can skip
this and add it later — everything else works without it.

1. Go to https://legiscan.com/user/register and sign up (email +
   password, free, no credit card).
2. Once logged in, go to https://legiscan.com/legiscan and find the
   button to generate/view your API key.
3. Copy the key and paste it somewhere temporary (a Notes app) — you'll
   need it again in Step 6.

## Step 2 — Create your GitHub account

1. Go to https://github.com/signup
2. Enter an email, create a password, pick a username. Verify your email
   when it asks.
3. You now have a free GitHub account — no payment needed for anything
   in this guide.

## Step 3 — Create the repository and upload your files

1. Once logged in, click the **+** icon in the top-right corner of any
   GitHub page, then click **New repository**.
2. Under "Repository name," type `doorman-state-tracker`.
3. Leave it set to **Public** (this is required for the free hosting in
   Step 8 to work — being public just means anyone with the link can view
   the site and the code; it doesn't mean it's advertised or searchable
   unless someone already has the link or searches GitHub directly).
4. Leave everything else as-is — do **not** check "Add a README file."
5. Click the green **Create repository** button.
6. On the next page, look for a line of text that says something like
   "...or you can upload an existing file" and click **uploading an
   existing file** (it's a blue link, usually in the middle of the page).
7. Now open Finder, go into your unzipped `doorman-tracker` folder, and
   select everything inside it — click one item, then Cmd+A to select all
   (this should include `.github` since you revealed it in Step 0).
8. Drag all the selected items into the browser window, onto the area
   that says "Drag files here to add them to your repository."
9. Wait for the upload to finish — you'll see a file list appear showing
   everything you dragged, including `.github/workflows/update.yml`
   nested inside its folders. If `.github` didn't show up, go back to
   Step 0 and make sure hidden files are visible, then drag just that
   folder in separately.
10. Scroll down, and where it says "Commit changes," you can leave the
    default message or type "Initial upload." Click the green **Commit
    changes** button.

You now have a working copy of the whole project on GitHub.

## Step 4 — Add your LegiScan key as a secret

"Secret" here just means a password GitHub stores privately and hands to
your script when it runs, without showing it publicly on the page.

1. In your repository, click **Settings** (a tab near the top, with a
   gear icon — you may need to click a `...` or expand the tab bar on
   smaller screens).
2. In the left sidebar, click **Secrets and variables**, then click
   **Actions** underneath it.
3. Click the green **New repository secret** button.
4. For "Name," type exactly: `LEGISCAN_API_KEY`
5. For "Secret," paste the key you copied in Step 1.
6. Click **Add secret**.

(Skipped Step 1? You can come back and do this later — the site works
fine without it, just without the In Progress/Failed sections.)

## Step 5 — Let the daily job save its updates

1. Still in **Settings**, click **Actions** in the left sidebar, then
   **General** underneath it.
2. Scroll down to the section called **Workflow permissions**.
3. Select **Read and write permissions** (it's probably set to
   "Read repository contents permission" by default — change it).
4. Click **Save**.

This lets the daily scraper actually write its results back into your
repository. Without this step, it would run but silently fail to save
anything.

## Step 6 — Turn on the website (GitHub Pages)

1. Still in **Settings**, click **Pages** in the left sidebar.
2. Under "Build and deployment," find the **Source** dropdown and make
   sure it says **Deploy from a branch**.
3. Below that, set the branch dropdown to **main** and the folder
   dropdown to **/ (root)**. Click **Save**.
4. GitHub will show a message that it's building your site. Wait about
   a minute, then refresh the page — it'll show a link like
   `https://your-username.github.io/doorman-state-tracker/`. That's your
   live website. Click it to see the tracker (it'll show the data you
   uploaded, not yet freshly scraped — that's the next step).

## Step 7 — Run the scraper once right now

By default it only runs once a day automatically. Trigger it manually
once so you don't have to wait until tomorrow to see it work.

1. Click the **Actions** tab at the top of your repository (not inside
   Settings — the main tab bar).
2. In the left sidebar, click **Update Doorman state tracker data**.
3. On the right, click the **Run workflow** dropdown button, then click
   the green **Run workflow** button that appears.
4. Wait about 30–60 seconds, then click the refresh icon. You should see
   a new run appear with a yellow dot (running) that turns into a green
   checkmark (success) or a red X (something failed — click into it, the
   log will explain what).
5. Once it's green, revisit your website link from Step 6. It should now
   reflect a fresh scrape — check the badge in the top-right corner of
   the page, it shows the "Data current as of" timestamp.

## You're done

From here, it updates itself every day around 11:00 UTC with zero further
action from you or anyone else. Send the `github.io` link from Step 6 to
your employer — that's the only thing they need; no GitHub account
required to view it.

## Troubleshooting

- **Red X on the Actions run**: click into it, expand "Run scraper" to
  read the error. Most likely causes: Ballotpedia or NCSL changed their
  page layout (rare), or the LegiScan key is missing/mistyped (check
  Step 4's secret name is exactly `LEGISCAN_API_KEY`, no extra spaces).
- **Website shows old/blank data**: make sure Step 5 (Read and write
  permissions) is actually set — this is the most common miss.
- **`.github` folder never made it into the repo**: go to your repo's
  main page and check if you see a `.github` entry in the file list. If
  not, click **Add file → Upload files** again, make sure hidden files
  are visible in Finder (Step 0), and drag just the `.github` folder in,
  then commit.
- **Map looks empty/broken**: open the site, press F12 (or right-click →
  Inspect) to open developer tools, click the "Console" tab, and see if
  there's a red error message — that'll usually point at `data.json`
  failing to load, which almost always traces back to one of the two
  issues above.

## Later — giving your employer edit access

They never need this to *view* the site. If they eventually want to
change something in the code themselves, go to **Settings →
Collaborators** in your repo and add their GitHub username or email —
they'll get an invite email and, once accepted, can edit files directly
on GitHub.com (click a file, click the pencil icon, edit, commit) without
installing anything.
