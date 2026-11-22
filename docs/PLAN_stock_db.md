# Implementation Plan: `stock-db` 1,000 Volatile Stocks

## 1. Goal

Turn the existing stock dashboard into a small, correct, manually refreshed list of the
**1,000 most volatile eligible Trading 212 stocks across 23 developed markets**.

For each selected stock, store only:

- all-time max and min;
- current price, with its price date;
- calendar-year max and min, if available.

A missing value is better than a wrong value.

## 2. Non-Goals

- No all-stock coverage, ETFs, warrants or funds in the selected list.
- No price-history tracking, `daily_bars` table, alerts, charts or trading signals.
- No automatic stock sync, timer, code deployment or second scheduler.
- No new service, container, database engine, job queue or provider framework.
- No TradingView data, duplicate provider accounts, key rotation, proxies or IP rotation.
- No publication, sharing or commercial use of raw data.
- No crypto changes. Crypto keeps its automatic CoinGecko schedule.

## 3. Constraints

- **Owner decision on Yahoo (2026-11-22):** Yahoo stays the primary source. The owner was told
  its [terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html) prohibit automated
  collection and building a database without express prior permission, including through a
  manually started script, and accepted that risk. Never describe Yahoo data as licensed,
  permitted or verified. Yahoo can block the server, so failure must leave old values intact.
- Validators are used only within their own current terms. If terms are unreadable or do not
  allow the use, skip that validator.
- Extremes use split-adjusted, **not dividend-adjusted** prices (`auto_adjust=False`, never
  `Adj Close`). A calendar year means 1 January to date, not 52 weeks.
- Temporary price history may be held in memory during a run and must then be discarded.
- Keep one Gunicorn worker, four threads, the 512 MiB app limit and the existing deployment gate.
- Reuse the current app, SQLite database and `unittest` style. Add no dependency unless a phase
  records why it is unavoidable.
- Each phase ends with passing checks, a summary and owner approval. No commit or push unless
  the owner asks.

## 4. Current Understanding

### Verified on 2026-11-22

- `sync.py` fetches Trading 212 instruments, maps Yahoo symbols, calculates metrics and upserts
  `stocks`. `app.py` serves the dashboard and manual `POST /sync`.
- `sync.py:212` calls yfinance `history(period="max", actions=False)` with default adjustment.
  The export has 309 rows with `all_time_min <= 0` (277 negative, 244 on Frankfurt), 471 with
  ATH/ATL above 10,000 and 3 without year values. It has no row without a Yahoo symbol.
- The demo Trading 212 list has 10,371 stocks, 6,775 ETFs, 20 warrants and 9,166 unique stock
  ISINs. Stock currencies are USD, EUR, GBX, CAD and CHF. Japan, Australia, the Nordics and
  similar markets appear through foreign lines, so **country is the ISIN prefix**.
- Issuers with ISIN prefixes such as KY, BM, VG, JE and GG are outside the 23 countries unless a
  later reviewed rule maps them.
- The live app still runs release `79623e5c930fd942` and **its automatic stock scheduler is still
  active**. The next run is 2026-11-23 at 14:45 UTC. The local removal is not deployed because
  the session's permission system blocked `deploy/update.sh`.
- The server has 1 CPU, 1.9 GB RAM and about 33 GB free disk, shared with TradingSupport.
- WallStreetOdds is US-only and its terms on automated storage are unclear. EODHD's free plan has
  no stock all-time fields. Neither is part of this plan.

### Findings Phase 1 must confirm or refute

- Prices stop at the 2026-11-13 close.
- Extremes are dividend-adjusted (reported 184 of 185 sampled rows, ROST 0.083, XEB.F about −2.8m).
- Seven wrong Yahoo symbols: Archer Aviation ACIC→ACHR, Barrick Mining GOLD→B, Core Natural
  Resources CNX→CNR, Crane NXT CR→CXT, Mexedia ALAIR.PA→ALMEX.PA, Sono Group SEV→SSM, Under
  Armour Class A UA→UAA.
- LOBO is 10× too low after a reverse split.
- Stored values show float32 artifacts.

### Unknown — inspect before implementation

Live Trading 212 list and quotas; Yahoo fields and throttling for the selected size; whether
each quota can fill after filters; FX source and terms; current Massive and Alpha Vantage terms
and non-US coverage; how yfinance's split-adjusted values behave for serial reverse splits.

