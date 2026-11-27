# TradingSupport deployment configuration

Phase 1 prepares production configuration and tests protected access. It does not
install this application, modify DNS or change the live TradingSupport proxy.
The local setup/update tooling and guarded GitHub Actions deployment are documented in
[deploy/ROLLOUT.md](../deploy/ROLLOUT.md).
The actual rollout remains Phase 4 in
[PLAN_deployment.md](done/PLAN_deployment.md).

## Inspected server, 2026-11-21

- Existing SSH alias: `live`; target `ubuntu@92.5.106.5`, hostname
  `tradingsupport-vnic`. The existing key stays at `~/.ssh/live-server.key`.
  Strict SSH host verification is enabled. No new key or credentials were copied.
- Ubuntu 24.04.5, ARM64 (`aarch64`), Docker 29.1.3, Compose 2.40.3.
  The Docker service is enabled at boot.
- RAM: 1,951 MiB total, about 1,203 MiB available during inspection. Swap: 2,047
  MiB total. Root filesystem: 45 GiB, about 34 GiB available.
- Existing `trading-assistant` services: engine, API, web, PostgreSQL and Caddy.
  Web binds `127.0.0.1:3000`; Caddy owns public TCP 80/443. No port 8000 listener
  and no `~/allstocksDatabase` installation were present.
- Existing HTTPS hostname: `skyboyz1.duckdns.org`. The prepared app hostname,
  `stocks.skyboyz1.duckdns.org`, already resolves to `92.5.106.5`. This is a
  proposed default, pending an alternative from the owner; DNS resolution alone
  does not prove certificate issuance or live access for the new hostname.
- Caddy runs as `trading-assistant-caddy-1`, image `caddy:2.10.2-alpine`, on
  `trading-assistant_default`. That network also contains the web and engine;
  the API/database's private networks are separate. Existing login variables are
  configured; their values were neither printed nor copied.
- The live Caddyfile is a read-only bind mount from
  `/home/ubuntu/tradingSupport/docker/caddy/Caddyfile`. Live Compose configuration
  is `/home/ubuntu/tradingSupport/compose.yml`.

These are observations, not permanent assumptions. Setup must recheck identity,
ports, networks, headroom, DNS and existing service health before any rollout.

## App-only production configuration

Merge `compose.production.yaml` after `compose.yaml`. It sets the stable project
name `allstocks-database`, image `allstocks-database:APP_RELEASE`, data volume
`allstocks-database_data` and network alias `allstocks-database-app`. Release
directories must never change that volume identity. The deployment tool pins this
project name explicitly; do not substitute another operator-supplied `-p` or
`COMPOSE_PROJECT_NAME` value.

The production override removes all published application ports using `!reset`
(Compose >= 2.24) and attaches the app to the inspected existing
`trading-assistant_default` network. Caddy can reach
`allstocks-database-app:8000`; no new proxy or public port is introduced, and the
app is not attached to the database/private frontend networks. Local Compose
without the override retains loopback port 8000.

The app retains its non-root UID 10001, one Gunicorn worker, four threads, Docker
health check and `restart: unless-stopped`. The override caps memory/swap at
512 MiB, CPU at 0.5 cores, processes/threads at 128 and JSON logs at three 10 MiB
files; it drops Linux capabilities and disallows privilege escalation. A local
isolated render using copies of the existing 9,695 stock and 18,230 crypto rows
peaked around 180 MiB, under the 512 MiB limit. This is not full-sync ARM resource
evidence: measure real provider syncs on the server during rollout before claiming
that its capacity is sufficient for continuous operation.

The image continues to set both database paths under `/data`. The existing local
volume `allstocksdatabase_data` is separate from the proposed server volume. It
held approximately 1.3 MB of stock data and 2.7 MB of crypto data when inspected;
the most recent row timestamps were 2026-11-15. Migration versus a fresh bootstrap
is still a rollout decision. No database has been moved or changed by Phase 1.

Required server environment settings, in this app's private `.env` only:

- `APP_RELEASE`: set by the deployment tool, not included in the input environment file.
- `APP_PUBLIC_ORIGIN=https://stocks.skyboyz1.duckdns.org`: exact HTTPS origin,
  with no path, credentials, explicit port or trailing slash.
