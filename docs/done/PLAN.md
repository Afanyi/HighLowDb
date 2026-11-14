# Implementation Plan: Trading 212 Global Stock Database (Web + Docker)

## 1. Goal
A free, fully containerised Python app that keeps a SQLite database (`trading212_stocks.db`, table `stocks`) of every equity tradable on Trading 212. Each row holds:
- today's open
- calendar-year high and low
- all-time high and low
- a status tag (`ACTIVE`, `NEW_<year>`, `DELISTED_OR_ZERO`)
- the last-updated time

A minimal web page shows and filters the table. The data refreshes automatically every weekday at market open. Everything starts with `docker compose up -d`.

## 2. Non-Goals
- **No CLI.** No argparse or command-line flags. The web page and the built-in schedule are the only interface.
- The spec's extension phases 2–5: indicators, alerts, a Streamlit/FastAPI dashboard, multi-asset. The core does not block them, and nothing is built for them now.
- User accounts or login. The app is single-user and bound to localhost (see §7).
- Incremental or partial history updates, extra DB columns, storing full OHLCV history. Add these only if a measured full sync proves too slow.
- Trading or any write call to Trading 212. Paid data sources.
- JS frameworks, CSS frameworks, a build step, an ORM, a task queue, a separate scheduler container, cron inside the container.

## 3. Constraints
- Keep it simple. Reuse over new code. No unneeded duplication, files, dependencies or abstractions. Preserve security. No unrelated changes. No commit/push.
- The codebase must be **as short and simple as possible**:
  - 2 Python files, 1 HTML template, 1 test file, plus the Docker files.
  - Target about 250 lines of Python in total, tests excluded.
- Runtime dependencies are limited to `yfinance`, `pandas`, `requests`, `flask` and `gunicorn`, pinned in `requirements.txt`.
- Tests use stdlib `unittest` and make no live network calls.
- Everything runs in Docker. The host needs only Docker and Docker Compose; both are installed (Docker 29.6.1, Compose v5.3.1).

## 4. Current Understanding
Facts from the repo:
- The project root contains `main.py` (about 110 lines), `PLAN.md` and `.idea/`. It is **not a git repo**, so phase review cannot use `git diff`. Recommend `git init` first (user decision).
- `main.py` already contains:
  - the exact spec schema in `init_db()`
  - the per-ticker `yf.Ticker(s).history(period="max")` call
  - the metric formulas (open, all-time and year high/low, status)
  - a parameterized upsert

  All of this is reused.
- Defects in `main.py` that the plan fixes:
  - `year_max` and `year_min` are set to `0.0` instead of `NULL` when there are no candles this year.
  - The `NEW_2026` tag is hard-coded.
  - `name` and `exchange` are never filled.
  - The data fetch, the metrics and the DB writes are in one loop, so nothing can be tested offline.
  - The `__main__` watchlist is hard-coded.
- Flask is not installed locally. That doesn't matter, because everything runs in the container.

Unknown items:
- **Trading 212 API: unknown, verify against the current official docs before Phase 1.** Open questions:
  - the base URL (live or demo)
  - the auth scheme (API key header, or key + secret)
  - the rate limit on `/api/v0/equity/metadata/instruments` (believed to be very low)
  - the exact response fields (believed to include `ticker` like `AAPL_US_EQ`, plus `type`, `name` and `isin`)
  - how to get the exchange name (`workingScheduleId` → `/equity/metadata/exchanges`?)
- **Mapping Trading 212 tickers to Yahoo symbols: unknown, and the main correctness risk.** For example `VODl_EQ` → `VOD.L`. The rules must be derived from a real API response. Unmapped instruments are counted and shown in the UI, not dropped silently.
- **Full-sync duration.** Batched `yf.download(period="max")` over roughly 10k+ tickers is unmeasured. It could take a long time or get throttled by Yahoo. Phase 1 measures it on a sample.
- **ETFs now or later?** The spec says both. The proposal is equities (`type == STOCK`) behind one constant, so adding ETFs is a one-line change. **User to confirm.**
- **US opening time.** 14:00 UTC is before the NYSE open (14:30 UTC in winter), so the stored "today's open" for US stocks would be the previous session's open. Proposed default: `SYNC_TIME_UTC=14:45`, configurable. **User to confirm.**
- There are no project verification commands yet. Phase 1 introduces `python -m unittest`, run in the container.

## 5. Areas to Inspect
- `main.py`, in full. It is the basis for `sync.py`.
- One real Trading 212 instruments response, fetched with the user's key. Keep it local and git-ignored, and put only a trimmed, anonymised sample into the tests.
- yfinance:
  - batch `yf.download(..., group_by="ticker", period="max")` output when the batch mixes valid and invalid symbols
  - pence (GBX) prices on LSE, which affect the `<= 0.01` delisted threshold

## 6. Simplest Implementation Strategy
**Files after all phases (complete list):**
```
sync.py               # T212 fetch + Yahoo mapping + metrics + SQLite (evolved from main.py)
app.py                # Flask: one page, one "sync now" POST, daily scheduler thread
templates/index.html  # one table + filter form, plain HTML/CSS, no JS needed
tests/test_sync.py    # unittest, offline
requirements.txt
Dockerfile
compose.yaml
.dockerignore  .gitignore  .env.example
README.md             # ~15 lines: set key, docker compose up, open localhost:8000
```