## 5. Areas to Inspect

`sync.py`, `app.py`, `refresh.py`, `templates/index.html`, `tests/test_sync.py`, `tests/test_app.py`,
`deploy/release.py`, `deploy/manage.py`, the live stock DB (read-only) and research report
`Volatile stock universe selection` from 2026-11-22.

## 6. Simplest Implementation Strategy

### Target flow

1. **Monthly, manual selection:** fetch instruments, filter, score and save 1,000 members.
2. **About twice weekly, manual sync:** for members only, fetch history, validate, calculate the
   five values, save them and discard history.
3. **Manual validation:** compare a rotating sample with independent providers.

At 0.5 s pacing a member sync needs about 8–10 minutes. Selection is the larger Yahoo job because
it needs 63 sessions for each eligible candidate.

### Country quotas

Use the square root of MSCI ACWI IMI developed-market weights at
[30 June 2026](https://www.msci.com/downloads/web/msci-com/research-and-insights/video/acwi-imi-complete-geographic-breakdown/oct-2026/A%20Complete%20Geographic%20Breakdown%20of%20the%20MSCI%20ACWI%20IMI.pdf),
with the owner's US maximum of 200 and minimum of 10 per country. Quotas are ceilings. Empty seats
stay empty rather than admitting weak stocks. Recalculate from the live ISIN counts in Phase 1.

```python
COUNTRY_QUOTAS = {
    "US": 200, "JP": 104, "GB": 77, "CA": 76, "FR": 62, "CH": 60, "DE": 59, "AU": 54,
    "NL": 50, "ES": 39, "SE": 39, "IL": 26, "BE": 23, "DK": 23, "FI": 22, "NO": 19,
    "AT": 14, "IE": 12, "SG": 12, "IT": 10, "PT": 10, "NZ": 6, "HK": 3,
}  # Total 1,000. NZ and HK have only 6 and 3 unique ISINs in the demo list.
```

### Selection rule (starting values, calibrate on real data)

- Keep `type == "STOCK"` and one line per ISIN, choosing the highest median EUR traded value.
- Over 63 sessions, require at least 57 valid sessions, median traded value of at least €1m,
  at least 90% trading days, at most 6 zero-volume days, at most 20% zero-return days and a
  price of at least about $2. Current members use €0.75m, 85%, 9, 25% and $1.50.
- Exclude a stock with invalid OHLC, high/low of at least 3, or an unexplained split-like jump.
- Rank each country by RYZ63, a Yang-Zhang volatility with daily components clipped at
  median ± 6 scaled MAD. Fill new seats from the top of the country's quota. Keep a current member
  while it ranks within 1.3 × that quota.

### Data changes

Extend the existing migration pattern in `sync.init_db`. Add to `stocks`: `current_price`,
`price_date`, `isin`, `country`, `verification`, `quality_flags`. Add `universe_members`:
`ticker`, `isin`, `country`, `rank`, `volatility`, `selected_at`. No history table.

Verification values: `SINGLE_SOURCE`, `CONSISTENT`, `DISAGREES`. Agreement means consistency,
not proof.

### Validation

Every member must satisfy, when values exist:
`0 < all_time_min <= year_min <= current_price <= year_max <= all_time_max`.
A failed row keeps its last good values, is flagged and is reported.

Massive may check US current prices with one grouped-daily call. Alpha Vantage may check about 22
non-US current prices per day after its terms are confirmed. All-time values remain Yahoo-only.
With validation run only on two sync days, a full 800-stock non-US rotation takes about 18 weeks.

## 7. Security Considerations

Keep secrets in ignored env files and out of logs, reports and fixtures. Treat provider and
Trading 212 text as untrusted: use parameterized SQL, template escaping, CSV formula escaping and
validated symbols. Sync and selection stay behind login and CSRF, with no public maintenance
route or Docker socket. Local exports stay private and Git-ignored.

## 8. Implementation Phases

### Phase 0: Stop automatic stock sync on the server

**Status:** Done — Passed Review
**Depends on:** None

#### Goal

The server no longer starts a stock sync by itself, while manual stock sync and crypto keep working.

#### Scope

The existing uncommitted changes in `app.py` and `templates/index.html`, one manual update and
live verification.

#### Do Not Change

Stock data, sync logic, crypto schedule, proxy, TradingSupport services or server environment.

#### Reuse First

`deploy/update.sh`, including its drain, backup, health check and rollback.

#### Steps

1. Confirm no job is running.
2. Operator runs `bash deploy/update.sh` before 2026-11-23 14:45 UTC.
3. Verify the deployed code, status, data counts and other services.

#### Acceptance Criteria

- The running app has no stock scheduler thread and the dashboard says "manual only".
- No scheduled stock attempt appears after 2026-11-23 14:45 UTC.
- Manual sync route remains protected by login and CSRF.
- Crypto remains scheduled; row counts and TradingSupport container IDs are unchanged.

#### Verification

Local dry run, deployment tests and production checks passed on 2026-11-22. After deployment, use
read-only container inspection, refresh status, logs and row-count queries.

#### Security Check

Confirm no new route, published port, secret output or weakened form protection.

#### Execution Evidence (2026-11-22)

- `bash deploy/update.sh` completed at about 14:38 UTC with packaged offline application tests.
  Live release is `c9bad96e9fd67e20`, started `14:38:23Z`, healthy, `unless-stopped`, with no
  published ports. Backup: `backups/20261122T143815-16bb99e4`.
- Running code has 0 stock scheduler starts and 1 crypto scheduler start. The Stocks page says
  "manual only" and shows no next run; the Crypto page still shows its next scheduled run.
- Stocks 9,695 and crypto 18,311 rows are unchanged; all three SQLite integrity checks are `ok`.
  Stock refresh state is still `never`. Crypto next due is `2026-11-23T02:00:00Z`.
- Without credentials, `/`, `/health`, `POST /sync` and `POST /sync/crypto` return 401. Port 8000
  has no host listener. A valid-login manual sync was deliberately not run.
- All five TradingSupport container IDs are unchanged. `engine` shows a new start time of
  `2026-11-22T10:39:15Z`, about four hours before this update and with the same container ID;
  this update did not cause it.
- The stored stock `due_at` remains but no thread reads it.

#### Review Result (2026-11-22)

- **Done — Passed Review by owner instruction.** The app diff removes only the stock scheduler
  start. Running code has 0 stock scheduler starts, no stock sync started in the logs after
  release, and no app timer or cron entry exists.
- The owner accepted closing the phase before `2026-11-23T14:45:00Z`, so the observation that no
  attempt occurs at that time was **not run**.

### Phase 1: Audit current data and confirm the live universe

**Status:** Not Started
**Depends on:** Phase 0

#### Goal

Produce evidence in `docs/AUDIT.md` and `docs/SOURCES.md` for every later phase.

#### Scope

Read-only code and data inspection, a small read-only Yahoo sample, and those two documents.

#### Do Not Change

Application code, databases, schedules or server configuration.

#### Reuse First

Existing DB, `sync.py` mappings and the 2026-11-22 research report.

#### Steps

1. Confirm or refute every section 4 finding with counts and examples.
2. Fetch the live Trading 212 list with a read-only key and recalculate ISIN counts and quotas.
3. Map countries, exchanges, Yahoo suffixes and currencies, including GBX.
4. Check Massive, Alpha Vantage and FX-source terms and coverage.
5. Sample Yahoo to measure runtime, memory, raw/split-adjusted fields and filter pass rates.

#### Acceptance Criteria

- Every finding is marked confirmed or refuted with evidence.
- The final quota table totals at most 1,000, has US at most 200 and shows each country's supply.
- Every country below 10 is listed with the reason.
- Each provider has limits, terms, source links and check date recorded.

#### Verification

Rerun audit queries on a read-only connection and confirm matching counts. `git diff` contains only
the two new documents and this plan.

#### Security Check

Confirm neither document or sampled output contains a key, secret or credential.

### Phase 2: Correct the five stored values

**Status:** Not Started
**Depends on:** Phase 1

#### Goal

A sync calculates correct all-time, current and year values and never overwrites good data with
invalid data.

#### Scope

`sync.py`, schema migration, seven reviewed symbol overrides, dashboard field use and focused tests.

#### Do Not Change

Universe size, schedules, crypto, deployment or live data.

#### Reuse First

`compute_metrics`, `_Yahoo`, `_upsert`, `init_db`, `yahoo_symbols` and existing tests.

#### Steps

1. Request `auto_adjust=False` and use split-adjusted OHLC only.
2. Store `current_price` and `price_date` from the latest valid session.
3. Reject impossible bars, enforce the ordering rule and retain last good values on failure.
4. Add overrides with name validation and quality flags.
5. Add regression tests.

#### Acceptance Criteria

- Dividend-adjusted fixtures cannot produce negative or non-traded lows.
- XEB.F, TBT.F, ROST, LOBO, UA/UAA, GOLD/B and the other overrides pass regression tests.
- A failed or empty Yahoo response leaves previous values unchanged and flags the row.
- Year values use the calendar year; float64 values are preserved.
- No price history is written to the database.

#### Verification

Run the full `unittest` suite in an isolated app-image container with dummy keys, temporary
databases and no network.

#### Security Check

Confirm parameterized SQL, safe symbol handling and no raw provider error text in stored status.

### Phase 3: Select the 1,000-stock universe

**Status:** Not Started
**Depends on:** Phase 2

#### Goal

A manual selection command produces a reviewable list of top-volatile stocks by country.

#### Scope

Selection logic in the smallest suitable existing or new local module, `universe_members`, a CLI
command, a private CSV review file and tests.

#### Do Not Change

Live membership or dashboard contents until the owner approves the list in Phase 5.

#### Reuse First

`fetch_instruments`, `_Yahoo` pacing, `refresh.job_guard` locking and Phase 1 quotas.

#### Steps

1. Build candidates by ISIN and country.
2. Fetch 63 sessions with resumable progress, then discard them after scoring.
3. Apply gates, RYZ63, quotas and the 1.3× membership buffer.
4. Write a private review CSV with reasons for exclusions and unfilled seats.

#### Acceptance Criteria

- The output has at most 1,000 unique ISINs, at most 200 US and no ETF, warrant or fund.
- Every country is at or below quota. Any country below 10 has a stated supply or filter reason.
- Bad-tick, illiquid, duplicate-listing, IPO and reverse-split fixtures are handled as specified.
- Rerunning with the same fixtures gives the same list.
- Interrupting a run does not change current membership.

#### Verification

Run unit tests with fake providers, then one owner-approved real selection on the server or an
isolated environment, measuring runtime and peak memory.

#### Security Check

Confirm selection has no public route, review files are private and CSV text is escaped.

### Phase 4: Add independent validation

**Status:** Not Started
**Depends on:** Phase 3

#### Goal

Selected rows show whether an independent source is consistent with Yahoo.

#### Scope

Small Massive and Alpha Vantage clients, persisted daily call counts, a manual validation command
and verification labels.

#### Do Not Change

Yahoo-derived values. A validator never overwrites a price.

#### Reuse First

`requests`, existing timeout/backoff patterns and `quality_flags`.

#### Steps

1. Reconfirm terms before coding each provider.
2. Compare same-date US prices through Massive.
3. Rotate non-US checks through Alpha Vantage within a 10% quota margin.
4. Treat currency, unit, date and split mismatches as disagreements.

#### Acceptance Criteria

- No client exceeds its configured limit, and a 401, 403 or 429 stops that provider for the run.
- A mismatch sets `DISAGREES` without changing stored prices.
- A missing validator leaves `SINGLE_SOURCE`.
- The UI never labels a row "verified" or "true".
- No validator data is stored beyond the comparison result allowed by its terms.

#### Verification

Run recorded-fixture tests with no live calls, then the full suite.

#### Security Check

Confirm fixed HTTPS hosts, encoded symbols and no key in logs, stored errors or fixtures.

### Phase 5: Review, replace and release

**Status:** Not Started
**Depends on:** Phase 4

#### Goal

After owner approval, the live dashboard shows the corrected 1,000-stock list.

#### Scope

A rebuild on a database copy, `docs/REBUILD_REPORT.md`, owner review, manual release and first sync.

#### Do Not Change

TradingSupport, crypto data or automatic-sync status.

#### Reuse First

`deploy/data.py` snapshots, `deploy/update.sh` and existing rollback.

#### Steps

1. Build the list and values on a private database copy.
2. Report old vs new values, change reasons, exclusions, unfilled quotas and flags.
3. Stop for explicit owner approval.
4. Back up, release and activate membership.
5. Run one manual sync and verify the dashboard and export.

#### Acceptance Criteria

- **The owner approves the report before live data changes.**
- The live list matches the approved list, with no stored price at or below 0.
- Every row passes the ordering rule or is flagged and reported.
- A backup can restore the previous stock data.
- A second manual sync with unchanged provider data changes no values.
- No automatic stock run occurs.

#### Verification

Run all suites, deployment checks and production checks. Use read-only SQLite checks and app-scoped
Docker inspection after release.

#### Security Check

Confirm login/CSRF coverage, private backups and exports, no published port and clean logs.

### Phase 6: Finish the migrated TradingSupport deployment verification

**Status:** Partial — deployed; Remaining: owner confirmation that the Crypto view loads after login
**Depends on:** Deployment Phases 1–3 in `docs/done/PLAN_deployment.md` (`Done — Passed Review`)

Migrated from Deployment Phase 4 on 2026-11-22. **Owner decision:** automatic stock refresh is
removed, so the scheduled-stock-run step and criterion below are superseded. Crypto's automatic-run
evidence and all other criteria still apply. Historical evidence is preserved unchanged.

#### Goal

Make the protected dashboard live and prove that scheduled stock-data updates run
on the server independently of the operator's computer.

#### Scope

Execute the reviewed setup workflow on the confirmed TradingSupport server; import
existing data or bootstrap as established in Phase 1; integrate only the app's
reviewed route with the existing ingress; collect live evidence in this plan.

#### Do Not Change

TradingSupport's other services/data/credentials, global Docker/SSH settings,
existing provider quotas or other plans' phase statuses. Do not reboot the shared
server just to test this app; inspect boot configuration and test app restart.

#### Reuse First

Reviewed scripts/configuration, actual shared proxy and DNS, persistent data volume,
existing scheduler and persistent refresh-status records.

#### Steps

1. Recheck host identity, headroom, existing service health and DNS. Record redacted
   baseline service identities/start times, app release identity and data counts.
2. Install only this app with private server credentials. Import consistent data
   backups if selected; never merge or overwrite an unknown destination volume.
   Validate initial stock/crypto data without duplicating costly refresh runs.
3. Activate the app's validated HTTPS/authentication route through the existing
   ingress. Preserve the previous proxy configuration for recovery. Reload only
   the proxy if required, verifying existing TradingSupport access throughout.
4. Verify authenticated browser access, blocked unauthenticated routes, private
   upstream, live health, scheduler status and configured UTC stock/crypto times.
5. Recreate/restart only this app while idle; verify both data files, next-run
   status and scheduling recover. Confirm Docker boot enablement and restart policy.
6. With the operator's browser/computer disconnected, observe the next real weekday
   stock trigger through completion. Capture scheduled trigger/start/end, summary,
   failures and database update timestamps. Preserve the existing daily crypto run
   and record its automatic completion as well, without unnecessary extra calls.
7. Check resource use and other services against the baseline. Record hostname,
   release, backup/recovery locations, schedules and exact evidence timestamps.

#### Acceptance Criteria

- The chosen HTTPS hostname serves both datasets with valid credentials; missing
  or wrong credentials fail and port 8000 is not publicly reachable.
- Container and database health are verified; data survives an app-only restart
  and the server is configured to restart the app/schedulers after boot.
- **Automatic stock-data updates are active:** a real scheduled weekday run starts
  and completes on the server with no manual POST, SSH trigger or running local
  computer. Its completion summary and advancing data timestamps are recorded.
- Failures/partial coverage are reported accurately; a completed run with provider
  failures is qualified, not represented as a complete successful refresh.
- The existing automatic crypto schedule is preserved and its real completion is
  evidenced without exhausting the provider budget.
- Existing TradingSupport services stay healthy, with no unintended recreation or
  restart. The new app remains within measured host capacity.
- No automatic code-deployment mechanism is installed or enabled.

#### Verification

Use actual app-scoped Compose status/inspection, private health requests, public
authenticated/unauthenticated HTTPS checks, port inspection, scheduler status,
redacted start/end logs and SQLite timestamp/count/integrity queries. Compare
TradingSupport service identities/health to the baseline. Record the commands and
results actually used; do not publish credentials in URLs or evidence.

A healthy container or a manual sync is insufficient evidence of automatic stock
updates. If the next weekday run or crypto run has not completed, use
`Partial — deployed; Remaining: scheduled refresh evidence` and retain the exact
outstanding observation. Do not mark Ready for Review on mocked/anticipated results.

#### Security Check

Verify public authentication/CSRF coverage, no upstream bypass, private credential
and backup permissions, clean secret-free release/log evidence, scoped proxy
changes and no new exposure of TradingSupport databases or Docker control sockets.

#### Validation Readiness Review (2026-11-21)

- Phase 4 remains `Not Started`; it cannot be marked `Done — Passed Review`.
  Phases 1–3 have passed review, but this plan contains no Phase 4 execution
  evidence. The working tree was clean when this validation was requested.
- Missing evidence: actual TradingSupport installation and release identity,
  protected public HTTPS access and private upstream, database integrity and
  restart persistence, server restart/scheduler configuration, completed automatic
  weekday stock and daily crypto runs, and comparison of other service health
  and host resources against the deployment baseline.
- Phase 3's isolated tests do not establish these live acceptance criteria.
  No live checks or deployment commands were run during this readiness review;
  actual server deployment/access/refresh state remains unverified. Only this
  review record was added. Execute Phase 4 and collect its specified evidence
  before requesting completion review; this validation request did not start
  the rollout or change its status.

#### Execution Evidence (started 2026-11-22 Europe/Berlin)

- Dependencies verified: Phases 1–3 are `Done — Passed Review`. Started Phase 4
  with read-only inventory over strict, noninteractive SSH. Confirmed target
  `live` is `tradingsupport-vnic` (ARM64), Docker 29.1.3, Compose 2.40.3, Docker
  enabled at boot. Inventory timestamp: `2026-11-21T23:15:06Z`.
- Baseline: 1,236,224 KiB available RAM, 1,927,212 KiB free swap and
  35,689,971,712 bytes free disk. All five TradingSupport services were running;
  PostgreSQL was healthy. Container identities/start times recorded below for
  later comparison. No app installation, app container or target app volume was
  present. No port 8000 listener was present.

  | Existing service | Container ID prefix | Started at (UTC) |
  | --- | --- | --- |
  | web | `b34c473f2cc6` | `2026-10-30T10:28:17Z` |
  | engine | `9bf46ff28827` | `2026-11-21T10:38:44Z` |
  | api | `0dc773a2a874` | `2026-10-30T10:28:17Z` |
  | caddy | `d595921cae4f` | `2026-10-29T21:45:26Z` |
  | postgres | `c461f1835dde` | `2026-10-29T20:14:44Z` |

- Verified `trading-assistant_default` remains a non-internal bridge containing
  Caddy, web and engine. Caddy still binds the expected read-only Caddyfile, and
  both existing login settings are populated; no login values were printed.
  Authoritative local and live Caddyfiles match SHA-256
  `8bfcd84e1c7d2609b23c5d6967ee8b0b57ae0a9e6f269385846c25580fbd21fe`.
  Prepared the original and combined app route privately under
  `/tmp/allstocks-phase4-proxy-eb1xbe4n`; neither proxy source nor live proxy was
  changed. TradingSupport updates ship committed files and refuse a dirty source
  tree; the eventual source edit must be included in the operator's deployed code.
- DNS checks from outside the sandbox resolved both `stocks.skyboyz1.duckdns.org`
  and `skyboyz1.duckdns.org` to `92.5.106.5`. Verified HTTPS on the existing host
  returned 401 without credentials. The proposed stocks HTTPS endpoint did not
  establish a verified connection; its app route/certificate have not been enabled.
- Checked the existing local data volume read-only: stocks **9,695** rows,
  crypto **18,230** rows, both SQLite integrity checks `ok`. Latest row timestamps
  were respectively `2026-11-15 09:48:45` and `2026-11-15 02:03:17`. No data was
  copied, initialized or refreshed. Import/bootstrap remains pending installation.
- Created ignored private `deploy/server.env` (0600) with the reviewed existing
  SSH alias/key path. Reran `bash deploy/update.sh --dry-run`: packaged offline
  application tests passed; release `67d598327c896059`, private artifact
  `/tmp/allstocks-release-4jjpr_lh/review`. No server candidate was staged or built.
- Required credential check found no CoinGecko key in this app's environment.
  At the owner's request, checked the ctrade alert bot locally: no CoinGecko
  setting or integration was found, and no source credential was copied. The
  owner then requested a placeholder: appended empty `COINGECKO_API_KEY=` to this
  app's private `.env`, preserving its other values and mode 0600.
- **Blocker:** supply a real CoinGecko Demo API key in the private environment
  before resuming the reviewed setup, which requires both provider keys. An empty
  placeholder is not a valid deployment credential. No application installation,
  proxy modification, live data writes, manual provider run or scheduled refresh
  occurred. Automatic code deployment remains absent. Remaining Phase 4 work is
  installation, proxy integration, live security/persistence/resource checks and
  real scheduled weekday stock/daily crypto evidence after deployment.

#### Live State Verification (2026-11-22, 06:10–06:12 UTC)

- The CoinGecko blocker above is resolved: `.env` and `.env.production` (0600,
  Git-ignored) have the key set. Values were not printed.
- The server was already installed when this check began; the install and proxy
  activation were not recorded above and were not performed in this session.
  Release `79623e5c930fd942`, image `sha256:bc1f40bbbd5f...`, container
  `allstocks-database-app-1` started `2026-11-22T00:16:50Z`, healthy,
  `unless-stopped`, no published ports, 164.7 MiB of its 512 MiB limit.
- Data: stocks 9,695 rows, crypto 18,311 rows; all three SQLite files report
  integrity `ok`. Pre-import backups are in `bootstrap-backups/20261122T001633`.
- **Crypto automatic run evidenced:** scheduled start `02:00:00Z`, finish
  `02:11:28Z` on 2026-11-22; 17,373 coins, 200 backfilled, 16,778 backfill pending,
  0 failed, 271 calls. Next due `2026-11-23T02:00:00Z`.
- **Stock automatic run not yet evidenced:** state `never`; 2026-11-22 is a Sunday.
  First due `2026-11-23T14:45:00Z`.
- Active Caddy config routes `stocks.skyboyz1.duckdns.org` with authentication to
  `allstocks-database-app:8000`; the Let's Encrypt certificate verifies. Without
  credentials, `/`, `/crypto` and `/health` return 401. Port 8000 has no host
  listener and an external connection attempt did not connect.
- All five TradingSupport containers keep their baseline IDs and start times.
  Available RAM 1,105,708 KiB versus 1,236,224 KiB at baseline.
- **Open risk:** local and server Caddyfiles match (SHA-256 `af8f6e8e...`), but the
  local TradingSupport change is uncommitted. Its update workflow ships committed
  files and refuses a dirty tree, so the route must be committed by the owner.
- Open risk resolved: owner-requested TradingSupport commit `0d8b5de` on local
  `dev` contains only the route. Its Caddyfile hash matches the server (`af8f6e8e...`).
  It was not pushed, and no TradingSupport update was run.
- Owner reported on 2026-11-22 that login works on the live host, and supplied a
  Stocks view screenshot showing the expected pre-first-run status: next run
  `2026-11-23T14:45:00+00:00`, last attempt never, stale-data warning. Not separately
  confirmed: that the Crypto view loads, and live sync-form submission
  (deliberately not exercised before the first scheduled stock run).
  Gunicorn logs a nonfatal control-server error at each startup
  (`Permission denied: '/home/app'`); the app stayed healthy.
- Final comparison (2026-11-22T14:43Z, release `c9bad96e9fd67e20`): 1,134,356 KiB available RAM
  (baseline 1,236,224), 1,893,072 KiB free swap, 34,926,284,800 bytes free disk, app 127.1 MiB
  of 512 MiB. All five TradingSupport services are running with their baseline container IDs.
  No automatic code-deployment timer or cron entry exists for this app.
- **App-only restart passed (owner-approved, 2026-11-22T06:12Z).** No job was
  running. `docker restart` of `allstocks-database-app-1` returned to healthy on the
  same image, started `06:12:24Z`. Row counts, integrity, refresh status and next
  due times were identical before and after. Docker is enabled at boot and the
  restart policy is `unless-stopped`; the shared server was not rebooted. All five
  TradingSupport containers kept their IDs and start times. Port 8000 still has no
  host listener, and the public route still returns 401 without credentials.
