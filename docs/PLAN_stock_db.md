# Implementation Plan: `stock-db` Truthful Daily Stock Database

## 1. Goal

Fix the existing stock database and turn it into a self-updating daily pipeline, using
only free sources, so that:

1. every stock and ETF tradable on Trading 212 is in the database;
2. it updates automatically every day and keeps working when one source fails or hits
   its limit;
3. the data is truthful and legitimate. **A missing value is better than a wrong value.**

It runs on the owner's Oracle Cloud ARM server, beside the other bots, as its own service.

## 2. Non-Goals

- No paid provider. EODHD EOD All World is only documented as a possible future upgrade.
- No duplicate provider accounts, key rotation, proxies or IP rotation to evade limits.
- No publication of raw data. Data is for the owner's personal use.
- No crypto pipeline changes.
- No replacement of live values before the owner approves the rebuild review.

## 3. Constraints

- **Truth before completeness.** Never write a value that failed validation. Keep the
  last good value, mark the row and report it.
- **Free and legitimate.** Exactly one free account per provider. Respect terms, rate
  limits and `Retry-After`.
- **Real traded prices.** Extremes use split-adjusted, **not** dividend-adjusted daily
  OHLC. Never use `Adj Close` or `auto_adjust=True` for extremes.
- **Store the evidence.** Keep daily bars per source. Every derived value must be
  recomputable, and every row must state its source(s) and time.
- **Full precision.** 8-byte REAL, `DOUBLE PRECISION` or `NUMERIC`; never float32.
- **Check current docs first.** Read each provider's official docs and terms before coding
  it. Record verified limits in `SOURCES.md`.
- **Secrets** live only in Git-ignored env files, with `.env.example` provided.
- Code, comments, logs and commit messages are in English.
- Each phase ends with green tests, a short summary and owner approval before the next.
- Keep simple, reuse existing code and make no unrelated changes. No commit or push
  unless the owner asks.

## 4. Current Understanding

### Verified on 2026-11-22

- Stock data is in SQLite, written by `sync.py` and served by `app.py`. The in-app scheduler
  runs on weekdays at 14:45 UTC.
- `sync.py:212` calls `yf.Ticker(...).history(period="max", actions=False)` with
  yfinance's default adjustment. This is the likely cause of negative lows; Phase 0 must
  prove it.
- The 2026-11-22 export has 9,695 rows: 309 with `all_time_min <= 0` (277 negative, 244 on
  Frankfurt), 471 with ATH/ATL above 10,000, and 3 without year values.
- The reported "1 row without a Yahoo symbol" is **not** present in that export.
- `ACIC`, `GOLD`, `CNX`, `CR`, `ALAIR.PA`, `SEV` and `UA` are each shared by more than one
  instrument, consistent with the reported wrong mappings.
- The server has 1 CPU, 1.9 GB RAM and 33 GB free disk, shared with TradingSupport. The
  dashboard container is limited to 512 MiB and 0.5 CPU.
- `fix_stock_extremes.py` was not found locally or on the server. Build from this spec.

### Findings Phase 0 must confirm or refute

- Data stops at the Friday 2026-11-13 close; about 21% of sampled rows had a new 2026
  low/high a week later.
- Extremes use dividend-adjusted prices (184 of 185 sampled rows where it matters),
  including lows that never traded (ROST 0.083) and values that drift after dividends.
- Seven wrong Yahoo symbols: Archer Aviation ACIC→ACHR, Barrick Mining GOLD→B, Core Natural
  Resources CNX→CNR, Crane NXT CR→CXT, Mexedia ALAIR.PA→ALMEX.PA, Sono Group SEV→SSM,
  Under Armour Class A UA→UAA.
- Values were not updated after later splits (LOBO is 10× too low after a reverse split).
- Float32 storage artifacts such as `0.0010000000474974513`.
- "Year" means calendar year to date, not 52 weeks. Keep this definition.

### Unknown — inspect before implementation

