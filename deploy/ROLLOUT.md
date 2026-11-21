# Manual deployment to TradingSupport

These scripts are Phase 3 tooling. Running them without `--dry-run` performs a
deployment; Phase 4 is the live rollout. There is no Git/image polling, deployment
timer or automatic code update. Stock data still updates automatically on weekdays,
and crypto data daily, using the existing configured schedules.

## Prepare locally

Build the local dependency image once (`docker compose build`). Keep the real
`.env` private and outside release artifacts. Create `deploy/server.env` from
`deploy/server.env.example`, mode 0600. It contains only `AS_HOST=live` and the
existing `AS_KEY` path; settings are parsed as literals, never sourced. The SSH
alias must already have a verified host key; the scripts require strict checking.

```sh
bash deploy/update.sh --dry-run
```

The dry run uses a cached image (`--pull=never`), no container network and a private
allowlisted source mount with dummy keys and `/tmp` databases. It tests the actual
packaged source and prints a private review directory containing the archive,
manifest and unpacked code. It reads neither `deploy/server.env`, a private SSH
key nor an application environment file, and invokes neither SSH nor SCP. No live
volume is mounted. `--test-image NAME` selects another already-cached compatible
dependency image. Build/download dependencies separately if none is cached.

Releases contain only the explicit list in `deploy/release.py`. No `.env`, key,
database, log, `.git`, server setting or runtime file is included. The manifest
contains hashes and a code-content release ID. Extraction rejects unknown paths,
links, duplicates, excessive payloads and checksum mismatches before writing code.
The outer review/staging directory is private; non-secret code files are 0644 so
the image's non-root app user can read Docker COPY results.

## Environment file

Prepare a private mode-0600 application environment file. Use only the application
keys in `.env.example`, with `APP_PUBLIC_ORIGIN`, `APP_SECRET_KEY`, Trading 212 and
CoinGecko settings. `APP_RELEASE` is managed by the deployment tool: omit it.
Values are literals, optionally quoted, without whitespace, shell interpolation,
inline comments or backslash escapes. An empty `T212_API_SECRET` is allowed for a
legacy key; all other configured values must be nonempty. A valid read-only stock
key and a private CoinGecko key are required before setup. No provider calls are
made to validate credentials; live validity/quota checks belong to Phase 4.

Keep the reviewed HTTPS hostname aligned with `deploy/Caddyfile.stocks` and keep
`APP_SECRET_KEY` random, at least 32 characters and stable across updates. The
current local environment lacks a CoinGecko key; supply it privately. The tool
refuses a crypto quote-currency change against an existing installation because
old stored highs/lows use its original currency.
Changing a persisted stock/crypto UTC schedule also requires a reviewed
rescheduling procedure; ordinary updates preserve the existing schedule.

## First installation

```sh
bash deploy/setup.sh --env /absolute/path/to/private-app.env
```

The target is fixed at `/home/ubuntu/allstocksDatabase` on `tradingsupport-vnic`.
Before staging or uploading credentials, the first SSH command verifies that
hostname, the `ubuntu` account and its `/home/ubuntu` home directory. A mismatch
stops the operation before any upload or privileged deployment. Strict SSH
host-key checking remains enabled, and privileged preflight repeats the host check.
The SSH user is the existing `ubuntu` account with noninteractive sudo. The remote
deployer runs as root (Docker administration already grants equivalent access),
so installation metadata, server `.env` and recovery files are root-owned/private.
The application itself remains UID 10001. No SSH, package, swap, timezone, firewall,
TradingSupport or proxy setup is changed.

Preflight requires the inspected existing Caddy/network, enabled Docker, Compose
>= 2.24, at least 1 GiB available RAM and 3 GiB free disk. Existing app directories,
app containers or data volumes are refused for first setup. The candidate is built
on the target architecture and its tests run with dummy keys and no network. The
real staged environment stays outside the image build context and is not mounted
into tests. Build memory is subject to host headroom; candidate tests have a 512 MiB
limit. Setup creates only this app's labelled data volume and private installation.