- `APP_SECRET_KEY`: at least 32 randomly generated characters, kept stable across
  updates. Generate privately during setup; do not reuse another application's
  secret. A public origin with a missing/short key fails startup.
- This app's existing Trading 212 and CoinGecko settings. Credentials remain
  private and are checked during setup, not sent to providers during Phase 1 tests.

The inspected local `.env` contains Trading 212 key/secret settings but no
`COINGECKO_API_KEY` entry. No app environment exists on the server yet. Supply the
private CoinGecko key and validate provider access during setup before expecting
automatic crypto refreshes. Stock-provider validity and API quotas also remain
unverified; Phase 1 deliberately made no provider requests.

Without a public origin, local use generates a temporary session-signing key.
Forms open before a local restart must be reloaded. Local access remains on
loopback and is not a public authentication mode.

## Existing proxy integration, deferred to rollout

`deploy/Caddyfile.stocks` is the prepared route fragment for the existing Caddy
instance. It reuses that proxy's existing hashed-password login variables and
protects every app path, including `/health` and both sync routes. It forwards the
original Host to the private app alias. It is not a standalone Caddy service.

The authoritative source used by TradingSupport updates is currently
`/home/blasius/personal/video/tradingSupport/docker/caddy/Caddyfile`. Its
`update.sh` ships committed files using `git archive`; the live server has no Git
checkout. A server-only Caddyfile edit would be lost on the next TradingSupport
update. Before enabling this route in Phase 4, the reviewed fragment must also be
included in that authoritative source and in the code version its operator
deploys. This requires a narrowly scoped cross-repository change at rollout;
neither repository's shared proxy file has been changed in Phase 1. Do not silently
edit only the server or restart the entire stack.

At rollout, back up the actual proxy configuration, preserve its existing route,
validate the combined configuration in its existing environment, and reload only
Caddy. Verify both hostnames and keep a recovery copy. No certificate for the new
hostname has been requested during Phase 1.

The app validates Host using Flask's trusted-host support. Only loopback names and
the configured public hostname are accepted. It deliberately does not install
`ProxyFix` or trust forwarded headers: public-origin and secure-cookie settings
come from trusted server configuration. Both sync forms use a signed-session CSRF
token; wrong/missing tokens, cross-site fetches and foreign Origin/Referer headers
are rejected before a background job starts. Production cookies are Secure,
HttpOnly, SameSite=Strict and use a host-only `__Host-` cookie name. Response caching
is disabled; template escaping remains enabled. Authentication remains at Caddy;
the private upstream must never be made publicly reachable.

## Verification

The host-only integration check is:

```sh
python3 tests/check_production.py
```

It requires local Docker access and the cached images `allstocksdatabase-app:latest`,
`caddy:2.10.2-alpine` and `curlimages/curl:8.12.1`. It never pulls images. It parses
the production Compose merge with dummy settings, `/dev/null` as the interpolation
environment and `--no-env-resolution`, so the real app `.env` is not loaded.
It then creates a uniquely named internal Docker network, an app using only
explicit code/template mounts and temporary databases, and a proxy using the
actual route fragment. It publishes no host ports and has no external provider
access. The fixture uses dummy API/login/session credentials and an isolated CA;
curl verifies that CA rather than disabling certificate verification.

The check covers valid/invalid/missing authentication, both dashboards, health,
both sync actions and accepted/rejected CSRF forms. Its cleanup removes only its
own containers, anonymous proxy storage and uniquely named network. It does not
touch existing app volumes or TradingSupport. The existing unittest suite also
covers local forms, cross-browser token rejection, foreign hosts/origins, spoofed
forwarded headers, oversized bodies and public-configuration validation.

Automatic stock-data updates retain the existing weekday `SYNC_TIME_UTC` schedule,
with crypto at daily `CRYPTO_SYNC_TIME_UTC`. Phase 2 adds the persistence and
coordination described below. A real scheduled server refresh remains Phase 4
evidence; local tests do not establish live provider health.

## Refresh status and deployment coordination

`refresh.py` adds standard-library SQLite metadata and POSIX file locks, without
another scheduler, service or dependency. `app.py` retains its two scheduler
threads and per-dataset thread locks; both manual and scheduled jobs use the same
provider functions. Stock/crypto tables, calculations, pacing and configured UTC
times are unchanged.