Current provider limits and terms, whether Yahoo's terms satisfy the "legitimate" rule,
stored-bar size, rebuild duration on the shared host, the existing notifier target and
each exchange's OpenFIGI `exchCode` and quote currency.

## 5. Areas to Inspect

`sync.py`, `app.py`, `refresh.py`, `compose*.yaml`, `Dockerfile`, `deploy/manage.py`,
`deploy/release.py`, `deploy/data.py`, `tests/`, the live stock DB (read-only) and
TradingSupport notifier code (read-only).

## 6. Simplest Implementation Strategy

### Runtime

Use a separate container and database in this repository, reusing the release tooling.
The dashboard switches to verified data only after the owner approves the rebuild review.
Only then is the old stock scheduler retired.

### Code

New package `stockdb/`: `schema`, `universe`, `mapping`, `providers/`, `quota`, `validate`,
`extremes`, `pipeline`, `export`, `cli`. Add `overrides.yaml` only with a safe YAML parser;
add no other dependency without a reason recorded in its phase.

Reuse `sync.fetch_instruments`, `to_yahoo`, `EXCHANGE_COUNTRIES`, the `_Yahoo` backoff
pattern, `refresh.py` locking/status patterns, `deploy/data.py` SQLite backups and the
existing `unittest` style. Do not change `sync.py` behavior before cutover.

### Sources (limits must be verified)

| Role | Source | Stated free limit | Use |
| --- | --- | --- | --- |
| Universe | Trading 212 `/equity/metadata/instruments`, `/exchanges` | 1 / 50 s; exchanges 1 / 30 s | Daily instruments |
| ISIN mapping | OpenFIGI `/v3/mapping` | 25 requests / 6 s, 100 jobs each | New/changed instruments |
| Main prices | Yahoo chart endpoint (unofficial) | Unpublished; at most 2–3 req/s with backoff | Bars, history, events |
| US check | Massive grouped daily | 5 calls/min, 2 years, available after midnight ET | Every US row daily |
| US backup/history | Tiingo EOD | 50/hour, 1,000/day | Fallback, slow verification |
| Non-US spot checks | Alpha Vantage `TIME_SERIES_DAILY` raw | 25/day | Random EU/CA sample |
| Optional US backup | Twelve Data Basic | 800/day, US only | Only if Tiingo is down |

Providers implement one interface: `fetch_daily(symbols, date)`, `fetch_history(symbol)`,
`quota_left()`, and can be enabled by configuration.

### Data model

SQLite WAL at `/data/stock_db.sqlite`:

- `instruments`: t212_ticker (PK), isin, name, type, currency, exchange, added_on,
  first_seen, last_seen_in_t212, status (`ACTIVE`, `NEW`, `REMOVED_FROM_T212`, `NO_DATA`).
- `symbol_map`: t212_ticker → yahoo/massive/tiingo/alphavantage symbols, method
  (`OPENFIGI`, `MANUAL_OVERRIDE`, `NAME_SEARCH`), confidence, validated_at, provider long
  name, name_score.
- `daily_bars`: (t212_ticker, date, source) → open, high, low, close, volume, currency,
  split_adjusted_as_of.
- `corporate_actions`: splits and informational dividends per symbol, date, ratio, source.
- `extremes`: all-time and year min/max with dates, last_close, data_through,
  primary_source, confirmed_by, verification (`TWO_SOURCES`, `SINGLE_SOURCE`,
  `QUARANTINED`), quality_flags.
- `quarantine`: rejected values, reason, both sources' numbers, date; never auto-deleted.
- `source_usage`: calls, errors, 429s and breaker state per provider per day.
- `runs`: start/end, step results, counts and warnings.
- A compatibility view exposes the old export columns.

### Mapping rules

Key by ISIN + Trading 212 exchange, never ticker text alone. New or changed instruments go
through OpenFIGI, then provider-symbol construction. Before use, validate normalized name
similarity (at least 0.5), currency, exchange and that no other ISIN uses the symbol.
Failures become `confidence=LOW`, are excluded from extremes and are reported. Overrides
are preloaded with the 7 corrections and still pass validation. Re-validate monthly and
immediately on a provider name change.

