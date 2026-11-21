# Implementation Plan: TradingSupport Server Deployment

## 1. Goal

Run this application's Stocks and Crypto dashboard continuously on the existing
TradingSupport server, using its installed Docker engine. Provide the same operator
workflow as the crypto alert bot: shell scripts for initial setup and manual code
updates over SSH, with tests, a dry run, health checks and a recoverable release.

**Automatic stock-data updates must be active on the server.** The existing stock
scheduler must refresh data every weekday at the server installation's configured
`SYNC_TIME_UTC`, without a browser session or the developer's computer running.
Preserve the existing daily crypto-data schedule too.

Owner clarification, 2026-11-21: automatic update means stock-data refresh, **not
automatic application deployment**. Code releases remain explicitly operator-run.

## 2. Non-Goals

- No automatic code deployment, Git polling, image auto-updater or CI/CD service.
- No changes to TradingSupport's trading engine, database, credentials or jobs.
- No deployment to Market Analyser's server or the crypto bot's server.
- No rewrite of the application, SQLite migration to PostgreSQL, additional data
  provider, intraday streaming or changes to stock/ETF/ISIN calculations.
- No second stock scheduler, cron entry or systemd timer alongside the scheduler
  already in `app.py`.
- No implementation, SSH deployment or server changes during this planning task.

## 3. Constraints

- Choose the simplest safe implementation; reuse existing code, Compose and test
  patterns. Add no unnecessary dependency, release framework or service.
- Keep local use through `compose.yaml` working. Preserve one Gunicorn worker,
  four threads and no `--preload`: scheduling and sync locks are process-local.
- Give this application its own installation directory, stable Compose project
  identity, named data volume, credentials and image/release identity.
- Do not reinstall Docker, modify SSH hardening, change swap/timezone, prune shared
  Docker resources or restart the TradingSupport stack as part of app updates.
- Never interrupt an active stock or crypto sync for an update. Both can take
  longer than a deployment timeout; an update must defer safely in that case.
- Preserve the server's `.env` by default. Any environment replacement must be an
  explicit operator option, scoped to this application.
- Test candidates without production credentials or the live data volume. Importing
  `app.py` starts scheduler threads, including in a test process.
- No commit, push or discard of user changes. Execute exactly one requested phase
  at a time. Dependencies require `Done — Passed Review`.

## 4. Current Understanding

### Verified from local files

- `Dockerfile` runs the app as UID 10001 using Gunicorn; database paths are
  `/data/trading212_stocks.db` and `/data/crypto.db`.
- `compose.yaml` provides a persistent `data` volume, `restart: unless-stopped`,
  loopback-only `127.0.0.1:8000:8000` and a `/health` health check.
- `app.py` starts two scheduler threads on import. Stock updates run on weekdays
  at `SYNC_TIME_UTC` (default `14:45` UTC); crypto updates run daily at
  `CRYPTO_SYNC_TIME_UTC` (default `02:00` UTC). These are stored market-data updates,
  not real-time price feeds.
- `run_sync_job` and `run_crypto_sync_job` use separate non-blocking locks and log
  starts, final summary counts and failures. Manual POST routes reuse those jobs.
- Last summaries live only in memory. `/health` always returns `ok`; it does not
  prove scheduler activity, provider access or a completed data refresh.
- The scheduler calculates the next future run on startup; it does not catch up a
  missed schedule. Deployment must not silently claim a missed refresh succeeded.
- The app has no login or CSRF protection on its two sync forms. Its documented
  supported deployment is currently loopback-only. Protected public access is a
  prerequisite of this feature; current public exposure has not been established.
- `tests/` uses standard-library `unittest`. The documented suite is
  `docker compose run --rm app python -m unittest`.
- The crypto alert bot's `deploy/setup.sh`, `deploy/update.sh` and
  `deploy/ROLLOUT.md` provide useful workflow references: private staged releases,
  local/server tests, dry runs, preserving environment/state and rollback artifacts.
  Its systemd Python installation is not this app's runtime architecture.
- Local TradingSupport files at `/home/blasius/personal/video/tradingSupport`
  define a separate Compose project, `trading-assistant`. Its optional public
  profile uses Caddy on ports 80/443, HTTPS and hashed-password Basic Auth. Its
  `deploy.sh`, `update.sh` and `remote.sh` demonstrate the existing SSH workflow.
  Do not run those scripts to deploy this application: they affect other services.
- Local deployment checks earlier in this conversation found no containers for
  this project's Compose installation and no listener on local port 8000. This
  does not establish the state of the TradingSupport server.
