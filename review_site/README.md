# Book of Mormon Comic Review

A small website where invited volunteers each review one chapter of the finished comic at a time, flag problems by
clicking on the art, and request another chapter. You (the editor) triage the reports, track coverage, and send the
accepted ones back to this PC for fixing.

```
 your PC (comic pipeline)                      the website
 ┌─────────────────────┐   publish           ┌──────────────────────────┐
 │ runs/book/…/pages   │ ───────────────────▶│ chapters, versions, pages│
 │ review/flags.json   │◀─────────────────── │ reviewers → issues       │
 │ review-queue.json   │   pull (accepted)   │ you → accept / reject    │
 └─────────────────────┘                     └──────────────────────────┘
```

The site never reads `runs/` directly. Two small tools on your PC move things across: **publish** (pages up) and
**pull** (accepted issues down).

## 1. Try it on your PC (about 5 minutes)

From the repo root, in the project's virtual environment:

```sh
pip install -e ".[web]"                    # one time (or: pip install -r review_site/requirements-dev.txt)
python -m review_site.tools.devseed        # demo accounts + 6 real chapters, opened for review
uvicorn review_site.app.main:create_app --factory --reload
```

Open <http://127.0.0.1:8000>. Log in as `ann@example.com` (reviewer), `editor@example.com` or
`admin@example.com`, password `demo-password-123`. Everything lives in `review_site/data/` (delete the folder to start
over). Run the tests with `pytest review_site/tests` (or just `pytest`).

## 2. Putting it online (Fly.io, a few dollars a month)

