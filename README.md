# course-kit

Two double-clicks. **`Course Kit.command`** opens a dashboard in your browser where you fetch
course material, draft discussion posts, replies, study guides, reading analyses and wargame
turns, and then adjust each draft with one-click chips until it reads like you. Everything
stays on this computer. Nothing is ever posted to Moodle for you: you copy, you paste.

## Dashboard quick start

1. Double-click **`Course Kit.command`** (first time: right-click, Open). Leave its window open.
   Your browser opens http://127.0.0.1:8765.
2. **Settings**: pick a provider, paste an API key (the page links to where to get one), paste
   the participation rubric and two or three of your own past posts. Save, then Test connection.
3. **Fetch**: type a section number (or leave blank for the whole course) and click Fetch.
   Chrome opens, you log in, the log scrolls, the page refreshes with the course map.
4. Click a module. Pick **Discussion post**, **Reply to a classmate**, **Quiz study guide**,
   **Reading analysis** or **Wargame turn**. Twenty to sixty seconds later the draft opens.
5. Edit in the box, or hit a chip (Shorter, More formal, Add a citation, Sound more like me...),
   or type an instruction and Apply. Every version is kept; Restore any of them.
6. **Copy to clipboard**, paste into Moodle. **Approve and save** files it under the section's
   `outbox/` folder.

Weekly rhythm: open the dashboard, click **Refresh Announcements** (the instructor's baseline
spreadsheet comes down as an attachment there), fetch the current section, draft, adjust, paste.

## The fetcher on its own

This grabs everything in one Moodle course (pages, readings, forum threads with
classmate posts, quiz intros, assignment files, the wargame spreadsheet) into a
clean folder tree of markdown and files. It runs in **your own Chrome**, so CAC
or username login works exactly like it does by hand. No password is ever typed
into or stored by the script. It never submits anything.

```
course/
  manifest.json                     what was fetched, where it lives, any errors
  raw-course.html                   the course page (send this back if parsing goes wrong)
  section-07-week-7-logistics/
    md/     701-week-7-overview.md   703-week-7-discussion.md  ...   <- feed these to the AI
    files/  sustainment.pdf  wargame_turn7.xlsx
    raw/    the original HTML of each page, for debugging
```

## Mac quick start (no Python needed)

1. Put this folder somewhere sensible, like `~/Documents/course-kit`.
2. Double-click **`Fetch Course Tree.command`**.
   - macOS may say it "cannot be opened because it is from an unidentified developer."
     Right-click the file, choose **Open**, then **Open** again. Only needed once per file.
   - First run installs `uv` (a small Python manager) and the browser library. A minute or two.
3. A Chrome window opens on the course. **Log in by hand** there (CAC prompt,
   consent banner, whatever it asks). The script waits up to 15 minutes.
4. The Terminal window prints the week-by-week list of items and stops. Nothing
   is downloaded yet. If it prints zero sections, send `course/raw-course.html` to Kaden.

The login is remembered in `.browser-profile/` for next time. Treat that folder
like a password: don't share it, don't back it up anywhere public.

`config.yaml` already points at the course. If the course changes, paste any
link from inside the new course into `course_url`; the script works out the rest.

## Weekly run

Double-click **`Fetch Week.command`**, type the section number from the tree
(for example `7`), press Enter. Items already saved are skipped, so rerun
freely after new posts land. Forums are always refreshed, because classmates
keep posting. Leave the number blank to fetch the whole course.

Terminal equivalent:

```bash
uv run fetch_moodle.py --tree           # list only
uv run fetch_moodle.py --section 7      # one week
uv run fetch_moodle.py --force          # refetch everything
```

## Where things live

```
config.yaml       course link, model/provider/key, default post length
profile/          about.md, voice.md, rubric.md (edit in Settings)
prompts/          the recipe cards; plain markdown with {{slots}}, edit freely
course/           everything fetched, one folder per section; outbox/ holds approved drafts
drafts/           every draft and all its versions
```

## What it will not do

- It does not attempt quizzes. It saves the quiz description only.
- It does not follow external URL items. It records the link.
- It does not submit anything. Read-only.
- It paces itself at about one page every 1.5 seconds, like a person clicking.
  Leave `pace_seconds` alone.

## When it breaks

Moodle markup varies by version and theme. Anything the parser misses is still
saved as raw HTML under `raw/`, so nothing is lost. Send Kaden the `raw/` file
for the item that came out empty and the parser gets a one-line fix. There is a
fake Moodle in `tests/` the whole thing is tested against before it ships.

```bash
uv run --with pytest --with playwright==1.63.0 --with beautifulsoup4 --with markdownify --with pyyaml python -m pytest -q tests
```

## CAC notes

The script launches the system Google Chrome (`channel: chrome` in config.yaml)
because Chrome reads smart-card certificates from the macOS Keychain. If Chrome
is not installed, it falls back to Playwright's own Chromium, which on a Mac
also uses the Keychain, so CAC usually still works. If the browser asks which
certificate to use, pick the **authentication** cert, not the signature or email
one. If the site logs you out between runs, just log in again in the window that
opens.