Setup initializes empty stocks/crypto databases and refresh metadata using the
configured schedules, then starts only the app through Compose. It publishes no
ports and does not enable the new Caddy route. Initial source-data import versus
fresh bootstrap is a Phase 4 decision: do not silently reuse or overwrite an
existing destination volume. If importing data, drain the app, take source SQLite
backups, verify integrity/counts in temporary storage, and preserve UID 10001 and
private refresh-file permissions before resuming. An interrupted first install
must be inspected; never delete an unknown pre-existing volume to make setup pass.

## Update and recovery

```sh
bash deploy/update.sh
# Only to deliberately replace this app's server environment:
bash deploy/update.sh --env /absolute/path/to/private-app.env
```

The server `.env` is preserved by default. Treat that root-owned environment as
managed configuration; use `--env` to replace
it rather than editing it independently of the running release.
Code lives under `releases/<id>`; `current.json` records the actual immutable image
ID and release. Compose's
`APP_ENV_FILE` points to the stable server `.env`, while project, volume and network
identities remain fixed across release directories. The generated runtime override
pins the image ID and never contains credentials.

The host's private deployment lock serializes updates before building. After
candidate tests, the root deployer opens the same app-owned gate inode used by
Phase 2 and waits for both datasets to finish. It holds that descriptor itself
through backup, cutover, verification and recovery; Docker subprocesses inherit
it so an in-flight cutover retains the drain if its parent exits. The application
has no Docker socket mount. `--drain-timeout SECONDS` defaults to 300; timeout
defers the update and leaves the current release running. Closing the descriptor
releases the gate, and durable pending schedules resume automatically.

Each update takes consistent SQLite snapshots of both datasets and refresh
metadata, plus the actual previous release/image/environment, under
`backups/<timestamp>-<id>`. Restore checks operate on temporary copies, verify SQLite
integrity and counts, and reject schema/count changes from candidate initialization.
The actual rollback image is checked against the same copies. Incompatible schema
changes require a reviewed migration; the script stops before cutover.

Cutover uses `compose up` for this app service only, without rebuilding/pulling.
HTTP, both data files and refresh status must be readable while the gate is held.
On failure, the previous image/environment is restored and verified before the
lock releases. Rollback restores code/config, never an old database over newer
market data. A failed rollback returns nonzero and retains recovery files for
inspection. Backups remain private; after ten snapshots, archive app-only old
snapshots before updating again. No global prune or unrelated cleanup runs.

For explicit recovery on the server, choose a verified backup ID printed by the
update (the command also requires the original SSH account's sudo context):

```sh
sudo -n python3 /home/ubuntu/allstocksDatabase/releases/RELEASE_ID/deploy/manage.py recover BACKUP_ID
```

Use an installed reviewed release's helper. Inspect the retained `recovery.json`
and actual app image first. Do not edit its paths or restore database snapshots
over current data as part of a code rollback. Unhealthy or missing live state
requires inspection, not guessing a different project/volume.

## Public route and automatic data evidence

The existing Caddy route must be added to its authoritative TradingSupport source
and live configuration together in Phase 4, validated and reloaded without
restarting other services. See `docs/deployment.md`. The scripts do not change it.
Health/readiness checks prove local serving and scheduler/data initialization;
they do not prove a real scheduled provider refresh. Phase 4 still requires a
completed scheduled weekday stock refresh with the local computer disconnected,
and preserved daily crypto operation.

Operations use the local Unix Docker socket explicitly; Docker environment/context
variables cannot redirect a dry run or remote operation to another Docker host.
Deployment output never prints raw Docker inspect/environment or provider keys.
Failed stages and images are retained under app-specific names for inspection.

## Local verification

```sh
python3 -m unittest tests.check_deploy -q
python3 tests/check_deploy_docker.py
python3 tests/check_production.py
bash -n deploy/setup.sh deploy/update.sh
```

The host-only deployment tests use fake SSH/Docker commands for client failures
and verify real SQLite backups, safe extraction, private paths, lock cleanup and
descriptor inheritance. The Docker drill uses an administrative test controller
and uniquely named app resources: real initial setup/update, corrupt staging,
active-job deferral, failed-cutover rollback and explicit recovery. Both datasets
and the server environment are checked for preservation, and an unrelated sentinel
container must keep the same ID and stay running. The test controller's Docker
socket is never mounted into the application. No SSH or production credentials
are used; runtime fixture networks are internal. The driver removes only its
temporary containers, volume, network and fixture directory; namespaced test image
tags/build cache may remain for inspection.