### Validation rules

- Reject low/high/close `<= 0`, low > high, close outside [low, high] beyond a tiny
  tolerance, or a missing close.
- **Bad ticks:** low below 20% of min(open, close, previous close), or high above 5×
  max(same): repair the wick to the body, flag it and quarantine the original.
- **US agreement:** Yahoo vs Massive close within 1%, high/low within 2% → `TWO_SOURCES`.
  Otherwise use Tiingo as tie-breaker; if unresolved → `QUARANTINED`, keep the last good value.
- **Non-US:** `SINGLE_SOURCE` unless confirmed by an Alpha Vantage spot check.
- **Jumps:** close moves above 50% with no split: keep, flag `CHECK_JUMP`, verify when
  budget allows.
- **Splits:** providers must agree on date and ratio before history is re-adjusted. Then
  refetch full history and recompute extremes.
- `year_min >= all_time_min`, `year_max <= all_time_max`, and `data_through` must be a
  trading day of that exchange.
- ATH/ATL above 10,000 is allowed but flagged `EXTREME_RANGE`.

### Fallback and quota

Persist calls per day/hour/minute and stop before the limit, with a 10% margin. US order:
Yahoo, Massive, Tiingo, Twelve Data. Non-US order: Yahoo, Alpha Vantage if budget remains,
otherwise keep the last value and mark `STALE`. After N consecutive failures or any 401/403,
skip that provider for the run and report it. One failed source never stops the whole run.

### Daily pipeline (about 06:30 Europe/Berlin)

1. Lock and record a `runs` row.
2. Update the universe; mark new, removed, renamed and re-listed instruments.
3. Map new or changed instruments.
4. Backfill new instruments and new splits within a budget, so the normal update finishes.
5. Fetch every missed trading day since `data_through`; US rows also from Massive.
6. Validate, write bars and recompute extremes incrementally (full recompute only after a
   split or corrected bar).
7. Run fallback for failed symbols.
8. Spend remaining Tiingo/Alpha Vantage budget on spot checks, prioritizing never-checked and
   changed rows.
9. Write the report and backup.

The pipeline must be idempotent and restart-safe at every step.

### Outputs and operations

- Daily `stocks_export_<date>.csv`: old columns plus currency, all_time_min_date,
  all_time_max_date, year_min_date, year_max_date, last_close, data_through, primary_source,
  confirmed_by, verification, quality_flags, isin.
- Daily Markdown report and short notifier message (email if no notifier exists): totals,
  new/removed, rows updated, verification and stale counts, calls per provider, providers
  down and top quarantined rows.
- Alert when a run fails or more than 5% of rows are stale.
- Backup after every successful run, keeping 14 daily + 8 weekly.
- Health command: last successful run, `data_through` distribution, quota left per provider.
- Configuration uses `SDB_` variables: `DB_PATH`, `T212_API_KEY`, `T212_API_SECRET`,
  `T212_BASE`, `OPENFIGI_API_KEY`, `MASSIVE_API_KEY`, `TIINGO_API_KEY`, `ALPHAVANTAGE_API_KEY`,
  optional `TWELVEDATA_API_KEY`, `RUN_TIME`, `TIMEZONE_USER`, `NOTIFIER_TARGET`. A missing
  required key gives one clear error naming the variable.
- Documentation: `README.md`, `SOURCES.md`, `AUDIT.md`, `REBUILD_REPORT.md`, `DATA_QUALITY.md`.

### Definition of done

- Every Trading 212 instrument is in the DB or reported with why it has no data.
- No value `<= 0`, no dividend-adjusted extremes, no `confidence=LOW` mapping used for
  extremes, and the 7 known wrong mappings are fixed.
- Every active US row with data in Massive's 2-year window is `TWO_SOURCES`, or is listed
  with a reason (`QUARANTINED`, `STALE`, no second-source data).

