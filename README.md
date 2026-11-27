# Trading 212 Stock Database

Tracks every stock and ETF tradable: daily opening price, yearly and all-time highs and lows, and flags new listings (`NEW_<year>`) and delisted stocks (`DELISTED_OR_ZERO`). Syncs automatically every weekday and shows the data on a small web page.

## Run

```sh
cp .env.example .env    # then set T212_API_KEY (and T212_API_SECRET, T212_ENVIRONMENT)
chmod 600 .env
docker compose up -d --build
```

Open http://localhost:8000. Press **Sync Now** for the first sync (about an hour for all stocks); after that it runs every weekday at `SYNC_TIME_UTC`. Data is kept in the `data` Docker volume.
The page shows each stock's distance from the year and all-time highs and lows in percent, with min/max filters on each distance, multi-column sorting (up to three columns), and optional grouping into percentage ranges.

## Crypto

Switch between **Stocks** and **Crypto** at the top of the page. Crypto mode lists every coin [CoinGecko](https://www.coingecko.com) tracks with market data (about 18,000, from centralised and decentralised exchanges), with the same distance columns, filters, sorting and grouping. Coins are sorted by market-cap rank by default, and you can search by symbol or name.

- **Key:** set `COINGECKO_API_KEY` in `.env` to a free Demo key from https://www.coingecko.com/en/api.
- **Schedule:** the crypto sync runs every day at `CRYPTO_SYNC_TIME_UTC` (crypto trades 24/7); **Sync Now** in crypto mode starts one by hand. A run takes a few minutes and uses about 275 calls, so one run a day is about 8,250 of the Demo plan's 10,000 calls a month. Each extra **Sync Now** uses about 275 more. It never blocks the stock sync, and data is kept in its own file (`/data/crypto.db`).
- **Yearly high/low:** each run widens it with the coin's 24h high/low. Up to `CRYPTO_HISTORY_CALLS_PER_RUN` coins a day (biggest first) also get one call for their past year of prices.
- **Attribution:** the Demo plan requires crediting CoinGecko, which the crypto page does. It is for personal, non-commercial use.

## Stocks and ETFs

The sync covers both `STOCK` and `ETF` instruments from Trading 212 (warrants are skipped). Each row is marked with its type (`STOCK`/`ETF`) and the dashboard has a Type filter (All / Stock / ETF).

Stocks whose Trading 212 ticker Yahoo doesn't know (old SPAC names, renames) are recovered through their ISIN: the sync looks up the Yahoo symbol by ISIN, validates it against the instrument's expected exchange, and saves the match so later runs reuse it. Only instruments Yahoo really has no data for stay out, and the sync summary reports how many were recovered (`resolved_by_isin`).

The first run takes longer (about 6-7 hours) because of the ISIN lookups; later runs are faster thanks to the saved matches. A Milan mapping (`.MI`) was added so Borsa Italiana instruments resolve.

Tests: `docker compose run --rm app python -m unittest`

**Security:** the app is bound to `127.0.0.1` only and requires a form token for manual syncs. Do not expose port 8000 publicly without an authenticated HTTPS reverse proxy.

## Automated verification

GitHub Actions runs the **Application and deployment checks** job on pushes to every branch
(including names such as `feature/example`) and on pull requests targeting any branch.
Tag pushes are excluded. Every check is required: a failure stops verification and fails the job.
Testing does not depend on `ENABLE_AUTO_DEPLOY` or any deployment secret.

Run the same checks locally from the repository root:

```sh
bash tests/check_ci.sh
```

Requires Linux, Bash, Python 3.11+ and Docker with Compose >= 2.24 and support for
`config --no-env-resolution`. The script uses the local Unix Docker socket, downloads test
images and builds the existing app image as `allstocksdatabase-app:latest`.
It runs CI runner regression tests, offline application unittests, host deployment tests,
an isolated Docker setup/update/rollback/recovery drill, HTTPS/login/CSRF smoke checks and a
packaged-release dry run.
The host-side checks use only Python's standard library; application dependencies stay in Docker.

Tests use dummy credentials and disposable databases. They need no `.env`, SSH key or production
access. The drills remove only their own temporary resources; test images/build cache and the
private release dry-run directory may remain for inspection. Local development and manual server
deployment commands remain available. This workflow currently performs verification only;
automatic deployment is added in CI/CD Phase 2 and enabled after Phase 3 validation.

## Server deployment preparation

The [deployment plan](docs/done/PLAN_deployment.md) targets the existing TradingSupport
server. [Production configuration notes](docs/deployment.md) describe the private
Compose upstream and the proposed route for its existing Caddy proxy. These files
prepare deployment. Manual setup/update scripts are documented in
[deploy/ROLLOUT.md](deploy/ROLLOUT.md); the live rollout remains Phase 4.
Code deployment stays manual. Automatic stock-data updates continue to use the
existing weekday `SYNC_TIME_UTC` scheduler, independently of browser access.

The dashboard shows the next scheduled refresh, last attempt, last successful
completion and stale-data warnings. Refresh status survives container recreation.
Private deployment coordination and its CLI are described in
[the deployment notes](docs/deployment.md#refresh-status-and-deployment-coordination).
