# BACKLOG.md

Outstanding work observed while surveying the repository. The codebase contains
no `TODO` or `FIXME` markers, so every item below was derived from reading the
code, the docs, and CI -- each one cites what it is based on.

Current state: 696 Python tests, all of which pass on CI with none skipped; a
Windows checkout runs 686 of them, since ten need symlinks or media tools. 22
frontend tests, 61 browser tests covering every page and maintenance tab with an
axe pass on each. A UI conformance suite (tests/test_ui_conventions.py) pins the
shared component contract in DESIGN.md, so pagers, page sizes, selection
wording, and master select-alls cannot drift apart again. `ruff check` is clean
(including a C901 complexity ceiling and an enforced 120-column limit),
`ruff format --check` is clean, coverage is 83.51% against an 80% floor, and CI
is green on `main`.

There are no open questions. Everything below is mine to do.

## Interface

The reported problem is that the app is "messy and hard to navigate". One
concrete cause is fixed -- the scan folder is now shared across the maintenance
tabs -- and the rest of what a survey turned up is recorded here rather than
guessed at later.

### 1. Tabs do not create history entries

`history.replaceState` is used when switching maintenance tabs, so Back leaves
the page rather than returning to the previous tab. Deep links work, which is the
important half. Whether Back should walk the tabs is a genuine judgement call --
seven tabs deep means seven Back presses to leave -- so this is recorded rather
than changed.

### 2. The active nav link is marked by client-side script

`base.html` marks the current page after load rather than the server rendering
it, so the navigation is briefly unmarked. Milliseconds on a LAN, and it now
carries `aria-current`, but the server already knows which page it rendered.

## Emby tagline titles

Built and on the Emby Operations tab (`app/emby_taglines.py`): scan, review,
apply, undo, all over the Emby API.

### 1. First live run against the real server

The apply path is verified against a simulated Emby shaped like the real
responses, not against a live server. The first real run should be: scan,
review, apply to two or three items, check them in Emby, then do the rest. If
Emby's item GET turns out to omit fields the POST needs, the incomplete-item
refusal will catch it and nothing will be written.

## Emby library index

The index itself is built (`app/emby_library_index.py`): it sweeps Emby for
genres, tags, studios, people and this user's watch state, stores the result
under `/state/emby-index`, and filters locally so several tags can be required
at once. What remains is everything around it.

### 1. No way to build or use the index from the interface

There is no route, no Settings control to pick the Emby account, and no search
screen -- the module is reachable only from Python. It needs: a user picker on
Settings (`list_users` already returns the accounts), a "Rebuild index" action,
and a filter surface that uses `facets()` to offer values rather than asking the
operator to remember exact spellings.

### 2. Confirm how Emby handles multiple tags server-side

The index filters locally, which sidesteps the question -- but if Emby can
require several tags itself, a large library could be filtered without holding
every row in memory. Worth measuring once there is a real library indexed, not
before.

### 3. Preference signals from watch history

The rows now carry played, play count and favourite alongside every facet, which
is the dataset a "what does this operator like" summary needs. Deliberately not
built yet: the aggregates should be looked at against a real library before any
recommendation behaviour is designed on top of them.

## Lower priority

### 1. Several modules have outgrown one file

`app/video_preview_maintenance.py` (4,183 lines), `app/maintenance.py` (4,016),
`app/poster_maintenance.py` (2,092), `app/routes.py` (1,724), and
`app/subtitle_maintenance.py` (1,604) each hold scanning, planning, applying,
persistence, and payload shaping together. Nothing is broken, but the seams are
already visible in the module names (`duplicate_slots.py` and
`duplicate_review_store.py` were split out of `maintenance.py`).

Split opportunistically when touching one of these for another reason, not as a
standalone refactor.

`apply_duplicate_cleanup_plan` (441 lines, complexity 43) was surveyed as a
candidate, since AGENTS.md flags it and `build_duplicate_cleanup_plan` as worth
untangling. Its guards turned out to be four group-level checks that between
them shadow most of the per-file gate behind them, so a naive extraction would
reorder protections whose interaction is the whole point. Both layers now have
tests (`tests/test_duplicate_apply_guards.py`), which is the prerequisite for
touching it safely later.

## Deliberately not on this list

- **Authentication, CSRF, and rate limiting.** Absent by design, not by
  oversight: `SECURITY.md` and `README.md` both state vid2gif is for a trusted
  private LAN and list what would need adding before any internet-facing use.
  That is a product decision, not outstanding work.
- **`/healthz` returning 503 outside Docker.** Correct behaviour -- it fails
  when ffmpeg, ffprobe, or the worker threads are absent, which is the normal
  state of a dev machine.
- **The per-issue history in audit logs.** The logs record what each run did,
  not which finding it closed, so a rebuilt dashboard cannot restore historical
  discovered/resolved counts. Recording issue ids in every workstream's log
  would fix that for future runs, but it changes the log format across five
  domains to guard against losing a `/state` file that already has a backup and
  recovery path. Not worth the churn unless that store proves fragile.
