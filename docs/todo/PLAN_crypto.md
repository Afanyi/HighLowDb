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
- No change to the stock sync, the stock table, the stock DB file or the stock schedule.
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
**Status:** Not Started
**Depends on:** Phase 2

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
- No stock sync was interrupted: the stock run before the rebuild has a final summary line in the logs.
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
