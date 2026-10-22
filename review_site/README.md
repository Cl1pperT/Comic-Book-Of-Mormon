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

## 2. Putting it online (Fly.io, roughly $3–6 a month)

One-time setup:

1. Create an account at <https://fly.io> and add a card. Install the `flyctl` command-line tool
   (<https://fly.io/docs/flyctl/install/>) and run `fly auth login`.
2. Edit `review_site/fly.toml` and change `app = "bom-comic-review"` to a name nobody else has.
3. From the `review_site` folder:

```sh
cd review_site
fly launch --no-deploy --copy-config        # accept the name in fly.toml; say NO to a database
fly volumes create review_data --size 3 --region iad      # same region as fly.toml
fly secrets set REVIEW_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
fly secrets set REVIEW_TOOL_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
fly deploy
fly scale count 1                                          # SQLite needs exactly one machine
```

Write down the `REVIEW_TOOL_TOKEN` you generated (`fly secrets` won't show it again): your PC needs it. Then create
your admin account on the server:

```sh
fly ssh console -C "python -m review_site.manage create-user --email you@example.com --name 'Your Name' --role admin"
```

(It asks for a password.) Visit `https://<your-app>.fly.dev`, log in, and you're set. A custom domain is optional
(`fly certs add yourdomain.com`).

On your PC, so you don't type them every time (PowerShell, then restart the terminal):

```powershell
setx REVIEW_SITE_URL "https://<your-app>.fly.dev"
setx REVIEW_TOOL_TOKEN "<the token>"
```

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
