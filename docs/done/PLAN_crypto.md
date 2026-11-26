# Implementation Plan: Crypto Mode

Follows `docs/done/PLAN_dashboard.md`. Adds a second dataset (crypto) next to the stocks, with a switch on the dashboard.

## 1. Goal
- The dashboard gets a **Stocks | Crypto** switch. Crypto mode shows every coin CoinGecko tracks with market data, from both centralised and decentralised exchanges (about 17,500–20,000 coins as of 2026-11-14).
- Each coin has the same figures as a stock: current price, year high/low, all-time high/low, status and the 4 distance columns. The existing filters, multi-column sorting and grouping work the same way.
- A crypto sync runs once a day, every day (crypto trades 24/7), and can be started by hand. It respects CoinGecko's rate limits and never stops, blocks or slows the stock sync.

## 2. Non-Goals
- No Binance or other second crypto source. CoinGecko covers centralised and decentralised exchanges in one list.
- No tokens CoinGecko does not list (brand-new DEX tokens, GeckoTerminal pools).
- No intraday or live prices, no charts, no coin logos (no external images), no market-cap or volume filter.
- No change to the stock sync, the stock table, the stock DB file or the stock schedule (except Phase 5, added later at the user's request).
- No JavaScript and no new Python dependency (`requests` is already in `requirements.txt`).
- No commercial use: the CoinGecko Demo plan is for personal use and requires attribution.

## 3. Constraints
- Keep it simple. Reuse existing code over new code. No unneeded duplication, files, dependencies or abstractions. Keep security intact. No unrelated changes. No commit or push.
- **The running stock sync must not be interrupted.** A container rebuild happens only while no stock sync is running (Phase 3).
- **Free-plan budget.** The CoinGecko Demo plan allows 10,000 calls a month (from the signup page, 2026-11-14). The crypto sync uses at most about 275 calls a run, about 8,250 a month at one run a day.
- **Rate limit.** The exact Demo per-minute limit is unclear (the docs once said 30, the signup page shows 100). The sync paces itself at no more than 24 calls a minute and backs off on HTTP 429.
- Data rendered from CoinGecko is untrusted and stays autoescaped (no `|safe`).
- Tests run inside the app image (there is no local venv):
  `docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD":/src:ro -w /src --entrypoint python allstocksdatabase-app -m unittest discover -s tests -t .`

## 4. Current Understanding
- `sync.py`: the stock sync. It gets the instrument list from Trading 212, then one Yahoo request per ticker (`_Yahoo.history`) with a 0.5 s delay, exponential back-off on rate limits and retry passes. It writes to the `stocks` table in `DB_PATH` (`/data/trading212_stocks.db` in Docker) and commits every 100 rows. A full run takes about 4 hours.
- `app.py`:
  - **Scheduler:** one daemon thread runs `run_sync_job` on weekdays at `SYNC_TIME_UTC`. A non-blocking `_sync_lock` prevents two syncs at once, and the result is kept in `last_summary`. `POST /sync` starts a sync in a background thread.
  - **Page:** `_query_rows` builds one SQL query. Sort columns come from a whitelist (`ALLOWED_SORTS`). The 4 distance columns are SQL expressions (`DISTANCES`), and the min/max bounds are bound parameters. `index()` renders `templates/index.html`.
- `templates/index.html`: Bootstrap 5 CSS only, no `<script>`. The navbar holds the sync badge and **Sync Now**. The filter row under the headers holds search, country, status, Apply/Reset and the min/max boxes. Prices are formatted with `"%.2f"`.
- `Dockerfile`: copies `sync.py`, `app.py`, `templates/` and `tests/` explicitly, so a new `.py` file must be added there. It runs gunicorn with one worker, which means exactly one scheduler thread.
- `compose.yaml`: `env_file: .env`, a named volume `data` mounted at `/data`, port bound to 127.0.0.1.
- `.env` already has `COINGECKO_API_KEY` (tested, valid), `CRYPTO_VS_CURRENCY=usd` and `CRYPTO_SYNC_TIME_UTC=02:00`. `.env.example` documents them, but its comment wrongly mentions Binance.
- **CoinGecko API** (tested 2026-11-14 with the key):
  - `GET /coins/markets?vs_currency=usd&per_page=250&page=N` returns 250 coins per page with `id`, `symbol`, `name`, `market_cap_rank`, `current_price`, `high_24h`, `low_24h`, `total_volume`, `ath`, `atl` and `last_updated`. Pages run out somewhere between 70 and 80.
  - `GET /coins/{id}/market_chart?vs_currency=usd&days=365` returns daily prices for the last year (366 points for bitcoin).
  - The key goes in the `x-cg-demo-api-key` header.
- Unknown — inspect before implementation:
  - whether `/coins/markets` sends `Retry-After` on 429
  - whether it returns coins with a `null` price
  - the exact last page number

## 5. Areas to Inspect
- `sync.py` (`_Yahoo`, `run_sync`, `init_db`, `fetch_instruments`): patterns to copy for pacing, back-off, the missing-key check and the upsert.
- `app.py` (`run_sync_job`, `_scheduler_loop`, `next_run`, `_query_rows`, `index`, `sync_now`).
- `templates/index.html`: the navbar, the filter row, the `fmt` macro and the row markup.
- `tests/test_sync.py` and `tests/test_app.py`: fixtures and the mocking style.
- `Dockerfile`, `compose.yaml`, `.env.example`, `README.md`.

## 6. Simplest Implementation Strategy
- **One new module, `crypto_sync.py`.** It holds the CoinGecko client and the crypto sync, kept separate from `sync.py` because the sources, rules and schedule are unrelated.
- **A separate SQLite file, `CRYPTO_DB_PATH`** (default `crypto.db`, `/data/crypto.db` in Docker). The two syncs then never compete for a write lock, and the stock DB stays untouched.
- **Table `crypto`, using the stock column names wherever the meaning matches.** That way `DISTANCES`, sorting, bounds and grouping in `app.py` work unchanged:
  - `ticker` holds the CoinGecko `id` and is the primary key.
  - `symbol`, `name` and `market_cap_rank` describe the coin.
  - `opening_price` holds the current price.
  - `year_max`, `year_min` and `year_ref`: `year_ref` is the year that `year_max`/`year_min` belong to.
  - `all_time_max` and `all_time_min` hold CoinGecko's `ath` and `atl`.
  - `status_tag` and `last_updated` work as for stocks.
- **Where the yearly high/low comes from** (keeps the free budget):
  1. On every run, each coin's `high_24h`/`low_24h` widens its stored `year_max`/`year_min`. When `year_ref` is not the current year, the values are reset first.
  2. Coins whose current-year values have never been backfilled get one `market_chart?days=365` call each, filtered to the current year. That's at most `CRYPTO_HISTORY_CALLS_PER_RUN` coins per run (default 200), best market-cap rank first. A coin is backfilled once a year. The values are close to the true figures, because they come from daily prices plus each day's 24h high/low from then on.
- **Status:** `DELISTED_OR_ZERO` when the price is missing or 0, or the 24h volume is 0. Otherwise `ACTIVE`. There is no `NEW_<year>`, because CoinGecko markets data has no listing date.
- **App:**
  - A `mode` query parameter, whitelisted to `stocks` or `crypto` (default `stocks`), picks the DB file, the table and the column list. `_query_rows` gets the mode, and the table name comes only from the whitelist.
  - The country filter is hidden in crypto mode. The status filter is reused.
  - Crypto prices are shown with significant digits (`"%.6g"`) so PEPE does not show as 0.00.
- **Scheduling:**
  - Separate `_crypto_lock`, `last_crypto_summary` and a daily scheduler thread at `CRYPTO_SYNC_TIME_UTC`, every day of the week.
  - `next_run` gets a `weekdays_only` parameter.
  - New route `POST /sync/crypto`.
- **Avoid:** a generic data-source abstraction, a second Flask app or template, Binance, and new dependencies.

## 7. Security Considerations
- **API key:**
  - Sent only in the `x-cg-demo-api-key` header to the fixed host `https://api.coingecko.com/api/v3`, never as a query parameter, so it can't leak through URLs in exceptions or logs.
  - Never logged.
  - If it is missing, the sync raises `ValueError` before making any request, like `fetch_instruments`.
- **Path parameter:** the coin `id` in the `market_chart` path comes from the API. It is URL-encoded with `urllib.parse.quote(id, safe="")` so it can't change the path or host.
- **Env validation:**
  - `CRYPTO_VS_CURRENCY` must match `^[a-z]{3,5}$`.
  - `CRYPTO_SYNC_TIME_UTC` is validated like `SYNC_TIME_UTC`.
  - `CRYPTO_HISTORY_CALLS_PER_RUN` must be an integer from 0 to 1000.
- **SQL:** the table and DB path come from a fixed mode whitelist, and all values are bound parameters. No request value goes into an SQL f-string.
- **Untrusted data:** coin names and symbols are autoescaped. There are no external images or links except the attribution link (`rel="noopener"`).
- **Routes:** `POST /sync/crypto` follows `POST /sync` (POST only, background thread, lock). The app stays bound to 127.0.0.1 with no login, as today.
- **Request timeouts:** every HTTP request has a timeout (`REQUEST_TIMEOUT`).

## 8. Implementation Phases

### Phase 1: Crypto sync backend
**Status:** Done — Passed Review
**Depends on:** None

**Implementation notes (2026-11-14 rework after Review Failed):**
- The table has `history_year` (the year the one-off history call was done) next to `year_ref` (the year the stored high/low belongs to). `year_ref` is set on every upsert, so the 24h values build up from day to day. The history call result is merged with the stored values (max/min), not written over them.
- Network errors (timeouts, connection resets) get the same 3 attempts as HTTP errors. The rate-limit wait goes back to 60 s after a successful call.
- Markets pages are fetched in `order=id_asc`, a stable order, so coins aren't skipped or repeated while paging. Duplicate ids are dropped. History calls still go in market-cap order.
- `.env.example` leaves `CRYPTO_DB_PATH` commented out, so it can't override Docker's `/data/crypto.db`.
- The table also has an `exchange` column (always NULL), so Phase 2 can reuse the stock query's column list.
- Deviation from Step 3: nothing is written until every markets page has been fetched. A run that fails while fetching pages raises without writing anything, instead of keeping the rows it already had.
- Live run (scratch DB, cap 5): 18,230 coins, 79 calls, 3 min 15 s. BTC `all_time_max` is 126080, which equals CoinGecko's `ath`. The 5 history calls went to ranks 1–5. 6,462 coins are `DELISTED_OR_ZERO` (their 24h volume is 0). No coin has a null price.
- Second rework (after the second review): added `test_http_error_log_and_exception_exclude_key`. A 429 always waits at least `REQUEST_DELAY` (2.5 s), even with `Retry-After: 0`. Failed history calls are logged with the coin id. `.env.example` notes that changing `CRYPTO_VS_CURRENCY` needs a fresh crypto DB.

#### Goal
`crypto_sync.run_sync()` fills the `crypto` table in its own DB file from CoinGecko, within the rate limit and the free budget, and returns a summary.

#### Scope
- New `crypto_sync.py`:
  - **Config:** constants and env settings.
  - **`init_db`:** creates the `crypto` table.
  - **`_CoinGecko` client:**
    - pacing of at least 2.5 s between calls
    - back-off on 429: honour `Retry-After` if present, otherwise 60 s doubling up to 15 min, and the call is retried, never counted as a failure
    - 3 attempts for other errors
  - **`fetch_markets`:** all pages, stopping at the first empty page.
  - **`fetch_year_range(id, year)`:** reads `market_chart` for one coin.
  - **`compute_status`.**
  - **`run_sync`:**
    1. Upsert all coins from the markets data.
    2. Widen the year values from `high_24h`/`low_24h`.
    3. Backfill up to N coins.
    4. Return the summary: `coins`, `backfilled`, `backfill_pending`, `failed`, `calls`, `started_at`, `finished_at`.
- New `tests/test_crypto_sync.py`, with `requests` and `time.sleep` mocked.
- `Dockerfile`: add `crypto_sync.py` to `COPY`, and set `ENV CRYPTO_DB_PATH=/data/crypto.db`.
- `.env.example`:
  - Drop the Binance mention.
  - Add `CRYPTO_HISTORY_CALLS_PER_RUN=200` and `CRYPTO_DB_PATH` (optional).

#### Do Not Change
`sync.py`, `app.py`, templates, the stock DB, `compose.yaml`, `requirements.txt`.

#### Reuse First
- Copy the patterns, not the code, from `sync.py`:
  - the missing-key `ValueError` before any request
  - `REQUEST_TIMEOUT`
  - the back-off loop shape of `_Yahoo.history`
  - the `ON CONFLICT DO UPDATE` upsert
  - commits in chunks
- Use the `requests` library that's already installed.

#### Steps
1. Inspect `sync.py` and `tests/test_sync.py` for the patterns listed above.
2. Check the Unknowns with at most 3 live calls:
   - one 429 response's headers, if one happens to come up
   - whether any coin has a `null` price
   - the last markets page
3. Implement `crypto_sync.py`. A sync that fails on page N keeps the rows it already committed. A markets page that keeps failing after retries makes the run raise, so a partial list isn't reported as complete.
4. Write tests:
   - pagination stops at an empty page
   - 429 waits and retries without failing
   - a missing key makes no request
   - the key is sent in the header and not in the URL
   - the coin id is URL-encoded in the path
   - the year values widen, and reset when the year changes
   - the backfill respects the per-run cap and the rank order
   - the status rules
   - a rerun updates rows instead of duplicating them
5. Update `Dockerfile` and `.env.example`.
6. Run the full test suite. Run one live sync against a scratch DB in the scratchpad, with `CRYPTO_HISTORY_CALLS_PER_RUN=5`.

#### Acceptance Criteria
- All tests pass, both the existing and the new ones.
- A live run against a scratch DB stores at least 15,000 coins and bitcoin's `all_time_max` equals CoinGecko's `ath`. The summary shows `calls` ≤ 85 when the backfill cap is 5.
- With `COINGECKO_API_KEY` empty, `run_sync` raises `ValueError` and `requests.get` is never called.
- No log line or exception message contains the API key (checked by a test that captures the logs on 429 and on HTTP errors).
- The stock DB file and the `stocks` table are unchanged by a crypto run.

#### Verification
The test command from §3, then the live scratch-DB run: `docker run` with `--user $(id -u)`, `--env-file .env` and `CRYPTO_DB_PATH` pointing into the scratchpad.

#### Security Check
- The key appears only in the header.
- `quote(id, safe="")` is used on the path.
- Env values are validated, all SQL values are bound, and every request has a timeout.
- Grep the diff for the key's prefix `CG-` and for `print(`.

### Phase 2: Dashboard crypto mode and scheduling
**Status:** Done — Passed Review

**Implementation notes (2026-11-14 rework after Review Failed):**
- Each entry in `MODES` now lists its search columns and default sort. Crypto search covers `ticker` (the CoinGecko id), `symbol` and `name`, so searching "btc" finds Bitcoin first. The column names come only from `MODES`.
- Crypto defaults to sorting by `market_cap_rank` ascending, with unranked coins last. Stocks still default to `ticker`.
- The "New this year" status option is hidden in crypto mode. The search placeholder reads "Symbol or name" there.
- `README.md` has a Crypto section covering the source, the key, the schedule, the yearly high/low and the attribution.
- Checked on the 18,230-coin scratch DB: "btc" returns Bitcoin as the first row, the default order starts BTC, ETH, USDT, BNB, USDC, and unranked coins come last.
- Second rework (after the second review): the README now gives the CoinGecko usage per run (about 275) and per month (about 8,250 at one run a day). The search box's screen-reader label follows the mode ("Search symbol or name" / "Search ticker or name"), covered by `test_search_label_follows_mode`.
**Depends on:** Phase 1

#### Goal
The dashboard switches between stocks and crypto, the crypto sync runs daily and on demand, and CoinGecko is credited.

#### Scope
- `app.py`:
  - **Modes:** a `MODES` whitelist (DB path, table, columns) and a `mode` parameter, threaded through `_query_rows`, `index` and the sort/filter URLs.
  - **Crypto columns:** Rank, Symbol, Name, Price, Year High, Year Low, All-Time High, All-Time Low, Status, Updated and the 4 distances.
  - **Scheduler:** `next_run(weekdays_only=...)`, `_crypto_lock`, `last_crypto_summary`, `run_crypto_sync_job`, a second scheduler thread, and `POST /sync/crypto`.
- `templates/index.html`:
  - **Navbar:** a **Stocks | Crypto** toggle (two links, the active one highlighted), plus the badge and **Sync Now** for the current mode.
  - **Form:** a hidden `mode` input.
  - **Crypto mode:** the country filter is hidden and prices are shown with `%.6g`.
  - **Attribution:** "Crypto data provided by CoinGecko", linking to coingecko.com, in crypto mode.
- `tests/test_app.py`:
  - mode switching
  - an invalid `mode` falls back to stocks
  - the crypto query
  - `POST /sync/crypto` starts the crypto job only
  - the locks are independent
  - `next_run` every day vs. weekdays
  - the attribution appears in crypto mode
  - no `<script>`
- `README.md`: a short crypto section covering the source, the key, the schedule and the attribution.

#### Do Not Change
`sync.py`, `crypto_sync.py` (apart from fixing a bug found here, which must be reported), stock behaviour and URLs. A URL without `mode` must render exactly as today.

#### Reuse First
- Reuse `DISTANCES`, `BUCKETS`, `_group_rows`, `_parse_pct`, `_parse_sorts`, `_header_sorts`, `run_sync_job`'s lock pattern, `_scheduler_loop` and the existing template.
- Don't add a second template.

#### Steps
1. Inspect `app.py`, the template and `tests/test_app.py`.
2. Add `MODES`, and make the table and DB path and the column list depend on the mode. Sort columns are checked against the per-mode whitelist.
3. Add the crypto scheduler, lock, summary and route.
4. Template: add the toggle, the mode-aware badge and button, the hidden input, the price format and the attribution.
5. Write the tests, then run the full suite.
6. Render both modes with a test DB plus a copy of the scratch crypto DB, and take screenshots with headless Chrome.

#### Acceptance Criteria
- `/` and `/?mode=stocks` render the stock table as before. All existing tests pass unchanged, except for intended label changes.
- `/?mode=crypto` renders coins from `crypto.db`.
- `/?mode=foo` renders stocks.
- `/?mode=crypto&min_pct_all_time_max=0&max_pct_all_time_max=20` shows only coins between 0 % and 20 % below their ATH, both ends included.
- Clicking a column header keeps `mode=crypto`, and so does pressing Apply.
- `POST /sync/crypto` returns 302 and starts only the crypto sync. While the stock lock is held, the crypto sync still starts, and the reverse holds too.
- The crypto scheduler fires on Saturday and Sunday. The stock scheduler still skips weekends.
- Crypto mode shows the CoinGecko credit link. The page contains no `<script>`.
- A price of 0.0000035 is shown as `3.5e-06` or `0.0000035`, not `0.00`.

#### Verification
The full test command from §3, plus screenshots of both modes from the rendered HTML.

#### Security Check
- `mode` is whitelisted, and the table name never comes from the request.
- No `|safe`.
- The new route is POST-only.
- The diff contains no new request values in SQL f-strings.

### Phase 3: Deploy without interrupting the stock sync
**Status:** Done — Passed Review (2026-11-15). Dependencies Phase 2 and Phase 4 are Done. Re-review: the deploy interrupted no sync (Phase 3 and Phase 4 rebuilds both started with no sync running); a full stock sync on the deployed image finished with its summary line (Phase 4: 21:47→01:09 UTC, ok=8678, no_data=1634, failed=1); crypto summary 18,230 coins, failed=0, calls=274 (2026-11-14 and the 2026-11-15 02:00 run); `/?mode=crypto` shows 18,230 rows; stock row count unchanged across the Phase 3 rebuild (6,567 → 6,567). Security: port 127.0.0.1:8000, no `.env` in `/app` (`.dockerignore`), 0 `CG-` hits in `docker compose logs`. Previous status: Review Failed — the stock run before the rebuild was killed about 20:34–20:38 UTC, before this phase started.
**Depends on:** Phase 2, Phase 4

**Plan update (2026-11-14):** the first criterion below could not be met as written. Neither sync logs a summary line (the summary only lives in memory as `last_summary` and is lost on restart), and the stock run before the rebuild was killed at about 20:34–20:38 UTC. That criterion is now met by Phase 4's completed stock run on the deployed image. The crypto and row-count criteria were checked in the 2026-11-14 run below. Phase 3 is re-reviewed once Phase 4 is `Done — Passed Review`.

**Verification notes (2026-11-14):**
- `docker compose up -d --build` rebuilt and restarted the container; `/health` returned 200.
- `POST /sync/crypto` returned 302; first crypto sync completed: 18,230 coins, Backfilled: 200, Failed: 0.
- `/?mode=crypto` lists coins; port still bound to 127.0.0.1:8000; `.env` not in image (`.dockerignore`).
- Stock DB untouched: 6,567 rows.
- Caveat: the stock sync run before the rebuild was killed by a gunicorn worker timeout/SIGKILL at 20:38 UTC — before Phase 3 started, not caused by this phase. No sync was running when Phase 3 began, so nothing was interrupted by the rebuild.

#### Goal
The new image runs in Docker and the first crypto sync completes, without stopping a running stock sync.

#### Scope
- Rebuild and restart the container with `docker compose up -d --build`.
- Run the first crypto sync with `POST /sync/crypto`.
- Verify the result.

#### Do Not Change
Code, unless a deployment bug is found. In that case stop and report.

#### Reuse First
The existing compose setup and the `data` volume. `crypto.db` lives on the same volume.

#### Steps
1. Check that no stock sync is running: the dashboard shows no "Sync running…" badge, and `docker compose logs` shows the stock run's final summary line.
2. If a stock sync is running, wait for it to finish, using a background wait loop like the earlier one. Never rebuild while it runs.
3. Rebuild, then wait for `/health`.
4. Trigger `POST /sync/crypto` and follow the logs until the summary appears.
5. Check the row count and the summary, and open `/?mode=crypto`.

#### Acceptance Criteria
- No stock sync was interrupted by the deploy, and a full stock sync on the deployed image completes with a final summary line in the logs (evidenced by Phase 4).
- The crypto summary shows at least 15,000 coins, `failed` = 0 and `calls` ≤ 280.
- `/?mode=crypto` lists the coins.
- The stock row count is the same before and after.

#### Verification
- `docker compose logs`
- a row count through `docker compose exec app python -c ...` for both DB files
- `curl` on `/?mode=crypto`

#### Security Check
- The port is still bound to 127.0.0.1.
- `.env` is not in the image (`.dockerignore`).
- No key appears in `docker compose logs`.

### Phase 4: Keep long syncs alive and logged
**Status:** Done — Passed Review (2026-11-15). Review: diff limited to `app.py` (4 log calls ×2 jobs + `_summary_counts`) and `tests/test_app.py` (4 tests); no change to `sync.py`, `crypto_sync.py`, template, `Dockerfile` or `compose.yaml`; 132 tests pass. The 02:00 UTC crypto run also logged `crypto sync started` → `crypto sync finished: coins=18230, backfilled=200, failed=0, calls=274` (11 min) and the crypto badge shows OK; 0 `CG-` hits in the container log. Optional: the cause of the original 2026-11-14 kill stays unconfirmed (not reproduced; likely the restarts that night).

Execution notes: the kill did not reproduce, so no fix was applied (step 5, first branch). Evidence from the 2026-11-14/15 run:
- Log: `stock sync started` 21:47:27 UTC → `stock sync finished: ok=8678, no_data=1634, failed=1, unmapped=59` 01:09:04 UTC (3 h 22 min). One `Booting worker` (at startup, before the run); 0 `WORKER TIMEOUT`, 0 `SIGKILL`, 0 rate limits, 0 tracebacks.
- Counts: 8678 + 1634 + 1 = 10,313 mappable stocks. The one failure is `CASH.PA`, logged each pass as `YFInvalidPeriodError … Period 'max' is invalid, must be one of: 1d, 5d` (Yahoo serves no long history for it; handling that would be a `sync.py` change, out of scope).
- DB: 8678 `stocks` rows have `last_updated` inside the run window.
- `curl /` shows `OK: 8678, No data: 1634, Failed: 1, Unmapped: 59`.
- Memory stayed 122–134 MiB of 13.5 GiB (`phase4_stats.log`), so memory was not a factor.
- No crypto sync overlapped (stock run ended 01:09, crypto is scheduled 02:00), so the overlap criterion did not apply.
- Security: saved log has 0 hits for `CG-` and for the T212 key/secret prefixes. No `Dockerfile` change.
- 132 tests pass.
- Note (out of scope): yfinance logs `Permission denied: '/home/app'` for its TzCache/CookieCache; harmless, but the cache is off.
**Depends on:** Phase 2

#### Goal
A full stock sync (about 4 hours) runs to completion inside the deployed container without the gunicorn worker being killed. Both syncs log a start line and a final summary line, so you can see after the fact whether a run finished.

#### Scope
- `app.py`:
  - **Logging:** `run_sync_job` and `run_crypto_sync_job` log one `INFO` line when a run starts and one with the summary when it ends. A failed run logs one `ERROR` line with the traceback (`log.exception`).
  - **Skipped runs:** a sync that's skipped because one is already running logs one `INFO` line.
  - **Summary content:** counts and timestamps only, never the API key or ticker lists.
- **The fix for the worker kill,** chosen from what step 2 below shows. Likely candidates:
  - **If the logs show `WORKER TIMEOUT`:** set an explicit gunicorn `--timeout` in the `Dockerfile` `CMD`, or another gunicorn setting the evidence points to.
  - **If memory runs out:** reduce what the sync keeps in memory.
  - **If the evidence points elsewhere:** stop and report before changing anything.
- `tests/test_app.py`: tests for the start and summary log lines, the error line, and the skipped-run line.
- **No change to** `sync.py`, `crypto_sync.py` or the template unless the evidence points there. In that case, stop and update the plan first.

#### Do Not Change
- The sync logic, the database schemas, the dashboard and `compose.yaml` (port, volume).
- The one-worker setup (`-w 1`): the scheduler threads rely on exactly one worker.

#### Reuse First
- The existing `logging.basicConfig` in `app.py`.
- The `sync:` progress lines.
- The `/sync` route to start a run.

#### Steps
1. **Add the start, summary, error and skipped-run log lines to `app.py`,** with tests. Run the full suite.
2. **Rebuild only when no sync is running.** Check that the dashboard shows no "Sync running…" in either mode and that no `sync:` progress line has appeared in the last 5 minutes.
3. **Reproduce with evidence:**
   - Start one background job that copies `docker compose logs -f` into a file in the scratchpad. The evidence then survives restarts and rebuilds.
   - Start a second background job that records `docker stats --no-stream` (memory and CPU) once a minute into a file.
   - Then press **Sync Now** in stock mode.
4. **Wait for the run to end** (about 4 hours, up to 5 with retry passes), using a background wait loop. Don't rebuild or restart the container during the run.
5. **Then:**
   - **If the run finishes** with its summary line and no `WORKER TIMEOUT`, `SIGKILL` or `Booting worker` line after startup: the kill didn't reproduce. Record that and skip to step 7.
   - **If the worker is killed:** find the cause from the saved logs and the memory record, apply the smallest fix from Scope, and add a test where practical (e.g. that the `Dockerfile` `CMD` contains the chosen setting). Then repeat steps 2–4.
6. **If the cause is neither a timeout nor memory,** set this phase to `Blocked` with the evidence and stop.
7. **Check the results** against the acceptance criteria below.

#### Acceptance Criteria
- The saved log file contains `stock sync started` and a final stock summary line. Between them there is no `WORKER TIMEOUT`, `SIGKILL` or `Booting worker` line.
- **The summary's numbers add up and show no gaps:**
  - `ok + no_data + failed` equals the number of mappable stocks (10,313 on 2026-11-14, or whatever the run reports).
  - `failed` is 0, or every remaining failure is listed in `failed_tickers` with a reason in the log.
- At least `ok` rows in the `stocks` table have a `last_updated` within the run's time window.
- After the run, the stock dashboard shows the `OK: …` badge, not "Sync running…" or an error.
- **If a crypto sync runs during the stock sync** (scheduled at 02:00 UTC, or started by hand): both finish, and both summary lines are in the log.
- A test checks each new log line (start, summary, error, skipped). The full test suite passes.

#### Verification
- the test command from §3
- the saved log and `docker stats` files in the scratchpad
- a count of rows by `last_updated` through `docker compose exec app python -c ...`
- `curl` on `/`

#### Security Check
- The new log lines contain counts and timestamps only: no API key, no `.env` values, no full ticker lists.
- Grep the saved log file for `CG-` and for the Trading 212 key prefix, without printing the key.
- Any `Dockerfile` change keeps the non-root user, the one-worker setup and the 127.0.0.1 port binding.


### Phase 5: Recover missing stocks via ISIN and add ETFs
**Status:** Superseded on 2026-11-22 by `docs/PLAN_stock_db.md`; its still-relevant ISIN checks are in that plan's Phase 5. Previous status: Partial — review findings 1–5 fixed (`run_sync` now keyed by T212 ticker; saved match tried first; ISIN match saved and counted only after Yahoo returns prices; `_isin_symbol` removed; filter row `colspan="5"`), 6 new tests, 152 tests pass; deployed 2026-11-15 08:48 UTC (migration added `instrument_type`, 8,678 rows → STOCK); stock sync started 08:48:49 UTC with logs in the scratchpad (`phase5_app.log`). Remaining: wait for the run, check the acceptance criteria, run a second sync to check that saved matches are reused. Previous: Review Failed — 5 blocking issues (reproduced 2026-11-15 with the test fakes, script in the session scratchpad) and the live-run criteria are unverified (not deployed: the live `stocks` table has no `instrument_type` column). 146 tests pass.
Blocking:
1. Saved matches are never used for mapped tickers. `run_sync` `continue`s on a `to_yahoo` match before reading `yahoo_symbols`, so `DMYD_US_EQ` asks Yahoo for `DMYD` again and repeats the ISIN lookup every run (2 lookups in 2 runs). This breaks the "second run is faster" criterion, and saved rows for mapped tickers are never deleted.
2. Yahoo-symbol collisions drop instruments. `symbols` is keyed by Yahoo symbol, so an ISIN match equal to another instrument's symbol overwrites it. Example: `3ASMl_EQ` and `3ASMl1_EQ` both resolve to `3ASM.L`. Only `3ASMl1_EQ` gets a row, the main listing is lost, and `ok + no_data + failed` is 1 for 2 instruments. Key the work list by T212 ticker.
3. Crash after a fallback. When `_upsert` raises after the ISIN fallback, `errored.append(symbol)` adds the resolved symbol (`GENI`), which isn't a key of `symbols`. The next pass raises `KeyError('GENI')` and the whole sync dies.
4. `resolved_by_isin` is counted, and the match saved, before any data is confirmed. A match whose history is empty still counts as resolved and stays in `yahoo_symbols` (`OLD_US_EQ`→`NEW` saved, `no_data=1`, `resolved_by_isin=1`).
5. The dashboard filter row is misaligned. The header has 15 cells but the filter row only 14 (the Type `<th>` + `colspan="4"` should be Type + `colspan="5"`). Status, Apply and the % inputs sit one column to the left.
Optional: `_isin_symbol` only wraps `yahoo.isin_to_symbol`. A lookup that fails on network errors ends as `no_data` (the plan allows this). Yahoo often answers Frankfurt ISINs with `.DE`, which is rejected for `d` tickers. The header still says "Trading 212 Stocks". Add tests for 1–5, and a test for the main mapped-ticker case (`DMYD`→`GENI` on the first run).
**Depends on:** Phase 4

Added 2026-11-15 at the user's request ("yes add the phase to the plan, include etfs"). This phase changes the stock sync, which §2 otherwise rules out for this plan; the user approved the exception.

#### Goal
- **Recover missing stocks.** Stocks whose Trading 212 ticker Yahoo doesn't know (old SPAC names, renames) are found again through their ISIN.
- **Add ETFs** to stock mode, clearly marked as ETFs.
- **Report what's left.** Only instruments Yahoo really has no data for stay out, and the sync summary says how many were recovered.

#### Current Understanding (2026-11-15, from the Phase 4 run and the live instrument list)
- **Instrument list:** 17,183 instruments in Trading 212: 10,372 `STOCK`, 6,790 `ETF` and 21 `WARRANT`. `run_sync` keeps only `STOCK`.
- **Stock results:** 8,678 OK, 1,634 no data, 1 failed (`CASH.PA`, 1d/5d history only) and 59 unmapped. Of the 1,634 no-data stocks, 1,235 are US, 130 LSE, 86 Frankfurt and 58 TSX.
- **ISIN sample** (12 random no-data stocks, looked up with `yf.Ticker(isin).ticker`):
  - **10 resolved to a ticker with data:** `DMYD`→`GENI`, `TWCT`→`CLBT`, `SRM`→`TRON`, `CARR.L`→`FVA.L`, `FIHU.TO`→`FIH-U.TO`, among others.
  - **1 resolved to a different market:** Embraer `ERJ` came back as `EMY.F` (Frankfurt).
  - **1 lookup raised `ValueError`:** `CCL.L`.
- **ETFs:** 6,360 of the 6,790 map with today's `to_yahoo`.
- **Unmapped endings** (stocks and ETFs):
  - `…m_EQ` is Borsa Italiana Milan, Yahoo `.MI`. It covers about 250 ETFs (`Sm`, `Lm`, `Em`, …).
  - `…d1_EQ` and `…l1_EQ` are secondary listings (about 85 instruments).
- **Expected run time:** about 16,700 mappable instruments at about 1.2 s each, plus ISIN lookups for the misses. Estimate 6–7 hours for the first run. Later runs are shorter thanks to the saved ISIN matches. The run can overlap the 02:00 UTC crypto sync; the two use separate locks and sources.

#### Scope
- **`sync.py`:**
  - **Include ETFs:** keep instruments of type `STOCK` and `ETF`, not warrants.
  - **Store the type:** add an `instrument_type` column to `stocks`. `init_db` adds it to an existing DB with `ALTER TABLE … ADD COLUMN` when it's missing. Existing rows get `STOCK`.
  - **Milan:** add `"m": (".MI", "MILAN")` to `EXCHANGE_LETTERS` and `"MILAN": "Italy"` to `EXCHANGE_COUNTRIES`.
  - **ISIN fallback:** when an instrument has no data under its mapped symbol, or can't be mapped at all, and it has an ISIN, look up the Yahoo symbol by ISIN. The lookup goes through `_Yahoo`, so it gets the same pacing, rate-limit back-off and retries. Then fetch the history for that symbol.
    - **Accept the match** only if the returned symbol matches `^[A-Z0-9.\-^=]{1,20}$` and its suffix maps to the instrument's expected exchange. For instruments that couldn't be mapped, any known suffix is accepted and its exchange is used.
    - **Otherwise** count the instrument as `no_data` and log one `INFO` line naming the ticker and the rejected symbol. That keeps a different market (e.g. `EMY.F` for a US stock) out of the table.
  - **Save the matches:** a `yahoo_symbols` table (`ticker` PK, `yahoo_symbol`, `resolved_at`) in the same DB. On later runs a saved match is used first. If it returns no data, it's deleted and resolved again once.
  - **Summary:** add `resolved_by_isin` (the count). Everything else stays the same. `ok + no_data + failed` still equals the number of instruments tried.
- **`app.py` and `templates/index.html`** (stock mode only):
  - **Type column:** a "Type" column showing `STOCK`/`ETF`.
  - **Type filter:** a Type filter (All / Stock / ETF) in the filter row, whitelisted like `status`.
  - Crypto mode is unchanged. The no-JS rule still applies.
- **`tests/test_sync.py` and `tests/test_app.py`:** tests for every behaviour above.
- **`README.md`:** what is synced (stocks and ETFs), the ISIN fallback, and the new run time.

#### Do Not Change
- Crypto mode, `crypto_sync.py` and `crypto.db`.
- The Yahoo pacing and back-off constants, the scheduler and the one-worker setup.
- The existing columns and their meaning.
- Warrants stay out.

#### Reuse First
- `_Yahoo` for pacing, back-off and retries.
- `to_yahoo` and the `EXCHANGE_LETTERS`/`COUNTRY_CODES` maps, also reversed to check a suffix against an exchange.
- `_upsert`, the retry passes and the `status` filter pattern in `app.py`/the template.

#### Steps
1. **Inspect yfinance 1.7.0's ISIN lookup** (`yf.Ticker(isin)` / `yfinance.utils.get_ticker_by_isin`). Check:
   - which HTTP call it makes
   - how it fails: the `ValueError` seen for `CCL.L`
   - whether it raises `YFRateLimitError` when Yahoo rate-limits it

   Then wrap the lookup in `_Yahoo`. A failed lookup is `no_data`, never a crash. A rate limit backs off.
2. **Make the schema changes:** add `instrument_type` and `yahoo_symbols`, with a migration test on a DB in the old schema.
3. **Add ETFs and the `.MI` mapping, then the ISIN fallback,** with tests using a fake Yahoo:
   - a resolved symbol on the same exchange is used
   - a resolved symbol on another exchange is rejected
   - a saved match is reused
   - a saved match with no data is resolved again
   - an unmapped ticker with an ISIN is resolved
4. **Add the Type column and filter to the dashboard,** with tests: each filter value, a bad value is ignored, crypto mode has no Type filter, and there's still no `<script>`.
5. **Update the README.** Run the full test suite.
6. **Deploy following the Phase 3 rule,** only when no sync is running. Start a stock sync with the Phase 4 log capture and wait for it to end.
7. **Check the acceptance criteria.**

#### Acceptance Criteria
- **The sync finishes with its summary line,** and no worker restarts happen during it.
- **Counts add up:** `ok + no_data + failed` equals the mapped STOCK+ETF instruments plus the unmapped ones that have an ISIN. `unmapped` counts only instruments with neither a mapping nor an ISIN match.
- **Recovered instruments:** `resolved_by_isin` > 0. Of the 10 instruments the sample resolved, at least 8 are in the table with `last_updated` in the run window, for example `GENI` data under T212 `DMYD_US_EQ`.
- **No wrong markets:** no row's `exchange` disagrees with the market its T212 ticker names. The Embraer `ERJ_US_EQ` is either US data or absent, never Frankfurt data.
- **ETFs are in:** `stocks` has more than 5,000 rows with `instrument_type = 'ETF'`, and the Type filter shows only ETFs when ETF is chosen.
- **The second run is faster** for the resolved instruments, because it reads the saved matches: no ISIN lookup is logged for them.
- **The full test suite passes.**

#### Verification
- the test command from §3
- the Phase 4 log capture
- `docker compose exec app python -c ...` for row counts by `instrument_type` and `last_updated`, and the `DMYD_US_EQ` and `ERJ_US_EQ` checks
- `curl` on `/?type=ETF` and `/?mode=crypto`

#### Security Check
- **ISINs are public identifiers.** Only the ISIN goes to Yahoo; no Trading 212 credentials.
- **Resolved symbols are untrusted.** They're validated against the pattern before storage, used only as yfinance arguments and in parameterised SQL, and rendered autoescaped.
- **The Type filter is whitelisted.** No user value reaches SQL unparameterised.
- **Log lines** name tickers and symbols only, never keys.
