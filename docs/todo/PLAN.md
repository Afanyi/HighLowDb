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
**Status:** Partial — review issues 1–8 fixed, 19/19 tests pass, real Yahoo run verified with a mocked instrument list; Remaining: one real T212 run with the user's key (confirm the instrument count and the remaining exchange letters), then re-review
**Depends on:** None
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
**Status:** Not Started
**Depends on:** Phase 1
#### Goal
A browser at `http://localhost:8000` shows the stock table, can filter and search it, can start a sync, and a sync runs automatically every weekday at `SYNC_TIME_UTC`.
#### Scope
- `app.py`:
  - `GET /` with status filter, search, whitelisted sort and a row cap
  - `POST /sync` with a lock
  - the scheduler thread
  - the last-sync summary on the page
- `templates/index.html`: plain HTML with a little inline CSS and no JS.
- Add `flask` and `gunicorn` to `requirements.txt`.
- Add tests for the next-run time and for the routes, using the Flask test client against a temporary DB.
#### Do Not Change
`sync.py` logic, apart from importing it.
#### Reuse First
`sync.run_sync` and `sync.init_db`. Flask's built-in Jinja autoescaping.
#### Steps
1. Write `app.py` and the template.
2. Write the scheduler as a pure `next_run(now, hh, mm)` plus a sleep loop.
3. Write the tests.
4. Run it locally.
5. Run the security check.
#### Acceptance Criteria
- `GET /` returns 200 and lists rows.
- `?status=NEW_2026` shows only those rows.
- `?status=bogus` is ignored or returns 400, never a SQL error.
- `?q=apple` filters by name or ticker.
- `?sort=<not whitelisted>` falls back to the default sort.
- A stock named `<script>alert(1)</script>` is rendered escaped (route test).
- `POST /sync` while a sync is running starts no second sync. `GET /sync` returns 405.
- `next_run` tests:
  - Friday 15:00 → Monday 14:45
  - Saturday → Monday 14:45
  - a weekday at 10:00 → same day 14:45
- The page shows the last sync time and its ok, failed and unmapped counts.
#### Verification
`python -m unittest -v`, and a manual check in the browser against the Phase 1 sample DB.
#### Security Check
XSS (no `|safe`), SQL whitelist and parameters, no key on the page or in logs, Flask debug off.

### Phase 3: Docker containerisation
**Status:** Not Started
**Depends on:** Phase 2
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

## Open Decisions (user)
1. Run `git init` before Phase 1, so each phase can be reviewed by diff?
2. Equities only for now, with ETFs as a one-constant change later?
3. Default sync time 14:45 UTC (after the US open), or the spec's 14:00 UTC?