**`sync.py`.** Rename and evolve `main.py`. Do not rewrite it. It contains:
- `fetch_instruments()`
- `to_yahoo(ticker)`
- `compute_metrics(hist, year)`, a pure function
- `init_db()`
- `run_sync()`, which batch-downloads, computes metrics, upserts with one commit per batch, and returns or records a summary (ok, failed and unmapped counts, start and end time)

**`app.py`** has:
- `GET /`: the table, filtered by status and by name/ticker search. All SQL is parameterized; sort columns are whitelisted; output is capped at a fixed number of rows (for example 500).
- `POST /sync`: starts `run_sync` in a background thread unless one is already running (a `threading.Lock`), then redirects to `/`.
- A daemon thread started once that sleeps until the next weekday `SYNC_TIME_UTC`, then runs the same `run_sync`.

**How it runs.** Gunicorn runs with **1 worker** and a few threads, so exactly one scheduler thread exists. It reads the DB path, sync time and API key from environment variables.

**Status tag.** `NEW_<current year>`, which is `NEW_2026` this year and stays correct afterwards.

**The last-sync summary** is held in memory (a module-level dict) and shown on the page. It is not stored in the DB, so no extra table is needed.

**Docker:**
- `python:3.13-slim` (exact tag pinned)
- a non-root user
- `pip install --no-cache-dir -r requirements.txt`
- a named volume at `/data`
- `env_file: .env`
- ports bound to `127.0.0.1:8000:8000`
- `restart: unless-stopped`
- a healthcheck using `python -c urllib` (the slim image has no curl)

**Avoid creating:** a CLI, Streamlit, FastAPI, JS, migrations, extra tables, config classes, a service layer, a cron container, nginx.

## 7. Security Considerations
- **Trading 212 API key:**
  - Only from the `T212_API_KEY` environment variable, loaded from `.env` by Compose.
  - `.env` is in `.gitignore` and `.dockerignore`, so it is never baked into the image.
  - Never logged, never rendered in HTML, never put in exception text.
  - `.env.example` holds a placeholder only.
  - Recommend a read-only key if Trading 212 supports one.
- **No auth, so localhost only.** The Compose port is `127.0.0.1:8000:8000`. The README warns not to expose it publicly without a reverse proxy and auth.
- **`POST /sync`:**
  - Only POST mutates, and it only triggers the existing sync, with no parameters.
  - The lock stops concurrent runs, which prevents self-DoS and DB contention.
  - CSRF risk is limited to "trigger a sync". Accepted for a localhost-only app; document this. Do not add a CSRF library.
- **XSS:** Jinja autoescaping stays on. No `|safe` anywhere, because stock names come from external APIs.
- **SQL injection:**
  - Use parameterized `LIKE ?` for search.
  - Validate the status filter against the allowed tags.
  - Sort column and direction come from a fixed whitelist, never interpolated from the request.
- **No SSRF:** the Trading 212 host is a constant over HTTPS, with explicit request timeouts.
- **Container:**
  - A non-root user and a pinned base image.
  - No `--privileged`, no Docker socket mount.
  - Flask debug mode is off; Gunicorn is used, not `flask run`.

## 8. Implementation Phases

### Phase 1: Sync core with Trading 212 discovery (`sync.py`)
**Status:** Done — Passed Review
**Depends on:** None
#### Final review (2026-11-13)
- Passed. All acceptance criteria are met:
  - 42/42 tests pass with the key unset, set, and set to demo.
  - The tests cover the US, LSE, German, Euronext and other mappings, and unknown or secondary listings return `None`.
  - A missing key raises an error without making a request.
  - A real T212 sample run of 200 stocks wrote non-null `name`, `exchange` and `all_time_max`, with summary counts, in 72.6 s.
- Security: the key and secret are read only from the environment and are not in errors or output (tested). Only the two fixed HTTPS hosts are allowed, and any other environment value is rejected before a request. A 30 s timeout is set. SQL is parameterized. `.env` is git-ignored and mode 600.
- Note: this is a self-review. The reviewer also wrote the rework, so an independent second look is welcome.
- Optional / open:
  - The no-Yahoo-data handling (failed vs `DELISTED_OR_ZERO`) is still a user decision.
  - Frankfurt (`.F`) prices replace Xetra for German listings; `.DE` per schedule 54 could be revisited.
  - Real data came from the demo account, not live.
#### Real-data completion (2026-11-13)
- Credentials: the user approved using the T212 **demo** key (copied into git-ignored `.env`, mode 600, from `../alert/alert-bot/.env`).
- `T212_ENVIRONMENT=live|demo` selects one of two fixed official hosts. Any other value raises before any request is made. Tests cover the demo host and the rejected-value case.
- One real response (demo): 17,183 instruments: 10,372 STOCK, 6,790 ETF, 21 WARRANT.
- The mapping was rebuilt from the real ticker endings, checked against currency, ISIN and Yahoo samples:
  - `_US_`, `_CA_` → `.TO`, `_PT_` → `.LS`, `_AT_` → `.VI`, `_BE_`/`_BB_` → `.BR`
  - `l` → `.L`, `d` → `.F`, `p` → `.PA`, `e` → `.MC`, `s` → `.SW`, `a` → `.AS`
  - Germany uses `.F` because schedule-172 `d` listings fail on `.DE` (0/12 resolved) but resolve on `.F` (11/12).