Fly charges for what runs: a 512 MB machine is about $3.69/month if it ran 24/7, and ours sleeps when nobody is using
it, so expect roughly $1–3. The 3 GB data volume is $0.15/GB/month (about $0.45). There's no free tier, but there's no
minimum either. (Prices from Fly's pricing page, October 2026.)

One-time setup, in **PowerShell**:

1. Install the Fly tool: `iwr https://fly.io/install.ps1 -useb | iex`, then **open a new terminal**.
2. Create your account and log in: `fly auth signup` (a browser opens). New accounts get a short trial; add a card
   when Fly asks.
3. Edit `review_site\fly.toml` and change `app = "bom-comic-review"` to a name nobody else has, e.g.
   `bom-comic-review-yourname`. If you change the region (`iad` = Virginia), use the same one below.
4. From the repo root:

```powershell
cd review_site
fly launch --no-deploy --copy-config      # say NO to Postgres/Redis/etc.; keep the name from fly.toml
fly volumes create review_data --size 3 --region iad
.\..\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"   # copy the output
fly secrets set REVIEW_SECRET_KEY="<paste it>"
.\..\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"   # a second, different one
fly secrets set REVIEW_TOOL_TOKEN="<paste it>"
fly deploy --ha=false                     # one machine: SQLite can't be shared between machines
```

**Keep the second value (the tool token) somewhere safe**: your PC needs it to publish and pull, and Fly won't show
it again. Then create your admin account on the server:

```powershell
fly ssh console
# inside the server's prompt:
python -m review_site.manage create-user --email you@example.com --name "Your Name" --role admin
exit
```

It asks for a password. Visit `https://<your-app>.fly.dev`, log in, and you're set. A custom domain is optional
(`fly certs add yourdomain.com`).

On your PC, so you don't type them every time (then open a new terminal):

```powershell
setx REVIEW_SITE_URL "https://<your-app>.fly.dev"
setx REVIEW_TOOL_TOKEN "<the tool token>"
```

To update the site after changing the code: `cd review_site; fly deploy --ha=false`. Your data lives on the volume
and survives deploys. Fly also snapshots the volume daily (kept 5 days), but see "Back up the database" below for your
own copy.

## 3. Day to day

| I want to… | Do this |
|---|---|
| Put chapters on the site | `python -m review_site.tools.publish` (first run converts ~1,100 pages and uploads about 600 MB; later runs only send what changed) |
| Let reviewers start | Site → **Admin → Chapters → Open all for review** (new chapters arrive *closed*) |
| Invite someone | **Admin → Users & invites → Create invite**, send them the link. They pick their own password |
| Help someone who forgot their password | **Users & invites → Reset link**, send it to them privately |
| See who's covered what | **Admin → Coverage** (every chapter coloured by completed reviews) |
| Triage reports | **Admin → Issues**: open one, set **Accepted / Rejected / Duplicate**; tick several to change in bulk |
| Send accepted issues to my PC | `python -m review_site.tools.pull` (add `--dry-run` to look first) |
| Release a redrawn chapter | Re-assemble it as usual, run **publish** again, then **Admin → Chapters → (chapter) → Make it live** |
| Remove a careless review from the counts | **Admin → Review history → Exclude** |
| Download everything | **Admin → Issues → Download CSV** |
| Back up the database | `curl -H "Authorization: Bearer $REVIEW_TOOL_TOKEN" $REVIEW_SITE_URL/api/tool/backup -o backup.db` |

### What `pull` does with accepted issues

* **Art problems on a panel** (character looks wrong, art error, continuity, layout) become a flag in that chapter's
  `review/flags.json`, the same as a flag made in the draft reader, so your redraw step picks them up.
* **Story problems on a panel** (accuracy, missing/invented content, dialogue, spelling) are added to
  `runs/book/review-queue.json` for the story job. Use `--no-queue` to skip this.
* **Whole-page reports and "Other"** are only listed in `runs/book/site-issues-report.md` for you to handle.
* If the page picture changed on this PC after the reviewer saw it, the issue is listed as *stale* and left alone.

Pulled issues are marked as sent on the site, so running `pull` twice never duplicates anything.

## 4. How it behaves

**Assignment.** A reviewer's "Get a chapter" picks, among open chapters they haven't reviewed on this version, those
with the fewest *completed* reviews (the first pass covers every chapter once, then second reviews, and so on),
choosing randomly among ties and preferring chapters nobody else is currently holding. Chapters they've reviewed in an
older version, or just skipped, are used only when nothing else is left. The same person never gets the same version
twice.

**Holds.** A claimed chapter is held for 48 hours and renews whenever the reviewer saves something. After that it
returns to the pool. A reviewer holds one chapter at a time and can skip three a day.

**Completing a review.** Every page must be ticked *This page looks good* or have at least one issue. Zero issues is
fine. Submitted reviews are locked.

**Privacy.** Reviewers only ever see their own issues, never other people's or the pipeline's automated approvals.
Editors don't see an issue until its review is submitted. Page images require login.

**Versions.** A chapter's version is a fingerprint of its page images and verse text. Publishing a changed chapter
creates a *pending* version, you choose **Full re-review** or **Changed pages only**, then make it live. Only the live
version's reviews count toward coverage; older reviews and issues are kept as history.

**Roles.** *Reviewer*: reviews. *Editor*: also triages issues, manages chapters and reviews. *Admin*: also manages users
and invites.

## 5. Settings (environment variables)

| Variable | Meaning |
|---|---|
| `REVIEW_DATA_DIR` | Where the database and images live (default `review_site/data`; `/data` online) |
| `REVIEW_DATABASE_URL` | Override the database (default: SQLite file in the data folder) |
| `REVIEW_SECRET_KEY` | Signs login cookies. Required (32+ characters) when `REVIEW_SECURE_COOKIES=1` |
| `REVIEW_TOOL_TOKEN` | Password for the publish/pull tools. Unset = tool API disabled |
| `REVIEW_SECURE_COOKIES` | `1` online (HTTPS only cookies) |
| `REVIEW_HOLD_HOURS` | Hold length (default 48) |

## 6. Code map

```
app/main.py            create_app() and middleware        app/models.py     all tables
app/routers/           auth · reviewer · admin · tool_api   app/services/     the rules (testable without the web)
  services/assignment  which chapter next                   services/versions publishing + going live
  services/reviews     marks, issues, submit                services/issues   triage, CSV
app/templates, static  pages; static/review.js is the review screen
tools/                 publish · pull · devseed (the only code that imports the comic pipeline)
migrations/            Alembic (python -m review_site.manage upgrade)
tests/                 pytest
```

Change the tables → `alembic -c review_site/alembic.ini revision --autogenerate -m "what changed"` and commit the new
file in `migrations/versions/`.

## 7. Not built yet (easy to add later)

Google sign-in, email notifications, leaderboards, drawn boxes on art, phone-specific layout, reviewer chapter
preferences, side-by-side comparison of versions, and a public read-only progress page.