## 7. Security Considerations

Secrets stay in ignored env files, with no key in logs, reports or fixtures. Provider data is
untrusted: use parameterized SQL, escape CSV formulas and validate symbols before building
URLs. Add no public control route or Docker socket in the app. Raw data and exports stay
private; `docs/stocks_export_2026-11-22.csv` should not be committed if data must stay
unpublished.

## 8. Implementation Phases

### Phase 0: Inspect the project and audit current data

**Status:** Not Started
**Depends on:** None

#### Goal

Produce evidence-based `AUDIT.md` and draft `SOURCES.md`, the input for later phases.

#### Scope

Read-only inspection of code and data, plus those two documents.

#### Do Not Change

Application code, databases, schedules or the server.

#### Reuse First

Existing DB, export and `sync.py` mapping tables.

#### Steps

1. Inspect engine, schema, row writes, Yahoo calls, mapping, status, scheduling and deployment.
2. Confirm or refute every section 4 finding with counts and example rows.
3. Map each exchange to Yahoo suffix, OpenFIGI `exchCode` and currency, including GBp.
4. Read current provider docs and terms. If Yahoo's terms conflict with the "legitimate"
   rule, stop for an owner decision.
5. Estimate stored-bar size and rebuild time from a small sample; propose resource limits.

#### Acceptance Criteria

- Every finding is marked confirmed or refuted, with evidence.
- The exchange table covers every exchange in the DB.
- Provider limits and terms are recorded with source links and check dates.
- Sizing and proposed limits are recorded.

#### Verification

Rerun the audit queries against a read-only connection and confirm matching counts. Confirm
`git diff` contains only the new documents.

#### Security Check

Confirm no secret, key or credential appears in either document or sampled output.

### Phase 1: Build schema, Trading 212 universe and validated mapping

**Status:** Not Started
**Depends on:** Phase 0

#### Goal

Store every Trading 212 instrument with a validated provider mapping.

#### Scope

`stockdb` schema, universe, mapping, overrides, config validation and tests.

#### Do Not Change

Existing `stocks` data, `sync.py` behavior, dashboard or schedules.

#### Reuse First

`sync.fetch_instruments`, `to_yahoo`, `EXCHANGE_COUNTRIES` and existing test style.

#### Steps

1. Create the schema with 8-byte numeric storage and WAL.
2. Implement universe updates and OpenFIGI mapping keyed by ISIN + exchange.
3. Implement validation and preload the 7 overrides.
4. Add recorded-fixture and regression tests.

#### Acceptance Criteria

- Every fixture instrument is stored or reported with a reason.
- All 7 known mappings resolve correctly through validation.
- A failed mapping becomes `LOW` and cannot be selected for extremes.
- The old database is byte-identical after a run.

#### Verification

Run the full `unittest` suite in an isolated container with dummy keys and no network.

#### Security Check

Confirm parameterized SQL, safe YAML loading, and no credentials in logs or fixtures.

### Phase 2: Add providers, quota manager and circuit breakers

**Status:** Not Started
**Depends on:** Phase 1

#### Goal

Fetch from each free provider safely behind one interface.

#### Scope

Yahoo, Massive, Tiingo and Alpha Vantage plug-ins, optional Twelve Data, quota and breakers.

#### Do Not Change

Live data, the old sync and provider account arrangements.

#### Reuse First

The `_Yahoo` pacing/backoff pattern and `requests`.

#### Steps

1. Reconfirm each provider's docs before coding it.
2. Implement parsers using unadjusted or split-only prices as required.
3. Persist usage, honor `Retry-After` and keep the 10% margin.
4. Add breakers, fallback order and fixture tests.

#### Acceptance Criteria

- No request is sent once a provider's safety limit is reached.
- A 401/403 disables that provider for the run while others continue.
- Yahoo parsing never uses dividend-adjusted values for extremes.

#### Verification

Run fixture tests with no live calls, then the full suite.

