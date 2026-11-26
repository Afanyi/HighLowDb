# Phase 1 Source terms and limits — stock-db

Initial audit: 2026-11-22; Trading 212 and ECB continuation: 2026-11-25. Limits, terms and
coverage for each data source that may be used by later phases. Each row records its own check date.
Provider calls were limited to demo instrument metadata, small Yahoo samples and public ECB FX
files; no validator API was called and keys were never printed.
See docs/AUDIT.md for the data-quality findings. No key, secret or credential appears in this
document.

## 1. Yahoo Finance (yfinance 1.7.0) — primary source

| Item | Value |
|------|-------|
| Role | All-time max/min, current price (latest valid close + date), calendar-year max/min |
| Audit / target adjustment | `auto_adjust=False` (split-adjusted only); never `Adj Close` / dividend-adjusted |
| Audit / target call | `yf.Ticker(symbol).history(period="max", auto_adjust=False, actions=False)`; the home-screening probe used six months and enabled action metadata. |
| Current app | Still omits `auto_adjust=False` and therefore uses dividend adjustment. Application correction belongs to Phase 2; it was not changed during this audit. |
| Pacing | `YAHOO_REQUEST_DELAY` (default 0.5 s) between requests; exponential back-off on 429 |
| Terms | Yahoo's terms prohibit automated collection and building a database without express prior |
|  | permission, including through a manually started script. The owner accepted this risk on |
|  | 2026-11-22. Never describe Yahoo data as licensed, permitted or verified. Yahoo can block |
|  | the server, so a failed sync must leave old values intact. |
| Source | https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html (read 2026-11-22; risk accepted by owner) |
| Check date | Terms: 2026-11-22; additional home-exchange and split probes: 2026-11-25. |
| Measured | 418 full-history requests in 390.5 s, 0 exceptions, 146.6 MiB peak. Default prices are dividend-adjusted (226 of 231 affected stored lows match) |

## 2. Trading 212 — instrument universe