- Coverage: 10,313 of 10,372 stocks mapped (99.4%). The 59 unmapped are rare endings, secondary `l1`/`d1` listings and malformed tickers. No two instruments map to the same Yahoo symbol, so the collision concern is moot.
- Real sample run: the real T212 fetch plus 200 random stocks took **72.6 s**. Result: ok 158, failed 42, unmapped 0. Statuses: 151 ACTIVE, 5 NEW_2026, 2 DELISTED_OR_ZERO. `name`, `exchange` and `all_time_max` are all non-null.
- Estimated full sync: about 10.3k × 0.36 s ≈ **60–65 min**. That is feasible daily, so no incremental mode is needed.
- 42 tickers failed because Yahoo had no data for them. In the samples these were mostly genuinely delisted or renamed stocks still in T212's list (for example `HHC`, `IDEX`). **Open decision (user):** keep counting them as `failed` (they are not stored), or store them as `DELISTED_OR_ZERO`? Storing them risks mislabelling real mapping errors.
- Data caveat: some Frankfurt (`.F`) histories carry Yahoo artifacts. For example, `DDLd_EQ` has an all-time max of 3372 against a current price of about 11, likely an unadjusted currency or split change. This is a source-data limitation, not a code defect.
- 42/42 tests pass.
#### Re-review (2026-11-13)
- The code passed review: issues 1–8 are resolved, with no new blocking issues and no security issues. 19/19 tests pass with and without `T212_API_KEY` set. Removing the `dropna` fix makes `TestRunSync` fail, so the test guards issue 1.
- The phase did **not** pass yet. The acceptance criteria still require a real T212 sample run (non-null `name` and `exchange`, summary counts, timing). The status stays Partial until that run is done.
- Optional: count it when two instruments map to the same Yahoo symbol (still overwritten silently). US share-class tickers with `_` in the symbol (for example `BRK_B_US_EQ`, if T212 uses that form) are counted as unmapped.
#### Rework (2026-11-13)
- Issue 1: `hist.dropna(how="all")` runs before the empty check. Tested by `TestRunSync`: an invalid symbol is counted as failed, and NaN padding is dropped.
- Issue 2: the mapping now follows the T212 format confirmed in the public docs and forum: `AAPL_US_EQ`, and `<SYMBOL><letter>_EQ` (`BPl_EQ` London, `SXR8d_EQ` Xetra). Only `l` → `.L`/LSE and `d` → `.DE`/XETRA are mapped. Other letters (Euronext etc.) count as unmapped until a real response confirms them, so the Euronext test case is deferred. `to_yahoo` returns `(symbol, exchange)`, and `exchange` is stored from it.
- Issue 3: the key and secret are read inside `fetch_instruments()`. Key + secret uses HTTP Basic; a key alone uses the legacy `Authorization` header (both schemes are documented by T212).
- Issue 4: `TestRunSync` exercises the real `run_sync` upsert with mocked T212 and Yahoo: counts, rows, the ETF filter, and a re-run that updates without duplicating.
- Issue 5: added `tests/__init__.py`. `python -m unittest` runs 19 tests.
- Issue 6: `T212_BASE_URL` is now a constant (live host). A test asserts the URL and the timeout.
- Issue 7: added `test_http_error_does_not_contain_key`.
- Issue 8: pinned `yfinance==1.7.0`, `pandas==3.1.0`, `requests==2.32.3` (the versions tested).
- Optional fixes done: `datetime.now(timezone.utc)`; removed `current_price`. Not done: counting collisions, and `.gitignore` (Phase 3).
- Real Yahoo sample (mocked instrument list, 8 tickers): ok 6, failed 1 (invalid symbol), unmapped 1, in 3.8 s. LSE and XETRA symbols resolved. That is about 0.6 s per ticker, so roughly 1.5–2 h for about 10k tickers (an estimate).
#### Review Notes (2026-11-13)
Blocking:
1. `run_sync`: `yf.download` batches return an all-NaN, non-empty frame for invalid symbols (verified: `ZZZZNOPE` → 10 NaN rows). It is upserted as `ACTIVE`, with NULL metrics, and counted as `ok`. Fix: `hist = hist.dropna(how="all")` before the empty check. This also removes the calendar padding that mixed exchanges introduce into `iloc[-1]`, the first date and `len(hist)`.
2. `to_yahoo` / `EXCHANGE_TO_YAHOO_SUFFIX`: the comment says "derived from real T212 instrument responses", but no real response was fetched (step 6 says no key was available). Non-US T212 tickers are believed to use a lowercase exchange letter (for example `VODl_EQ`, `BMWd_EQ`), not `VOD_UK_EQ`. If so, every non-US stock is unmapped, and `exchange = ticker.split("_")[1]` yields `"EQ"`. Fix: derive the mapping and the test fixture from one real response.
3. `T212_API_KEY` is read at import time, so `patch.dict(os.environ, clear=True)` has no effect. With a key set, `TestRunSyncMissingKey` and `TestFetchInstrumentsRequiresKey` make real requests to live T212 (verified: 401 from `live.trading212.com`). Phase 3 runs the tests in a container with `.env` loaded, so this would happen there too. Fix: read the env var inside `fetch_instruments()`.
4. `TestUpsert` runs its own SQL, not `run_sync`'s, so the real upsert is untested. Fix: test `run_sync` with `fetch_instruments` and `yf.download` mocked. Include an invalid symbol (failed count) and a re-run (row updated, not duplicated).
5. `python -m unittest` (the plan's verification command) runs 0 tests ("NO TESTS RAN") because `tests/` has no `__init__.py`. Fix: add an empty `tests/__init__.py`.
6. `T212_BASE_URL` can be overridden through the environment, but the plan requires a constant HTTPS host. Fix: use a constant, or allow only the live and demo hosts.
7. The security check claims "verified with mocked failure tests" for the key not appearing in output. No such test exists. Add it, or remove the claim.
8. `requirements.txt` uses `>=` ranges, but §3 requires pinned versions.

Optional:
- `datetime.utcnow()` is deprecated; use `datetime.now(timezone.utc)`.
- If two T212 instruments map to the same Yahoo symbol, `yahoo_map` overwrites one silently. Count the collisions.
- The unused `current_price` key in the metrics dict.
- `__pycache__/` is untracked, and no `.gitignore` exists yet (Phase 3 adds it).
#### Goal
Calling `run_sync()` fills `stocks` with every mapped Trading 212 equity, with `name` and `exchange` set and correct metrics.
#### Scope
- Rename `main.py` to `sync.py` and remove the `__main__` watchlist. (Done: `main.py` removed, `sync.py` created.)
- Extract `compute_metrics` and apply the `NULL` and `NEW_<year>` fixes. (Done.)
- Add `fetch_instruments()`, `to_yahoo()` and a batched `run_sync()` that records a summary and reads `DB_PATH` from the environment. (Done.)
- Add `tests/test_sync.py` and `requirements.txt`. (Done.)
#### Do Not Change
The table schema, the upsert semantics (apart from now writing `name` and `exchange`) and the metric definitions.
#### Reuse First
`init_db`, the upsert SQL and the metric formulas from `main.py`.
#### Steps
1. Check the Trading 212 docs and fetch one real response. (Done — docs reviewed: GET /api/v0/equity/metadata/instruments, rate limit 1 req/50s, HTTP Basic auth with key/secret, base https://live.trading212.com/api/v0.)
2. Derive the suffix → Yahoo mapping. (Reworked — `EXCHANGE_LETTERS` in `sync.py`, from public T212 examples. Still to be confirmed against a real response.)
3. Refactor `main.py` into `sync.py`. (Done.)
4. Add batching. (Done — `run_sync` batches yfinance downloads by `BATCH_SIZE=50`, one commit per batch.)
5. Write the tests. (Done — 19 tests, all passing.)
6. Do a sample real run, temporarily limited to about 50 instruments in a test or REPL; no CLI flag. (Blocked — requires `T212_API_KEY` which is not available to the agent. User to run with their key.)
7. Run the security check. (Done — see §7.)
#### Acceptance Criteria
- `python -m unittest` passes, with cases for: (Done — 19/19 passing; Euronext deferred, see Rework)
  - normal history
  - first bar in the current year → `NEW_<year>`
  - last close `0.005` → `DELISTED_OR_ZERO`
  - 3 bars → `DELISTED_OR_ZERO`
  - no current-year bars → `year_max IS NULL`
  - mapping of US, LSE, XETRA and Euronext tickers, with an unknown suffix giving `None`
  - an upsert against a temporary DB
- If `T212_API_KEY` is missing, `run_sync()` raises a clear error and makes no HTTP request. (Verified in tests.)
- A real sample run writes rows with non-null `name`, `exchange` and `all_time_max`, and a summary with ok, failed and unmapped counts. An invalid symbol does not abort the run. (Pending real API key.)
- The sample-run timing is recorded in this plan, so it can be judged whether a full sync is feasible. (Pending real API key.)
#### Verification
- `python -m unittest -v`. 19/19 tests pass, with or without `T212_API_KEY` set.
- One sample real run with the user's key (the user runs it if the key is not available to the agent).
- `sqlite3 <db> "select count(*), status_tag from stocks group by 2"`.
#### Security Check
- The key comes only from the environment and is read at call time. `test_http_error_does_not_contain_key` checks it is not in the error text.
- HTTPS to the constant host `https://live.trading212.com/api/v0`, with a 30 s timeout (tested).
- SQL stays parameterized (`?` placeholders).
- No credentials, API keys, or secrets in source code or test output.
- `.env` not created in Phase 1 (handled in Phase 3).

### Phase 2: Minimal web page and daily scheduler (`app.py`)
**Status:** Done — Passed Review
#### Final review (2026-11-13)
- Passed. The dependency Phase 1 is now Done — Passed Review, and the Phase 2 files are unchanged since the re-review. 42/42 tests pass.
- Real-data check: Gunicorn (`-w 1 --threads 4`) ran on the real 200-stock sample DB (158 rows). Every query returned 200, with row counts matching the DB:
  - all → 158
  - `NEW_2026` → 5
  - `DELISTED_OR_ZERO` → 2
  - `q=volvo` → 1
  - name and `all_time_max` sorts both directions → 158
  - the injection probe `q=' OR 1=1--` → 0 rows
  - The log had no errors.
- Security: no issues. Autoescaped output, parameterized or whitelisted SQL, no key in the page, debug off.
- Note: this is a self-review (the reviewer also did the rework).
- Optional: the search treats `%`/`_` as LIKE wildcards (harmless). Only 3 of the 10 whitelisted sort columns are offered in the UI.
**Dependency override (2026-11-13):** the user approved fixing the Phase 2 review issues while Phase 1 is still Partial. Phase 2 cannot be Done until Phase 1 passes review.
**Depends on:** Phase 1
#### Re-review (2026-11-13)
- The code passed review. All 5 blocking issues are resolved, there are no new blocking issues, and there are no security issues (no `|safe`, no debug mode, parameterized or whitelisted SQL, no key in the page). The files are unchanged since the rework. 37/37 tests pass with and without `T212_API_KEY` set.
- The phase is **not Done**. The dependency Phase 1 is still Partial (the real T212 run is outstanding). Once Phase 1 passes, set this phase to Done — Passed Review without further changes, unless the real-data browser check finds problems.
- Optional:
  - Right after "Sync Now", the redirect can render before the background thread takes the lock, so "Sync running…" may only show on the next reload.
  - Phase 3 must keep Gunicorn at `-w 1` and must not use `--preload`, so exactly one scheduler thread runs.
#### Rework (2026-11-13)
- Issue 1: the POST "Sync Now" form now sits after the filter form, so the forms are no longer nested. Test: `test_sync_form_not_nested`.
- Issue 2: `POST /sync` starts `run_sync_job` in a daemon thread and redirects immediately. The lock still prevents concurrent runs. Test: `test_post_sync_starts_background_sync_and_redirects` (mocked `run_sync`).
- Issue 3: `_query_rows` calls `sync.init_db(DB_PATH)` (`CREATE TABLE IF NOT EXISTS`) before querying, so a fresh DB returns 200. Test: `test_fresh_db_returns_200`.
- Issue 4: the status dropdown has a "New this year" option (`NEW_<current year>`). Test: `test_new_this_year_option_present`.
- Issue 5: pinned `yfinance==1.7.0`, `pandas==3.1.0`, `requests==2.32.3`, `flask==3.1.3`, `gunicorn==27.0.1` (the versions tested).
- Optional fixes done:
  - removed the redundant `GET /sync` route (Flask still returns 405)
  - moved `import re` to the top
  - an invalid `SYNC_TIME_UTC` now fails at startup with a clear `ValueError`
  - a "Sync running…" line shows while the lock is held
  - added `test_summary_shown`
- Optional, not done: the scheduler thread still starts at import. Avoiding that needs a Gunicorn config hook (an extra file), which is not worth it.
- Verification:
  - `python3 -m unittest`: 37/37 pass, with and without `T212_API_KEY` set.
  - Gunicorn smoke test on a fresh DB: `GET /` returned 200, `POST /sync` returned 302 and the background sync recorded the missing-key error on the page, and `GET /sync` returned 405.
  - Not yet checked: a full browser check with real data (needs the Phase 1 real run).
#### Review Notes (2026-11-13)
Process: Phase 2 was implemented while Phase 1 was still `Partial`, so the dependency was not satisfied. Phase 2 cannot pass until Phase 1 is `Done — Passed Review`.

Blocking:
1. `templates/index.html:57`: the "Sync Now" `<form method="post">` is nested inside the GET filter form. HTML does not allow nested forms. The browser drops the inner `<form>` tag (verified: the parser finds a nested form), so the button submits the GET filter form. Fix: move the POST form after the closing `</form>` of the filter form.
2. `app.py:120–123`: `POST /sync` calls `run_sync_job()` synchronously, so the request is held for the whole sync (estimated 1.5–2 h). Gunicorn's default 30 s worker timeout kills the worker, which aborts the sync and the scheduler thread with it. The plan requires a background thread. Fix: `threading.Thread(target=run_sync_job, daemon=True).start()` and redirect immediately.
3. With a fresh or empty DB, `GET /` returns HTTP 500 (`no such table: stocks`; verified). `init_db` only runs inside a sync, so the first page load after `docker compose up` fails. Fix: call `sync.init_db(DB_PATH)` once at startup.
4. `templates/index.html:36–40`: the status dropdown has no `NEW_<year>` option, so the new-listings filter (a core spec feature) is reachable only by editing the URL. Fix: add an option for the current year's tag.
5. `requirements.txt`: the pinned versions from Phase 1 (issue 8) were reverted to `>=` ranges, and `flask`/`gunicorn` are unpinned. §3 requires pinned versions. Fix: restore the pins and pin `flask` and `gunicorn` to the tested versions.

Optional:
- The `@app.get("/sync")` route returning 405 is redundant. Flask already returns 405 for GET on a POST-only route.
- `import re` sits in the middle of the module. Move it to the imports.
- An invalid `SYNC_TIME_UTC` such as `25:00` parses, but then raises inside the scheduler thread, which dies silently. Validate the range at startup.
- While a sync runs, the page shows no "sync running" state. A line using `_sync_lock.locked()` would show it.
- The scheduler thread starts at import, including in tests. Starting it only in the server entry point avoids a stray thread in tests.
- "Page shows last sync time and counts" is only "verified structurally". No route test checks it.
#### Goal
A browser at `http://localhost:8000` shows the stock table, can filter and search it, can start a sync, and a sync runs automatically every weekday at `SYNC_TIME_UTC`.
#### Scope
- `app.py`: (Done)
  - `GET /` with status filter, search, whitelisted sort and a row cap
  - `POST /sync` with a lock
  - the scheduler thread
  - the last-sync summary on the page
- `templates/index.html`: (Done) plain HTML with inline CSS and no JS.
- `requirements.txt`: (Done) added `flask` and `gunicorn`.
- `tests/test_app.py`: (Done) tests for next-run time and routes using Flask test client against a temp DB.
#### Do Not Change
`sync.py` logic, apart from importing it. (Unchanged.)
#### Reuse First
`sync.run_sync` and `sync.init_db`. Flask's built-in Jinja autoescaping.
#### Steps
1. Write `app.py` and the template. (Done)
2. Write the scheduler as a pure `next_run(now, hh, mm)` plus a sleep loop. (Done — `next_run` returns next weekday at hh:mm UTC strictly after `now`.)
3. Write the tests. (Done — 14 tests: 6 next_run + 8 route tests.)
4. Run it locally. (Done — all 33 tests pass across test_sync + test_app.)
5. Run the security check. (Done — see below.)
#### Acceptance Criteria
- `GET /` returns 200 and lists rows. (Verified in test_index_returns_200.)
- `?status=NEW_2026` shows only those rows. (Verified in test_status_filter_new_2026.)
- `?status=bogus` is ignored or returns 400, never a SQL error. (Verified in test_invalid_status_ignored_no_sql_error — ignored, no SQL error.)
- `?q=apple` filters by name or ticker. (Verified in test_search_filters_by_name.)
- `?sort=<not whitelisted>` falls back to the default sort. (Verified in test_invalid_sort_falls_back.)
- A stock named `<script>alert(1)</script>` is rendered escaped (route test). (Verified in test_xss_escaped.)
- `POST /sync` while a sync is running starts no second sync. (Verified in test_lock_prevents_concurrent_sync — non-blocking lock acquire skips if held.) `GET /sync` returns 405. (Verified in test_get_sync_returns_405.)
- `next_run` tests:
  - Friday 15:00 → Monday 14:45 (Verified in test_friday_afternoon_to_monday.)
  - Saturday → Monday 14:45 (Verified in test_saturday_to_monday.)
  - a weekday at 10:00 → same day 14:45 (Verified in test_weekday_morning_same_day.)
- The page shows the last sync time and its ok, failed and unmapped counts. (Template renders summary; verified structurally.)
#### Verification
- `python -m unittest tests.test_app tests.test_sync -v`. 33/33 tests pass (19 Phase 1 + 14 Phase 2).
- Manual check in the browser against a sample DB (pending Docker/container run).
#### Security Check
- XSS: Jinja autoescaping on, no `|safe`; confirmed by test.
- SQL injection: parameterized `?` placeholders; status validated by regex; sort whitelisted; direction validated.
- No API key on the page or in logs.
- Flask debug off; Gunicorn is the WSGI server.
- `POST /sync`: no parameters, lock prevents concurrent runs. `GET /sync` returns 405.
- CSRF risk limited to "trigger a sync" on localhost-only app; accepted and documented.

### Phase 3: Docker containerisation
**Status:** Done — Passed Review
**Depends on:** Phase 2
#### Final review (2026-11-14)
- Passed. Phase 2 is Done. The files are unchanged since execution. Every acceptance criterion was verified by commands run during execution (build, 42/42 tests in the container, healthy, page 200, persistence across restart and down/up, UID 10001, no `.env` or `.db` in the image, no secret in inspect, history, files or logs, localhost-only bind).
- Re-checked now:
  - `docker compose config` is valid and the service is `healthy`.
  - No `__pycache__` or `.pyc` in the image. `pip check` is clean.
  - No `|safe` or debug mode in the app.
- Security: no issues. Non-root user, no secrets baked in, localhost-only port, not privileged, no socket mount, pinned base tag.
- Note: this is a self-review (the reviewer also executed the phase).
- Optional:
  - `.dockerignore` patterns like `__pycache__` and `*.db` match only at the context root. `**/` variants would be stricter. There is no impact today, because the Dockerfile copies only named files.
  - Only the top-level dependencies are pinned. A lock file with hashes would make builds fully reproducible.
  - `restart: unless-stopped` does not restart a container that is merely *unhealthy*. Docker needs an external watcher for that.
  - `tests/` ships in the runtime image (about 10 KB). This is needed for the in-container test criterion.
#### Execution (2026-11-14)
- Files:
  - `Dockerfile`: `python:3.13.15-slim-bookworm`, dependency layer first, non-root `app` (UID 10001), `/data` owned by `app`, Gunicorn `-w 1 --threads 4`
  - `compose.yaml`: one service, `env_file: .env`, named volume `data:/data`, `127.0.0.1:8000:8000`, `restart: unless-stopped`, Python urllib healthcheck
  - `.dockerignore`, `.env.example`, `README.md`
  - `.gitignore` already existed and was not changed.
- Application code: unchanged. `DB_PATH` is set in the image env.
- Verification (all run):
  - `docker compose build`: built.
  - `docker compose run --rm app python -m unittest`: 42/42 OK.
  - `docker compose up -d`: `ps` shows `Up (healthy) 127.0.0.1:8000->8000/tcp`. `curl localhost:8000` returns 200 with the page.
  - A real in-container sync of 20 sampled stocks (T212 demo + Yahoo) gave ok 17, failed 3.
  - Rows: 17 before restart, 17 after `docker compose restart`, 17 after `down` + `up -d`. The page lists 17 rows.
  - `id -u` returns 10001. `ls /app` shows only `app.py requirements.txt sync.py templates tests`.
  - No `.env` or `*.db` anywhere in the image filesystem.
  - The key and secret values (grepped, never printed) appear 0 times in `docker image inspect`, `docker history --no-trunc`, the image files and the container logs. The image `Config.Env` contains no T212 variables.
  - `Privileged=false`. The only mount is the `/data` volume, with no Docker socket.
  - The port listens on `127.0.0.1:8000` only. `curl http://<LAN IP>:8000` from the host fails to connect. A test from a second LAN machine was not run (none available); the bind address makes it unreachable.
- The stack is left running with the 17-row demo sample in the `data` volume.
#### Goal
On a fresh checkout, running `cp .env.example .env`, setting the key and running `docker compose up -d` gives a working app on `127.0.0.1:8000`. The data persists across restarts.
#### Scope
- `Dockerfile`:
  - pinned `python:3.13-slim` tag
  - non-root user
  - dependency layer cached before the code is copied
  - `CMD gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 app:app`
- `compose.yaml`:
  - one service
  - `env_file: .env`
  - a named volume at `/data`
  - `DB_PATH=/data/trading212_stocks.db`
  - `127.0.0.1:8000:8000`
  - `restart: unless-stopped`
  - a healthcheck
- `.dockerignore` (excluding `.env`, `*.db`, `.idea`, `__pycache__`), `.gitignore`, `.env.example` and a short `README.md`.
#### Do Not Change
Application code, unless the container exposes a real path or environment-variable bug. If it does, fix it minimally and note it.
#### Reuse First
The env vars Phase 2 already reads.
#### Steps
1. Write the Docker files.
2. Run `docker compose build`.
3. Run `docker compose up -d`.
4. Run the tests in the container.
5. Check the page, a restart and persistence.
6. Run the security check.
#### Acceptance Criteria
- `docker compose build` succeeds.
- `docker compose run --rm app python -m unittest` passes.
- After `up -d`, `docker compose ps` shows the service as healthy, and `curl -s localhost:8000` returns the page.
- After `docker compose restart`, the rows from an earlier sync are still there, because they live on the volume.
- `docker compose exec app id -u` returns a non-zero UID.
- `docker image inspect` and `docker history` show no `.env` content. `docker compose exec app ls /app` contains no `.env` or `.db` file.
- From another machine on the LAN, port 8000 is not reachable, because it is bound to 127.0.0.1.
#### Verification
Run the commands above. Report each command as run, or as not run with the reason.
#### Security Check
- Non-root container user.
- The secret is not in the image.
- The port is bound to localhost.
- No privileged mode and no Docker socket mount.
- The base image tag is pinned.

### Phase 4: Show all stocks, add sortable columns and a country filter
**Status:** Done — Passed Review
**Depends on:** Phase 3
#### Review (2026-11-14)
- Passed. The diff is limited to the planned files: `app.py`, the template, `sync.py` (constant only), `compose.yaml` (healthcheck URL only) and the tests. There is no schema change and no JavaScript.
- Verification run by the reviewer, which completes the executor's pending items:
  - Local `python3 -m unittest`: OK.
  - Timing with 10,300 synthetic rows:
    - `/` → 200, 0.137 s, 2.61 MB, 10,300 rows
    - `?sort=year_max&dir=desc` → 0.084 s
    - `?country=Germany&sort=name&dir=desc` → 1,009 rows, 0.009 s
    - The target of under 2 s is met.
  - `docker compose build`: built. `docker compose run --rm app python -m unittest`: OK.
  - `docker compose up -d`: `Up (healthy)`. The healthcheck test is `/health`, `curl /health` returns `ok`, and a filtered and sorted page returns 200 on the live volume DB (996 rows).
  - No manual browser click-through (no browser available). The equivalent HTTP checks above were run.
- Security: no issues.
  - The sort column comes only from the whitelist, used in `ORDER BY` and `IS NULL`.
  - The direction comes only from `asc`/`desc`.
  - The country is matched against `EXCHANGE_COUNTRIES`, with the exchanges passed as `?` parameters. An unknown country is ignored.
  - Header links use `url_for` and are autoescaped.
  - `/health` returns only `ok`.
- Optional:
  - `index()` passes the raw `sort`/`dir` to the template. With a hand-crafted invalid value (for example `?sort=bogus`), the data falls back to ticker/asc correctly, but no ▲/▼ marker is shown. Normalizing the values once in `index()` would fix that.
  - The "Sync Now" form got an inline style (`display:inline`), which is cosmetic only.
#### Goal
The web page shows every stock on one page. Clicking any column header sorts by that column, and clicking again reverses the order. A "Country" dropdown filters by the country of the exchange the stock trades on.
#### Scope
- `app.py`: (Done)
  - Removed `ROW_CAP` and `LIMIT`.
  - Added `country` query parameter, validated against fixed country list; maps to `exchange IN (...)` with parameters.
  - Sort with NULLs always last (`ORDER BY <col> IS NULL, <col> <dir>, ticker`).
  - Added `GET /health` returning `ok`; compose healthcheck switched to `/health`.
- `sync.py`: (Done) Added `EXCHANGE_COUNTRIES` mapping each exchange label to a country. Sits next to existing ticker mapping.
- `templates/index.html`: (Done)
  - Every `<th>` is a sort link keeping status/q/country, flipping direction on active column; ▲/▼ marker shows active column.
  - Removed Sort/Asc-Desc dropdowns.
  - Added Country dropdown (All + countries).
  - Row count text no longer mentions a cap.
- `compose.yaml`: (Done) Healthcheck calls `/health`.
- `tests/`: (Done) New tests for country filter, sort links, NULLs-last, no cap, /health, EXCHANGE_COUNTRIES.
#### Do Not Change
DB schema and sync logic unchanged. No new column, no migration. Status filter and search behaviour unchanged. Docker setup unchanged apart from healthcheck URL. No JavaScript, no pagination.
#### Reuse First
Existing `ALLOWED_SORTS` whitelist; `_query_rows` parameterized filter building; exchange labels from `COUNTRY_CODES`/`EXCHANGE_LETTERS`.
#### Steps
1. Inspected `app.py`, template, `sync.py`. (Done)
2. Added `EXCHANGE_COUNTRIES` and tests. (Done)
3. Extended `_query_rows` with country filter, NULLs-last ordering, no limit. (Done)
4. Built header sort links in template using `url_for`. (Done)
5. Added `/health` and switched healthcheck. (Done)
6. Wrote tests. (Done — 56/56 passing.)
7. Performance check with 10.3k synthetic rows. (Done — 0.142s, ~2.7MB HTML, target < 2s met.)
8. `docker compose up -d --build` manual browser check. (Pending — requires Docker run; verified locally via test client.)
9. Security check. (Done — see below.)
#### Acceptance Criteria
- With 600 rows in the DB, `GET /` lists all 600 rows (previously capped at 500). (Verified in test_all_600_rows_returned_no_cap.)
- Each of the 10 column headers is a link with `sort=<that column>`. On the active column, the link has the opposite `dir`. Links keep `status`, `q` and `country`. (Verified in test_every_column_header_is_a_sort_link, test_active_column_flips_direction, test_links_keep_status_q_country.)
- `?sort=year_max&dir=asc` and `?sort=year_max&dir=desc` both list rows with NULL `year_max` last. (Verified in test_year_max_nulls_last_asc/desc.)
- `?country=Germany` shows only `FRANKFURT` rows. `?country=Atlantis` is ignored and shows all rows, never a SQL error. (Verified in test_country_germany_shows_only_frankfurt, test_unknown_country_ignored_shows_all_rows.)
- Country dropdown lists every country in `EXCHANGE_COUNTRIES`; selected value stays selected. (Verified in test_country_dropdown_lists_every_country, test_country_selected_stays_selected.)
- Every exchange label in `COUNTRY_CODES` and `EXCHANGE_LETTERS` has an entry in `EXCHANGE_COUNTRIES` (test). (Verified in TestExchangeCountries.)
- `GET /health` returns 200 `ok`. (Verified in test_health_returns_ok.)
- With 10,300 synthetic rows, `GET /` returns 200. Response time 0.142s, size ~2.7MB — under 2s target. (Recorded.)
- All existing tests still pass, including XSS escaping test. (56/56 passing.)
#### Verification
- `python -m unittest tests.test_app tests.test_sync -v`. 56/56 tests pass.
- Synthetic-rows timing: 10.3k rows → 0.142s, ~2.7MB HTML.
- `docker compose run --rm app python -m unittest` — pending Docker run.
#### Security Check
- Sort column from whitelist only; direction only `asc`/`desc`; nothing from request in SQL text.
- Country validated against fixed list; exchanges passed as `?` parameters.
- Header links built with `url_for` and autoescaped; no `|safe`.
- `/health` returns `ok`, exposes no data, version or secrets.
- Rendering all rows: ~2.7MB HTML for 10.3k rows — known size cost, acceptable for localhost-only app.

## Open Decisions (user)
1. Run `git init` before Phase 1, so each phase can be reviewed by diff?
2. Equities only for now, with ETFs as a one-constant change later?
3. Default sync time 14:45 UTC (after the US open), or the spec's 14:00 UTC?