#### Security Check

Confirm fixed HTTPS hosts, encoded symbols and no key in URLs written to logs or errors.

### Phase 3: Validate data and produce the reviewed rebuild

**Status:** Not Started
**Depends on:** Phase 2

#### Goal

Build correct values and let the owner review changes before anything replaces old values.

#### Scope

Validation, quarantine, extremes, resumable rebuild, `REBUILD_REPORT.md`, `DATA_QUALITY.md`.

#### Do Not Change

Live dashboard values and the old stock table.

#### Reuse First

`deploy/data.py` backups and Phase 1–2 modules.

#### Steps

1. Implement validation rules and extremes with dates.
2. Fetch Yahoo history within limits, resumably.
3. Load 2 years of Massive grouped daily bars and cross-check US rows.
4. Start the slow Tiingo verification and track progress.
5. Report old vs new values, change reasons and quarantined rows.

#### Acceptance Criteria

- Regression tests pass for XEB.F, TBT.F, UA/UAA, GOLD/B, LOBO, float precision, year
  definition, bad-tick wick and Yahoo/Massive disagreement.
- No stored extreme is `<= 0`.
- The rebuild resumes after interruption without duplicate bars.
- **The owner approves the changed rows** before any replacement.

#### Verification

Run the full suite, sample-recompute extremes from stored bars and compare against the report.

#### Security Check

Confirm reports contain no secrets and CSV output escapes formula text.

### Phase 4: Deploy the daily pipeline and switch to verified data

**Status:** Not Started
**Depends on:** Phase 3. Retiring the old scheduler also requires Phase 5's stock-run evidence.

#### Goal

Run the verified pipeline automatically every day on the server.

#### Scope

Pipeline, catch-up, exports, reports, alerts, backups, health command, container deployment,
dashboard switch and old stock scheduler retirement.

#### Do Not Change

Crypto schedule, TradingSupport services or data, global Docker or SSH settings.

#### Reuse First

Existing Compose, deployment scripts, release allowlist and `refresh.py` patterns.

#### Steps

1. Implement the daily pipeline, outputs and alerts.
2. Choose scheduling from Phase 0 findings. Confirm 06:30 Berlin remains after Massive's
   availability across daylight-saving changes.
3. Deploy as a non-root ARM container with measured limits and rotated logs.
4. After owner approval, switch the dashboard and retire the old stock scheduler.

#### Acceptance Criteria

- Two real daily runs complete and report accurately.
- Re-running a day changes nothing.
- A simulated provider outage still finishes and reports.
- The section 6 definition of done holds.
- TradingSupport containers keep their IDs and start times.

#### Verification

Run the fixture end-to-end test (US, EU, new listing, removed instrument, one provider down),
`tests.check_deploy`, `tests/check_production.py`, the update dry run and live read-only checks.

#### Security Check

Confirm non-root runtime, no published ports, private backups and exports, and no Docker socket.

### Phase 5: Finish the migrated TradingSupport deployment verification

**Status:** Partial — deployed; Remaining: scheduled refresh evidence (stocks), confirmation that the Crypto view loads after login
**Depends on:** Deployment Phases 1–3 in `docs/done/PLAN_deployment.md` (`Done — Passed Review`)

Migrated unchanged from Deployment Phase 4 on 2026-11-22. It is last in this plan but may be
completed independently, because the first scheduled stock run is 2026-11-23 at 14:45 UTC.
That run proves automation only; it will still write the known-bad adjusted values.

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
- **App-only restart passed (owner-approved, 2026-11-22T06:12Z).** No job was
  running. `docker restart` of `allstocks-database-app-1` returned to healthy on the
  same image, started `06:12:24Z`. Row counts, integrity, refresh status and next
  due times were identical before and after. Docker is enabled at boot and the
  restart policy is `unless-stopped`; the shared server was not rebooted. All five
  TradingSupport containers kept their IDs and start times. Port 8000 still has no
  host listener, and the public route still returns 401 without credentials.