| Item | Value |
|------|-------|
| Role | Instrument list (ticker, ISIN, name, type, currencyCode), one read-only fetch for selection |
| Endpoint | `GET {base}/equity/metadata/instruments`, base by `T212_ENVIRONMENT` (live/demo) |
| Auth | `T212_API_KEY` (+ `T212_API_SECRET` as HTTP Basic when present); fixed host only |
| Environment | Demo only by owner decision (2026-11-25); `.env` is `T212_ENVIRONMENT=demo`. The former live-key requirement is superseded for Phase 1. |
| Demo result | 2026-11-25: HTTP 200; 17,162 instruments = 10,371 STOCK + 6,770 ETF + 21 WARRANT; 9,166 unique stock ISINs. The 2026-11-22 snapshot had 17,166 instruments. |
| Currencies | First API line per unique stock ISIN: USD 5,383 / EUR 2,491 / GBX 824 / CAD 372 / CHF 96. A multi-listed ISIN can have several currencies, so these counts depend on representative-line choice. |
| ISIN prefixes | 63 codes; 23 developed markets plus KY, BM, VG, JE, GG and others outside scope |
| Rate limit | Instrument list: 1 request / 50 seconds; exchanges: 1 / 30 seconds. Limits are per account across keys and IPs. One metadata request on 2026-11-25 succeeded without retry. |
| Sources | [API specification](https://docs.trading212.com/_bundle/api.json?download=), [API terms](https://www.trading212.com/legal-documentation/API-Terms_EN.pdf), [key and IP restrictions](https://helpcentre.trading212.com/hc/en-us/articles/14584770928157-Trading-212-API-key) |
| Terms | Personal testing with the owner’s account/client only; no redistribution, resale or applications for other users without written consent. Algorithmic trading is prohibited by clause 4.2(a); potential future order features need their own terms review. Clause 2.1 recommends minimum necessary permissions. |
| Key scope | Owner requests all available demo permissions for future testing. This audit uses only metadata GET; it does not exercise or verify order permissions. |
| IP policy | Owner permits only server and local public outbound IPs. Local metadata access succeeded; the provider allowlist and server access with the replacement key were not independently inspected. |
| Check date | 2026-11-25 (demo request, API documentation and terms) |
| Live access | Deferred by owner; not required for this demo-only Phase 1 and not verified. |

## 3. Massive (formerly Polygon.io) — independent US price check

| Item | Value |
|------|-------|
| Role | Cross-check US current prices via one grouped-daily call (validation only, never overwrites) |
| Endpoint | `api.massive.com/v2/aggs/grouped/locale/us/market/stocks/{date}` (grouped daily bars) |
| Coverage | US stocks and ETFs only; 18 exchanges + FINRA TRFs + OTC Reporting Facility; OTC excluded by default |
| Free tier (Stocks Basic) | 5 calls/minute, 2 years history, end-of-day data; no trades/quotes/snapshots/WebSockets/flat files |
| Paid stock plans | Starter $29/mo (unlimited, 15-min delayed, 5 yrs); Developer $79/mo (10 yrs); Advanced $199/mo (real-time, 20+ yrs) |
| License | Individual plans: personal, non-commercial, non-transferable. Display/redistribution to other users needs a commercial (Business) license. |
| Storage | Licensed to "access, download, store and use" for personal, non-business, non-commercial use |
| After licence ends | Delete within 30 days, including stored copies and caches. Unused automatic backups may remain up to 90 days. Own results may be kept if they do not contain the data and cannot recreate or replace it |
| Restrictions | No sharing or redistribution; no index, benchmark or reference price; no multiple accounts or rate-limit circumvention |
| Comparison | Not explicitly addressed. Store only pass/fail comparison results |
| Source | https://massive.com/legal/market-data-terms-of-service ; https://massive.com/legal/individuals-terms-of-service ; https://massive.com/docs/rest/stocks/overview |
| Check date | 2026-11-22 (terms last updated 2026-11-03) |
| Notes | One grouped-daily call returns ~12,510 US rows, so it can check many US current prices in a single call. Not usable for non-US prices. |

## 4. Alpha Vantage — independent non-US price check

| Item | Value |
|------|-------|
| Role | Cross-check roughly 22 non-US current prices per day (within the daily allowance) |
| Endpoint | `alphavantage.co/query` REST, JSON or CSV; `apikey` required |
| Free allowance | 25 API requests per day (majority of datasets); verified open-source/educational projects exempt |
| Paid plans | From $49.99/mo (75 requests/min, no daily limit); real-time and 15-min delayed US data is premium/entitlement-gated |
| Coverage | Global equities, forex, crypto, commodities; 100,000+ symbols globally; 25+ years daily history. Non-US freshness is end-of-day unless a US real-time/delayed entitlement is purchased. |
| Adjustment | Splits and cash dividends; raw unadjusted option available on many endpoints |
| License | Personal, non-commercial use, including private investment analysis, research, testing and monitoring. Use for a company, a financial professional or access by others is commercial |
| Storage | **Not addressed.** Do not store Alpha Vantage prices; keep only pass/fail comparison results |
| Source | https://www.alphavantage.co/terms_of_service ; https://www.alphavantage.co/support/api-key ; https://www.alphavantage.co/documentation |
| Check date | 2026-11-22 |
| Notes | With a 10% margin: about 18 weeks if run on two sync days, or about 5 weeks if the manual command runs daily. Exchange coverage must be tested per symbol |

## 5. FX source — traded-value conversion to EUR

| Item | Value |
|------|-------|
| Role | Convert traded value to EUR for the selection gate only |
| Selected | European Central Bank euro reference rates |
| Coverage | Home-listing conversion needs USD, GBP, CAD, CHF, JPY, AUD, SEK, DKK, NOK, ILS, SGD, NZD and HKD; all are present. Rates quote major currency units per EUR. GBX/GBp means GBP/100; ILA means ILS/100. |
| File | https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml — the full file is needed because 63 trading sessions can exceed the 90-calendar-day file. Match each session to the same or preceding ECB working day; never a future rate. |
| Terms | Free reuse with ECB cited; modified values must be stated. Rates are for information and are discouraged for transactions |
| Unconfirmed | The pages do not address automated download or storage, or say whether the statistics reuse policy covers these rates |
| Sources | https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html ; https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html ; https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html |
| Status | **Approved by owner on 2026-11-22**, knowing the unconfirmed items above. Cite the ECB, state that values are converted, use only for the selection gate and do not republish |
| Check date | Terms: 2026-11-22; coverage and full-history XML: 2026-11-25. |

## 6. Providers not part of this plan

| Provider | Reason excluded |
|----------|-----------------|
| WallStreetOdds | US-only; terms on automated storage unclear |
| EODHD | Free plan has no stock all-time fields |
| TradingView | Explicitly non-goal (plan section 2) |
| Twelve Data / Finnhub / FMP | Not selected in the current plan; their complete terms and free non-US coverage were not established |

## 7. Provider summary table

| Provider | Primary use | Free allowance | Non-US coverage | Key required | Terms allow storage? | Check date |
|----------|-------------|----------------|-----------------|--------------|----------------------|------------|
| Yahoo | All values (primary) | n/a (web-scraped via yfinance) | Global (via suffixes) | No | Owner-accepted risk (prohibits automated collection) | 2026-11-22 terms / 2026-11-25 samples |
| Trading 212 | Demo instrument universe | Metadata: 1 request / 50 s | 23 developed markets + others | Yes (demo) | Personal testing only; no redistribution | 2026-11-25 |
| Massive | US current-price cross-check | 5 calls/min (Basic) | US only | Yes | Yes, for personal use; delete within 30 days after licence ends | 2026-11-22 |
| Alpha Vantage | Non-US current-price cross-check | 25 requests/day | Global equities | Yes | Not addressed; store comparison result only | 2026-11-22 |
| ECB (approved) | Traded value → EUR | Public file | All 13 required home-listing currencies | No | Not addressed; owner-approved | 2026-11-22 terms / 2026-11-25 coverage |

## 8. Security check

- No provider key or secret appears in this document.
- `SDB_MASSIVE_API_KEY`, `SDB_ALPHAVANTAGE_API_KEY` and related `SDB_*` variables exist in `.env`
  but are unused by this plan (coded in Phase 4); their values are not printed here.
- Trading 212 `T212_API_KEY`/`T212_API_SECRET` are in `.env` (mode 0600, Git-ignored) and were
  used only for a demo metadata GET; values never logged or printed. Both local env files remain
  mode 0600 and Git-ignored. Broader demo permissions are the owner’s choice, not permission
  to place orders during this audit.
