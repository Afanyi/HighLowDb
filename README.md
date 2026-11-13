# Trading 212 Stock Database

Tracks every stock tradable on Trading 212: daily opening price, yearly and all-time highs and lows, and flags new listings (`NEW_<year>`) and delisted stocks (`DELISTED_OR_ZERO`). Syncs automatically every weekday and shows the data on a small web page.

## Run

```sh
cp .env.example .env    # then set T212_API_KEY (and T212_API_SECRET, T212_ENVIRONMENT)
chmod 600 .env
docker compose up -d --build
```

Open http://localhost:8000. Press **Sync Now** for the first sync (about an hour for all stocks); after that it runs every weekday at `SYNC_TIME_UTC`. Data is kept in the `data` Docker volume.

Tests: `docker compose run --rm app python -m unittest`

**Security:** the app has no login and is bound to `127.0.0.1` only. Do not expose port 8000 publicly without a reverse proxy with authentication.