- `docs/todo/PLAN_crypto.md` Phase 5 is still `Partial`; its remaining live-run
  evidence must not be marked complete by this deployment plan. Do not change its
  implementation or status here. Record any release-blocking finding separately.

### Unknown — inspect before implementation

- Exact TradingSupport SSH destination, user, existing private-key location,
  installation permissions, OS/CPU architecture and available RAM/CPU/disk.
- Live Docker/Compose versions, Docker boot enablement, occupied ports, actual
  Caddy/proxy configuration and running services. Local files are references,
  not proof of the server's current configuration.
- Dashboard hostname, DNS, certificate readiness, intended login credentials and
  the authoritative source of the shared proxy configuration.
- Whether an existing database should be migrated or the server should start with
  a fresh sync; actual source volume/path, size and database schema.
- Validity and permitted use of this app's server API credentials, configured
  schedule and remaining provider quota. Never print their values.
- Exact network attachment/upstream needed for the existing proxy to reach this
  app without reaching unrelated databases or exposing port 8000 publicly.

Resolve deployment-specific unknowns with read-only inspection first. If required
credentials, a hostname or access are unavailable, record the precise blocker;
do not guess or broaden the deployment to another server.

## 5. Areas to Inspect

- `compose.yaml`, `Dockerfile`, `.dockerignore`, `.gitignore`, `.env.example`,
  `requirements.txt` and `README.md`.
- `app.py`: scheduler startup, next-run calculation, both job wrappers, sync
  routes, request handling and health behavior.
- `sync.py`, `crypto_sync.py`, `templates/index.html` and existing `tests/`.
- The referenced crypto-bot deployment scripts and TradingSupport proxy/SSH
  configuration, only to reuse relevant operational patterns.
- During execution, read-only server inventory and the actual shared ingress
  configuration before selecting an app-specific production override.

## 6. Simplest Implementation Strategy

Keep a separate Compose installation on TradingSupport. Reuse its existing Docker
daemon and HTTPS ingress, with an app-specific hostname and authentication. Do not
start another proxy competing for ports 80/443. Keep the app upstream private.
If the live ingress differs from local Caddy files, inspect it before implementation.

Proposed new deployment entry points, to be implemented in this repository:

- `deploy/setup.sh`: first-install preflight and setup for this application only.
- `deploy/update.sh`: local tests, private packaging, SSH staging, candidate tests,
  safe cutover, health verification and recovery; `--dry-run` makes no SSH connection.
- `deploy/server.env.example`: non-secret configuration template; the real
  `deploy/server.env` is ignored and private. SSH keys stay outside the repository.
- `deploy/ROLLOUT.md`: exact installation, data migration, update, rollback,
  refresh-status and troubleshooting commands after server inspection.

Use a production Compose override or small existing-file changes only where needed
for stable project identity, release images, private proxy access, resource limits
and bounded log rotation. Exact additional filenames are determined in Phase 1.
Extend ignore rules so deployment credentials and backups cannot enter an image,
release archive or Git.

Reuse the existing stock/crypto scheduler. Add only enough persistent, secret-free
status to show configured next runs, last attempts, completions and failures after
restarts, and enough coordination for deployment to drain sync jobs safely. Reuse
SQLite and standard-library facilities rather than adding a job queue or service.
Keep `/health` as liveness; evaluate refresh success separately.

Use one release path for initial setup and later updates, with stable persistent
storage across staging directory changes. Retain a known-good image/config and
consistent database backups before cutover. Rollback normally restores code/config,
not an old database over newer market data. Incompatible database changes must
stop before cutover and require a reviewed migration/recovery procedure.

## 7. Security Considerations

- All public dashboard and sync routes require authentication over HTTPS; private
  health or maintenance endpoints must not create an alternate public access path.
  Use hashed proxy passwords and avoid exposing an unauthenticated upstream.
- Authenticated browsers can still submit cross-site requests. Add CSRF protection
  to manual-sync forms, validate allowed hosts/origins and trust forwarded headers
  only from the inspected proxy boundary. Retain template escaping.
- Preserve SSH host verification. Parse deployment settings as data; validate
  target paths and identifiers; quote arguments and refuse unsafe/symlinked paths.
- Release archives have an explicit payload, private staging and checksums.
  Secrets, databases, logs, `.git` and local environments are excluded. Checksums
  detect corruption; SSH authenticates the transport.
- Runtime environment files and backups remain private; no shell tracing of
  secrets, public status secrets, credentials in command output or Docker socket
  mounted into the application. Application credentials must be separate from
  TradingSupport and grant only necessary provider access.
