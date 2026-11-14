# Trading 212 Stock Database

Tracks every stock tradable: daily opening price, yearly and all-time highs and lows, and flags new listings (`NEW_<year>`) and delisted stocks (`DELISTED_OR_ZERO`). Syncs automatically every weekday and shows the data on a small web page.

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

Tests: `docker compose run --rm app python -m unittest`

**Security:** the app has no login and is bound to `127.0.0.1` only. Do not expose port 8000 publicly without a reverse proxy with authentication.