With the image's default database path, metadata is stored in
`/data/trading212_stocks.db.refresh.db`, next to both existing data files on the
persistent volume. It stores each dataset's last attempt/source, safe integer
summary counts, completion time, last successful completion and next due time.
States are `never`, `running`, `completed`, `partial`, `failed` and `interrupted`.
A full success requires positive updated-row counts and zero failed items; partial
or failed attempts preserve the previous success timestamp. An orphaned running
record is marked interrupted when status is inspected and its dataset lock is
free. A later actual attempt becomes the current record.

The dashboard restores this status after restarts and compares success against the
latest scheduled UTC slot, accounting for stock weekends. It warns about stale,
never-refreshed or unavailable status separately from HTTP liveness. `/health`
remains `ok` even when provider data is stale or a refresh failed. Persisted/displayed
errors and wrapper error logs contain the failure type rather than provider
exception text; summaries exclude tokens, symbol lists and unknown fields.

On first initialization, each dataset gets its next future slot; unknown historical
runs are not invented. Once recorded, a due slot remains pending while deployment
holds the gate, including across a worker/container restart. It runs once after
release and advances to the next future configured slot rather than replaying
every missed day. A manual job begun after a due time satisfies that slot too;
scheduled admission rechecks it under the dataset lock to avoid a duplicate call.
Stock recovery does not add automatic weekend calls: a missed weekday slot waits
until Monday's configured run, including after an early Monday restart. Scheduler
admission and displayed status reconstruct this eligibility from the persisted due
time and the latest weekend using the same calculation; no extra schema or pause
marker is needed. Ordinary weekday recovery after a deployment delay remains
immediate, and crypto remains daily. A worker killed mid-refresh
cannot certify success; its pending retry can repeat provider reads from the
interrupted attempt, while completed committed slots are not repeated.

The volume also holds three stable, private lock files:

- `trading212_stocks.db.refresh.gate.lock`: shared by running jobs, exclusive to
  deployment. The exclusive lock can only be obtained after both datasets drain.
- `trading212_stocks.db.refresh.stocks.lock` and
  `trading212_stocks.db.refresh.crypto.lock`: prevent duplicate dataset runs across
  processes while allowing stock and crypto to run independently.

Files must be owned private regular files; symlinks, hard links and shared-mode
files are rejected. Do not unlink/replace lock files while any worker/helper is
running: all participants must lock the same inode. The kernel releases locks on
descriptor/process closure, so failed helpers do not leave a permanent pause flag.
An update can time out while jobs remain active; it must leave the live app running.

Private CLI commands, run as the app user in the matching image/data environment:

```sh
python refresh.py status
python refresh.py hold --timeout 60
```

`status` prints secret-free JSON and never imports the web app or starts a job.
`hold` uses a bounded wait, prints `drained` after acquiring exclusive access, then
holds it until stdin closes. It exits nonzero on timeout or unsafe state paths.
The Phase 3 deployer runs as the existing server administrator through sudo. It
opens the same app-owned gate file through the local Docker volume's inspected
mountpoint and holds the descriptor itself across app recreation, backup and
recovery. Docker subprocesses inherit it while a cutover runs. This uses the same
Phase 2 kernel lock, without depending on a separate helper's lifetime. Rootful
Docker with a local volume driver is required; unexpected file ownership is
rejected. The private `hold` CLI remains available for operator use in a separate
container, with stdin kept open. Never put the gate holder inside the app being
recreated. No Docker socket is mounted in the application and no browser
maintenance endpoint is added.

Phase 2 validation built `allstocksdatabase-phase2-check:local`, ran 184 unittests
with dummy keys and no external network, and reran the isolated HTTPS smoke test.
Two containers were recreated against one temporary named volume with fake
providers: both datasets, summaries, completion timestamps and next scheduled runs
survived, and neither provider was invoked again for a future slot. The test volume
was removed afterward. This is persistence/recovery evidence, not a live server
deployment or a real provider refresh.

R1 rework validation rebuilt the image and passed **187 unittests** (including a
fresh-process Monday restart regression, one scheduled call at the configured
time, custom UTC time/multiple-week backlog coverage, and unchanged weekday/crypto
recovery). The isolated HTTPS/authentication smoke test and `git diff --check`
also passed. No live server or provider was used for this fix.