- Use measured resource limits and bounded build/log/archive storage to protect
  the shared server. Cleanup applies only to this application's named artifacts.
- No security blocker has been confirmed against the live server because it has
  not been inspected. Apply the skill's Security Gate during each execution/review.

Primary references checked during planning:
[Compose production deployment](https://docs.docker.com/compose/how-tos/production/),
[Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/),
[Caddy Basic Auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth),
[Flask web security](https://flask.palletsprojects.com/en/stable/web-security/).

## 8. Implementation Phases

### Phase 1: Prepare isolated production configuration and protected access

**Status:** Done — Passed Review
**Depends on:** None

#### Goal

Produce a tested configuration for this app on TradingSupport with a private
upstream and protected browser access, without making it live yet.

#### Scope

Read-only server discovery; app-specific Compose/proxy configuration; request and
form protections in `app.py`, `templates/index.html` and focused existing tests;
environment/ignore documentation. Proposed production configuration files only
where the inspected setup requires them.

#### Do Not Change

Stock/crypto calculations, provider pacing, database schemas, schedule times,
TradingSupport services or the live proxy during this phase.

#### Reuse First

Existing Compose, non-root image, local Caddy authentication pattern, Flask and
standard-library security facilities, existing route test fixtures.

#### Steps

1. Inspect the actual host, resource headroom, port bindings, proxy ownership,
   network boundary, DNS, data source and credential availability. Record facts
   and remaining unknowns without secret values.
2. Define stable app-only Compose/image/volume identities and a private upstream;
   select measured limits and bounded logs. Avoid replacing existing host setup.
3. Prepare the smallest app-host route for the existing ingress. Establish how
   that route survives TradingSupport updates through its authoritative config;
   identify any required cross-repository edit before making it.
4. Add missing CSRF/host/origin protections and production environment validation.
   Protect sync actions without exposing maintenance controls or breaking local use.
5. Test production configuration and access behavior in an isolated environment.
   Keep live routing unchanged until Phase 4.

#### Acceptance Criteria

- Server facts and an unambiguous app target/upstream are recorded; no guessed
  domain, port or shared-network assumption remains in executable configuration.
- Production Compose renders successfully, retains one worker and persistent
  paths, and publishes no unauthenticated public application port.
- An isolated proxy test requires valid credentials for dashboard and sync routes;
  missing/wrong credentials are rejected. Authenticated valid forms work, while
  missing/invalid CSRF tokens and untrusted origins/hosts are rejected.
- Existing dashboard/filter behavior and local Compose use remain functional.
- No live service or proxy configuration is changed in this phase.

#### Verification

Validate the inspected Compose configuration; run the existing unittest suite in
an isolated image with dummy credentials, temporary data and no provider access.
Run focused route/security tests and an isolated proxy authentication test.
Determine exact server/proxy validation commands during inspection.

#### Security Check

Review upstream bypass, authentication coverage, CSRF, host/forwarded-header trust,
secret handling and project/network/volume isolation before enabling ingress.

#### Execution Evidence (2026-11-21)

- Read-only SSH confirmed `tradingsupport-vnic`, ARM64 Ubuntu 24.04.5,
  Docker 29.1.3, Compose 2.40.3, enabled Docker boot service, existing Caddy ingress
  and `trading-assistant_default`. Host/port/resources and authoritative proxy
  source are recorded in `docs/deployment.md`; no server files or services changed.
- Prepared `compose.production.yaml` with stable app-only project/image/volume,
  no published upstream ports, the inspected existing ingress network and bounded
  resources/logs. Prepared `deploy/Caddyfile.stocks` for
  `stocks.skyboyz1.duckdns.org`, which already resolves to this server, using the
  existing proxy login. This is the proposed default unless the owner chooses
  another hostname/login; no live route or certificate was activated.
- Added signed-session form tokens, trusted hosts, Origin/Referer and cross-site
  checks, secure production cookies, request-size limits and startup validation of
  the public origin/session key. Forwarded headers are not trusted. No new package,
  scheduler change, stock/crypto calculation change or public maintenance endpoint.
- Baseline: 152 unittests passed. Final: **162 unittests passed** in an isolated
  `allstocksdatabase-app:latest` container running current source, dummy keys,
  temporary database paths and `--network none`. Includes 10 new security tests;
  both existing manual-sync success tests now submit a real form token.
- `python3 tests/check_production.py` passed after the final code/template-only
  mount change. It checked the actual production Compose merge and actual proxy
  route in a unique internal Docker network, with no published ports, dummy keys
  and temporary databases. HTTPS verified the test CA; missing/wrong login was
  rejected for both dashboards, health and sync routes; valid authenticated forms
  succeeded and missing-token/foreign-origin forms failed. Test containers/network
  were removed by its scoped cleanup. No production credentials or volumes used.
- Under a 512 MiB cap, isolated rendering of copies of the existing local 9,695
  stock / 18,230 crypto rows returned HTTP 200 and peaked near 180 MiB. This is
  local render evidence, not an ARM full-sync or automatic-refresh verification.
- `git diff --check` passed. Self-check covered secret exclusions, private ingress,
  CSRF/host validation, forwarded-header spoofing, production startup failures,
  oversized forms and app-only resource identities; no security blocker found.
- Remaining rollout prerequisites: the authoritative TradingSupport Caddyfile must
  eventually include the reviewed fragment so its own updates preserve the route;
  that cross-repository/live edit is deferred to Phase 4. Local Trading 212 key/
  secret settings are present but unvalidated; the local `.env` lacks a CoinGecko
  key. Supply/validate private server credentials during setup. No real stock or
  crypto provider call, data migration or server deployment occurred in this phase.
- Submitted for Phase 1 review; Phases 2–4 remain `Not Started`.

#### Review Result (2026-11-21)

- **Done — Passed Review.** Reviewed actual tracked diffs and all new production
  configuration, route, smoke-test and documentation files against the Phase 1
  scope and acceptance criteria. No blocking or optional findings identified.
- Reran the complete unittest suite in the isolated container: **162 tests passed**
  in 4.272 seconds, with dummy credentials, temporary data and no external network.
- Reran `python3 tests/check_production.py`: production Compose merge passed;
  isolated HTTPS with trusted test CA passed; authentication and valid/invalid
  forms for both datasets passed. No live server or production data used.
- `git diff --check` and new-file whitespace checks passed. Confirmed unchanged
  `sync.py`, `crypto_sync.py`, base `compose.yaml` and `Dockerfile`, retaining the
  original schedules, one-worker/four-thread command and `/data` database paths.
- Security review found no blocker in private ingress/authentication coverage,
  token/session handling, host/origin validation, forwarded-header handling,
  request limits, secret exclusions or app-only resource/volume configuration.
- Live provider credentials, authoritative proxy integration, ARM full-sync
  capacity and scheduled server-refresh evidence remain explicitly assigned to
  later phases. This review does not claim deployment or automatic refreshes have
  occurred on the server. No implementation changes made during review.
- Phase 2 is eligible to start when requested. No later phase was executed.

### Phase 2: Make scheduled data refreshes observable and safe during updates

**Status:** Not Started
**Depends on:** Phase 1

#### Goal

Keep automatic stock-data refreshes active and provide reliable evidence of their
outcome, with a safe way for deployment to wait for running jobs.

#### Scope

Existing scheduler/job wrappers, minimal persistent status and deployment
coordination, dashboard status and focused scheduler/concurrency tests. Avoid
changes to price calculations or provider request behavior.

#### Do Not Change

Weekday stock/daily crypto semantics, configured UTC times, one-worker model,
provider budgets or the manual code-deployment requirement.

#### Reuse First

`next_run`, `_scheduler_loop`, both job wrappers and locks, existing summary logs,
SQLite persistence and the existing sync-status display.

#### Steps

1. Inspect thread lifecycle, restart behavior, summaries and existing tests. Reuse
   the wrappers for both scheduled and manual entry points.
2. Persist minimal last-attempt/completion/failure metadata and expose configured
   next runs to the operator; retain status across restarts. Mark an interrupted
   attempt truthfully rather than leaving a permanent running/success state.
3. Add the smallest deployment/sync coordination mechanism shared by all job
   entry points. Drain active jobs and prevent a new manual or scheduled job
   racing with backup/cutover. A check-then-stop alone is insufficient.
4. Define bounded waiting, failure cleanup and schedule resumption. If an update
   crosses a due time, defer the cutover or preserve one pending run; do not
   silently lose that stock refresh or create duplicate quota-consuming runs.
5. Verify locks, both schedules, exception recovery and durable status with mocked
   providers and controlled time. Do not add a second scheduling system.

#### Acceptance Criteria

- Stock updates run at the configured UTC time on weekdays, with weekends handled
  by existing semantics; crypto retains its daily schedule.
- Status distinguishes never run, running, completed, failed/interrupted and next
  due; completion timestamps survive container recreation.
- A stale dataset or failed refresh is visible even when `/health` returns `ok`.
- Scheduled/manual attempts cannot overlap for the same dataset, while stock and
  crypto jobs can still run independently during normal operation.
- Deployment draining cannot interrupt a job or race with a new job; timeout or
  pre-cutover failure leaves the current release running and schedules usable.
- Restarts resume scheduling automatically without browser interaction. A missed
  run is never presented as a completed refresh.

#### Verification

Extend existing unittest fixtures for controlled-time scheduling, manual/scheduled
concurrency, drain races, interrupted runs and restart persistence. Recreate an
isolated container using the same temporary volume and verify status/next runs.
Use fake providers for all automated tests; run the complete unittest suite once
the focused checks pass.

#### Security Check

Keep maintenance controls private; expose no tokens in status/errors; validate
persisted status and coordination files/paths; reject browser attempts to control
deployment. Ensure test scheduler threads cannot use production keys or data.

### Phase 3: Add tested shell setup, manual update and rollback workflows

**Status:** Not Started
**Depends on:** Phase 1, Phase 2

#### Goal

Deliver repeatable operator-run `.sh` deployment commands with candidate testing,
safe data preservation and recovery, matching the crypto bot's workflow.

#### Scope

Proposed `deploy/setup.sh`, `deploy/update.sh`, `deploy/server.env.example`,
`deploy/ROLLOUT.md`; minimal packaging/helper code only if needed; ignore rules,
README instructions and focused deployment tests following existing unittest style.

#### Do Not Change

No live rollout yet. No automatic code deployments, shared-host setup changes,
global pruning, replacement of server secrets by default or TradingSupport restarts.

#### Reuse First

Existing Docker build/test workflow, Phase 1 configuration, Phase 2 coordination,
SSH/release patterns from the crypto bot and SQLite's consistent backup support.

#### Steps

1. Implement a private allowlisted release payload and stable release identity.
   Run local tests without live keys before staging. Make `--dry-run` offline,
   print only artifact paths/identities and validate required payload contents.
2. Implement initial setup checks for the existing Docker host and app-only target.
   Fail on unsafe paths, conflicting projects/ports or insufficient resources.
   Reuse the same candidate/install path for first setup and later updates.
3. Stage over verified SSH, verify checksums and build/test an isolated candidate
   using the target architecture without touching the live volume or secrets.
4. Lock out concurrent deploys, drain syncs through Phase 2, make consistent private
   backups of both databases and capture the actual running image/config for
   rollback. Keep volume identity stable across releases and working directories.
5. Cut over only this app after checks pass. Verify liveness, data readability and
   scheduler readiness; retain original production credentials unless an explicit
   environment option was selected and validated.
6. On failed cutover, restore the previous compatible release and verify recovery;
   fail visibly if recovery fails. First-install failure returns to a safe private
   state. Always release coordination so updates do not leave scheduling disabled.
7. Document initial data import versus fresh bootstrap, manual update, backup,
   rollback, log/status inspection and exact shared-ingress integration steps.

#### Acceptance Criteria

- `bash deploy/update.sh --dry-run` tests and produces a reviewable private release
  without SSH, production keys, live-data writes or a deployment.
- Failed local/candidate tests, corrupted artifacts and unsafe target paths stop
  before changing the current release. Concurrent updates cannot both cut over.
- Artifacts/images contain no real `.env`, SSH key, database, log or server config
  containing credentials; server credentials remain unchanged by default.
- Updates refuse/defer safely while jobs cannot be drained. Backups restore into
  temporary storage with valid SQLite integrity and expected row counts.
- Update/recovery drills retain both databases and the prior working release;
  no stock or crypto job is killed, and schedules remain active afterward.
- Deployment affects only this app. No update automatically starts because of a
  new Git commit or image tag.

#### Verification

Run `bash -n` on new scripts; use focused tests with fake SSH/Docker commands for
failure paths and offline dry-run assertions. Exercise real initial installation,
successful update, interrupted staging, unhealthy candidate, recovery and database
restore in an isolated Docker environment. Run existing unittests on the packaged
candidate with temporary data and dummy credentials.

#### Security Check

Inspect argument quoting, target validation, SSH host verification, archive paths,
symlink handling, secret exclusions/permissions and app-scoped cleanup. Verify
traps/recovery cannot delete unrelated files or leave the maintenance gate active.

### Phase 4: Deploy on TradingSupport and verify automatic stock refreshes

**Status:** Not Started
**Depends on:** Phase 1, Phase 2, Phase 3

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
