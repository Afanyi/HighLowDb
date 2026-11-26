# Phase 1 Audit — stock-db universe and data quality

Initial audit: 2026-11-22; demo-only continuation: 2026-11-25 (section 9). Reworked after the
failed Phase 1 review. Environment: local
workstation, read-only server and database inspection, a read-only Trading 212 demo list and a
Yahoo sample of 418 ISINs across all 23 countries. No application code, database,
schedule or server configuration was changed. No key, secret or credential appears in this
document.

## 1. Scope

- Confirm or refute every finding in `PLAN_stock_db.md` section 4 with counts and examples.
- Fetch the Trading 212 instrument list and recalculate ISIN counts and country quotas.
- Map countries, exchanges, Yahoo suffixes and currencies (incl. GBX).
- Check Massive, Alpha Vantage and FX-source terms and coverage (see docs/SOURCES.md).
- Sample Yahoo for runtime, memory, raw/split-adjusted fields and filter pass rates.

## 2. Section 4 findings — confirmed / refuted

Each finding from `PLAN_stock_db.md` section 4 ("Current Understanding") is marked below.
Evidence is drawn from `docs/stocks_export_2026-11-22.csv` (9,695 rows, exported 2026-11-14/15),
a read-only Trading 212 demo fetch, and live Yahoo samples. Counts were re-run on a
read-only SQLite connection where a database was available and match the figures below.

| # | Finding (plan) | Status | Evidence |
|---|----------------|--------|----------|
| 4.1 | `sync.py` fetches T212 instruments, maps Yahoo symbols, computes metrics, upserts `stocks`; `app.py` serves dashboard + manual `POST /sync` | Confirmed | `sync.py:run_sync`, `sync.py:fetch_instruments`, `sync.py:compute_metrics`, `sync.py:_upsert`; `app.py` routes `/` and `POST /sync` |
| 4.2 | `sync.py:212` calls yfinance `history(period="max", actions=False)` with default adjustment | Confirmed | `sync.py:212`: `yf.Ticker(yahoo).history(period="max", actions=False)` |
| 4.3 | Export: 309 rows with `all_time_min <= 0` (277 negative, 244 on Frankfurt) | Confirmed | CSV re-run: 309 rows `<=0`, of which 277 `< 0`; Frankfurt accounts for 244 (next highest: US 20, Madrid 18) |
| 4.4 | Export: 471 rows with ATH/ATL above 10,000 | Confirmed | CSV re-run with positive lows: 471 rows where `all_time_max / all_time_min > 10,000`. Separately, 389 rows have an individual value above 10,000 |
| 4.5 | Export: 3 rows without year values | Confirmed | CSV re-run: 3 rows with empty `year_min` or `year_max` |
| 4.6 | Export: no row without a Yahoo symbol | Confirmed | CSV re-run: 0 rows with empty `yahoo_symbol` |
| 4.7 | Demo T212 list: 10,371 stocks, 6,775 ETFs, 20 warrants, 9,166 unique stock ISINs | Confirmed | Read-only demo fetch: 17,166 instruments = 10,371 STOCK + 6,775 ETF + 20 WARRANT; 9,166 unique stock ISINs |
| 4.8 | Stock currencies are USD, EUR, GBX, CAD, CHF | Confirmed | Demo `currencyCode` over 9,166 unique stock ISINs: USD 5,386, EUR 2,487, GBX 824, CAD 373, CHF 96 |
| 4.9 | Country is the ISIN prefix (Japan, Australia, Nordics appear via foreign lines) | Confirmed | Demo ISIN prefixes span 63 codes; major markets present (US 4,690, GB 725, JP 193, AU 142, SE 55, NO 62, DK 31, FI 35, CH 176, NL 125 unique ISINs) |
| 4.10 | Prefixes KY, BM, VG, JE, GG outside the 23 countries unless a later rule maps them | Confirmed | Demo unique ISINs: KY 427, BM 94, VG 108, JE 42, GG 20 — none are in the 23-country quota list |
| 4.11 | Live app release `c9bad96e9fd67e20`; automatic stock scheduler removed; crypto remains scheduled | Confirmed | Read-only server check: release `c9bad96e9fd67e20`; running `app.py` has 0 stock scheduler starts; commit `bc0bad4` |
| 4.12 | `deploy/manage.py` allows only known app environment keys | Confirmed | `deploy/manage.py:31` `APP_KEYS` allowlist |
| 4.13 | `deploy/release.py` ships an explicit file list; new runtime modules must be added | Confirmed | `deploy/release.py:13` `FILES` tuple |
| 4.14 | README, `.env.example`, `deploy/ROLLOUT.md`, `docs/deployment.md` still describe automatic/all-stock behavior | Confirmed | Pattern search found old automatic/all-stock wording: README 3 lines, `.env.example` 1, `deploy/ROLLOUT.md` 2, `docs/deployment.md` 4 |
| 4.15 | Server: 1 CPU, 1.9 GB RAM, ~33 GB free disk | Confirmed | Read-only SSH: 1 CPU, 1,951 MiB RAM, 33 GB free disk |

### Findings Phase 1 must confirm or refute

| # | Finding | Status | Evidence |
|---|---------|--------|----------|
| A | Prices stop at the 2026-11-13 close | Confirmed | Live stored opens matched Yahoo's session on 2026-11-13 for 359 of 363 sampled stocks with data. The other 4 matched older sessions (2026-10-30, 2026-11-05, 2026-08-26, 2026-02-11) |
| B | Extremes are dividend-adjusted | Confirmed | Of 231 sampled stocks where adjustment changes the low by over 0.5%, 226 stored lows (97.8%) match dividend-adjusted Yahoo lows, 0 match split-only lows and 5 match neither. The earlier 184/185 figure was a different sample. ROST: 0.0833 adjusted vs 0.1133 split-only. XEB.F: −2,816,771.75 adjusted vs 4.63 split-only |
| C | Seven wrong Yahoo symbols | Confirmed | Yahoo names: ACIC = American Coastal, ACHR = Archer; GOLD = Gold.com, B = Barrick; CNX = CNX Resources, CNR = Core Natural; CR = Crane Company, CXT = Crane NXT; ALAIR.PA = Groupe Airwell, ALMEX.PA = Mexedia; SEV = Aptera, SSM = Sono. Under Armour names match, so ISIN decides: Class A `US9043111072` resolves to UAA and Class C `US9043112062` to UA |
| D | LOBO is 10× too low after a reverse split | Confirmed | Yahoo records a 0.1 split on 2026-11-17, after the stored data. Stored all-time max 5.79 and year max 1.48 are now 57.90 and 14.80 |
| E | Stored values show float32 artifacts | Confirmed | CSV examples: `DFK0d_EQ` all_time_min `0.0010000000474974513`; `36Ld_EQ` all_time_min `1.6067187971202657e-05`; `88El_EQ` year_max `2.200000047683716` |

### Unknowns to inspect before implementation (plan section 4)

| Unknown | Status | Notes |
|---------|--------|-------|
| T212 environment and quotas | Demo audit authorized | Owner requests demo only and continuation of Phase 1 on 2026-11-25. The older live-key blocker is superseded for this audit. Refreshed demo supply and quotas are in section 9; live-account supply remains unverified. |
| Yahoo fields/throttling for the selected size | Measured | 418 full-history requests at 0.5 s pacing took 390.5 s with 0 exceptions and no rate-limit errors. A real 1,000-stock run has not been tested. |
| Can each quota fill after filters? | Unknown under the current home-exchange rule | Section 4 estimates use the superseded listing-line screening rule. Section 9 measures a small home-exchange sample; only Phase 3’s full candidate screen can establish actual fill counts. Empty seats are permitted. |
| FX source and terms | Owner-approved | ECB reference rates cover EUR to USD, GBP, CAD and CHF and allow free reuse with citation. Their terms do not address automated download or storage. See `docs/SOURCES.md`. |
| Massive / Alpha Vantage terms and non-US coverage | Researched | See docs/SOURCES.md. Neither provider is coded yet (Phase 4). |
| yfinance split-adjusted behavior for serial reverse splits | Sampled on 2026-11-25 | LOBO and TBT each have one reverse split; diagnostic ETF SQQQ has eight. Split-only OHLC remained positive with no split-factor price discontinuities at those sampled transitions. This checks provider behavior, not every historical price; details below. |

## 3. Historical Yahoo sample results (2026-11-22)

Method: seed `20261122`, up to 20 random unique stock ISINs per country, one random Trading 212 line
per ISIN, `auto_adjust=False`, 0.5 s pacing, a 512 MiB container and ECB FX rates.

- Sampled 418 ISINs: 363 had data, 55 had none, and 0 raised an exception.
- Runtime was 390.5 s, or **about 0.93 s per stock**. A 1,000-stock sync is estimated at about
  **16 minutes**. Screening all 8,244 ISINs once is estimated at about **2.1 hours**.
- Request time: median 0.422 s, p95 0.653 s. Peak memory: **146.6 MiB**.
- Gate results across 418: **104 passed**, 249 median traded value below EUR 1m, 55 no data, 3 too
  few sessions, 2 missing FX, 2 price below USD 2, 1 bad range, 1 zero-return days, 1 missing currency.

## 4. Historical country supply, quotas and estimated eligibility (2026-11-22 demo data)

"Estimated eligible" scales the sample pass rate to supply. "95% upper" is a one-sided upper bound.
These are estimates from small samples and random listing lines, not final counts.

| Country | Unique stock ISINs | Plan quota | Passed sample | Estimated eligible | 95% upper | Quota |
|---|---:|---:|---:|---:|---:|---|
| US | 4,690 | 200 | 13/20 | 3,048 | 3,858 | possible |
| JP | 193 | 104 | 0/20 | 0 | 27 | **likely unfilled** |
| GB | 725 | 77 | 7/20 | 254 | 405 | possible |
| CA | 807 | 76 | 6/20 | 242 | 410 | possible |
| FR | 343 | 62 | 6/20 | 103 | 174 | possible |
| CH | 176 | 60 | 7/20 | 62 | 98 | possible |
| DE | 374 | 59 | 0/20 | 0 | 52 | **likely unfilled** |
| AU | 142 | 54 | 1/20 | 7 | 31 | **likely unfilled** |
| NL | 125 | 50 | 12/20 | 75 | 98 | possible |
| ES | 160 | 39 | 4/20 | 32 | 64 | possible |
| SE | 55 | 39 | 0/20 | 0 | 8 | **likely unfilled** |
| IL | 87 | 26 | 8/20 | 35 | 53 | possible |
| BE | 70 | 23 | 10/20 | 35 | 49 | possible |
| DK | 31 | 23 | 1/20 | 2 | 7 | **likely unfilled** |
| FI | 35 | 22 | 0/20 | 0 | 5 | **likely unfilled** |
| NO | 62 | 19 | 0/20 | 0 | 9 | **likely unfilled** |
| AT | 43 | 14 | 4/20 | 9 | 17 | possible |
| IE | 61 | 12 | 10/20 | 30 | 43 | possible |
| SG | 17 | 12 | 5/17 | 5 | 9 | **likely unfilled** |
| IT | 12 | 10 | 2/12 | 2 | 5 | **likely unfilled** |
| PT | 27 | 10 | 8/20 | 11 | 16 | possible |
| NZ | 6 | 6 | 0/6 | 0 | 2 | **likely unfilled** |
| HK | 3 | 3 | 0/3 | 0 | 2 | **likely unfilled** |
| **Total** | **8,244** | **1,000** | **104/418** | | | |

Quotas total 1,000 with US at 200. **New Zealand (6) and Hong Kong (3) are below 10 because of
supply.** Every other country has at least 10 unique ISINs. Italy is the next smallest at 12.

**Historical finding under the superseded listing-line screening rule:** the sample suggested
several quotas would remain unfilled. Japan, Sweden, Finland, Norway,
Denmark, Australia, New Zealand and Hong Kong are almost entirely available through Frankfurt lines.
Yahoo's `.F` volume is small, so those stocks fail the EUR 1m gate. In a German test of 30 stocks,
**0 passed using `.F`, while 8 passed using `.DE`** (23 had `.DE` data). The `.F` line can therefore
understate how actively a company trades. **Owner decision (2026-11-22): measure liquidity and
volatility on the verified home exchange.** The table above used Trading 212 lines and is not a
home-exchange estimate.

## 5. Currencies, exchanges and Yahoo suffixes (demo)

Currencies over unique stock ISINs: USD 5,386, EUR 2,487, GBX 824, CAD 373, CHF 96.
GBX (pence, GBX = GBP/100) must be handled as a distinct currency when converting traded value to
EUR; it is not GBP.

T212 ticker endings → Yahoo suffix / exchange (from `sync.py` `COUNTRY_CODES` and
`EXCHANGE_LETTERS`, confirmed against demo tickers):

| T212 ending | Yahoo suffix | Exchange | Example |
|-------------|--------------|----------|---------|
| `_US_EQ` | (none) | US | `AAPL_US_EQ` → `AAPL` |
| `_CA_EQ` | `.TO` | TSX | `WELL_CA_EQ` → `WELL.TO` |
| `_PT_EQ` | `.LS` | Lisbon | `JMT_PT_EQ` → `JMT.LS` |
| `_AT_EQ` | `.VI` | Vienna | `AMS2_AT_EQ` → `AMS2.VI` |
| `_BE_EQ`, `_BB_EQ` | `.BR` | Brussels | `VGP_BE_EQ` → `VGP.BR` |
| `...l_EQ` | `.L` | LSE | `BPl_EQ` → `BP.L` |
| `...d_EQ` | `.F` | Frankfurt | `TLSd_EQ` → `TLS.F` |
| `...m_EQ` | `.MI` | Milan | Pattern from `sync.py` |
| `...p_EQ` | `.PA` | Paris | `SUp_EQ` → `SU.PA` |
| `...e_EQ` | `.MC` | Madrid | `FCCe_EQ` → `FCC.MC` |
| `...s_EQ` | `.SW` | SIX | `NESNs_EQ` → `NESN.SW` |
| `...a_EQ` | `.AS` | Amsterdam | `MTa_EQ` → `MT.AS` |

Share-class slashes become dashes: `BRK/A_US_EQ` → `BRK-A`. Rare/secondary listings
(`SWCl1_EQ`, `CEZd1_EQ`, `TPR_EQ_US`, ...) and unknown endings are unmapped.

## 6. Phase 1 acceptance criteria

- Every finding is marked confirmed or refuted with evidence: **met**.
- Demo quota table with supply: **verified in section 9** under the owner’s demo-only continuation.
  The earlier live-table requirement is superseded for Phase 1, not claimed as verified live data.
- Every country below 10 is listed with its reason: **met for demo supply**. NZ and HK.
- FX source approved, or blocker recorded: **met**. ECB approved by owner on 2026-11-22.
- Provider limits, terms, links and check dates: **met** in `docs/SOURCES.md`.

## 7. Phase 1 blockers

The earlier live-key blocker is **superseded for this demo-only audit** by the owner’s
2026-11-25 instruction. Live-account metadata remains unverified and is not authorized.
Current verification evidence and limitations are recorded in section 9. Phase 1 passed review
on 2026-11-25; see section 10 and the active plan for the review record.

Resolved by owner on 2026-11-22: home-exchange liquidity and volatility; ECB rates.

## 8. Security check

- No Trading 212 key/secret, Massive key, Alpha Vantage key, or any other credential appears in
  this document or in the sampled output.
- Trading 212 requests used fixed HTTPS hosts and read-only metadata operations with credentials
  from `.env` (values never printed or logged). The replacement demo key may have all permissions
  at the owner’s request; order permissions were not exercised or independently verified.
- Historical instrument data was at `/tmp/t212_instruments.json` (not present after reboot).
  Current metadata and aggregate probe results are under `/tmp/allstocks-phase1-20261125/`
  (directory mode 0700, data files 0600), outside source control and the release package.
  No downloaded price history was written to disk; the probes discarded it after computing metrics.


## 9. Demo-only Phase 1 continuation — 2026-11-25

### Authorization and data checks

The owner requested demo only, all available demo key permissions for future testing, and an
allowlist containing only the server and local public outbound IPs. Phase 1 was then resumed.
The earlier live-key requirement is superseded for this demo audit. No live broker API request,
order, stock sync, code deployment, database write or server configuration change was made.
A metadata GET does not verify the key’s other permissions or inspect its IP allowlist.

- `.env` and `.env.production` are mode 0600 and Git-ignored; both select `demo` and have key and
  secret settings. Only the local file was used for the metadata request; no credentials were
  printed or mounted in the Yahoo/ECB sample containers.
- One `GET https://demo.trading212.com/api/v0/equity/metadata/instruments` returned HTTP 200:
  **17,162 instruments = 10,371 stocks + 6,770 ETFs + 21 warrants**. There are **9,166 unique stock
  ISINs**, no missing stock ISINs, 63 prefixes and **8,246 ISINs in the 23 selected countries**.
- Relative to 2026-11-22, selected-country supply changed: US 4,690 → 4,691, GB 725 → 724,
  AU 142 → 141, SE 55 → 58. Total unique stock ISINs stayed 9,166.
- The ignored 9,695-row CSV reproduces 309 nonpositive lows, 277 negative lows, 244 nonpositive
  Frankfurt lows, 471 ATH/ATL ratios above 10,000, 389 individual extrema above 10,000, 3 missing
  year ranges and 0 missing Yahoo symbols. Its update timestamps span 2026-11-14/15.
- Read-only SQLite queries on the server reproduce 9,695 rows, 309 nonpositive lows, 277 negative
  lows, 471 ratios above 10,000, 3 missing year ranges and latest update `2026-11-15 09:48:45`.
  `PRAGMA integrity_check` is `ok`; running app source still has 0 stock scheduler starts.

### Recalculated demo quotas

Use the [MSCI 30 June 2026 country weights](https://www.msci.com/downloads/web/msci-com/research-and-insights/video/acwi-imi-complete-geographic-breakdown/oct-2026/A%20Complete%20Geographic%20Breakdown%20of%20the%20MSCI%20ACWI%20IMI.pdf)
and square-root weights. Iteratively pin countries outside their bounds, then distribute the
remaining seats proportionally among unpinned countries. Bounds: US 200, others at most 150;
75% of unique demo supply rounded down, except that the minimum of 10 overrides that cap where
supply allows. Final fractional seats use largest remainders, with country code as a tie-break.
This reproduces the existing quota allocation despite the changed supply counts.

| Country | MSCI weight % | Unique demo stock ISINs | Demo quota |
|---|---:|---:|---:|
| US | 62.7 | 4,691 | 200 |
| JP | 5.6 | 193 | 104 |
| GB | 3.1 | 724 | 77 |
| CA | 3 | 807 | 76 |
| FR | 2 | 343 | 62 |
| CH | 1.9 | 176 | 60 |
| DE | 1.8 | 374 | 59 |
| AU | 1.5 | 141 | 54 |
| NL | 1.3 | 125 | 50 |
| ES | 0.8 | 160 | 39 |
| SE | 0.8 | 58 | 39 |
| IL | 0.35 | 87 | 26 |
| BE | 0.27 | 70 | 23 |
| DK | 0.38 | 31 | 23 |
| FI | 0.26 | 35 | 22 |
| NO | 0.18 | 62 | 19 |
| AT | 0.1 | 43 | 14 |
| IE | 0.08 | 61 | 12 |
| SG | 0.39 | 17 | 12 |
| IT | 0.71 | 12 | 10 |
| PT | 0.04 | 27 | 10 |
| NZ | 0.04 | 6 | 6 |
| HK | 0.4 | 3 | 3 |
| **Total** | | **8,246** | **1,000** |

NZ (6) and HK (3) remain below 10 because that is all available demo supply. All other countries
have supply of at least 10. These are quota ceilings, not approved membership or evidence that
1,000 names will pass the filters. Live-account supply has not been checked.

### Home-exchange sample protocol

The old 418-ISIN random sample in sections 3–4 used Trading 212 listing lines and cannot estimate
eligibility under the owner’s home-exchange rule. The new probe deliberately selected 45 familiar
companies (two per country, one for Singapore) to check home-market coverage and units. It is
**not random or representative**; do not extrapolate its pass rate to country supply.

Match each candidate to an ISIN in the demo metadata, then compare Yahoo company name,
company country and the expected home exchange. This uses the plan’s name/country/exchange
identity alternative, not an independent ISIN lookup for every Yahoo symbol. Ambiguous matches
stay excluded. Get six months of `history(auto_adjust=False, actions=True)` in memory, discard
all rows on or after 2026-11-25 (conservative completed-session cutoff), remove rows with no OHLC,
and screen the latest 63 sessions. Price observations from 2026-11-25 were deliberately excluded
even for markets already closed, so results do not mix completed daily and intraday bars.

The audit checks positive, ordered OHLC; 68 available sessions before taking 63; at least 57 valid
sessions; at least 90% nonzero-volume sessions; at most 6 zero-volume days; at most 20% zero-return
days; median close × volume of EUR 1m; and latest price of USD 2. A close-to-close factor outside
0.5–2 is a conservative jump flag for manual review, not a production split detector. RYZ63,
market-calendar missing-session checks, full IPO handling and membership selection remain Phase 3.
Thus “pass” below means passing this audit probe, not final universe eligibility.

Convert each session with that day’s ECB rate, or the preceding published rate no more than
7 calendar days old (a conservative probe bound). The full ECB history file resolves missing
rates at the beginning of 63-session windows that extend beyond the 90-day file. All required
currencies are available. Divide GBX/GBp prices by 100 before using GBP rates, and ILA by 100
before using ILS rates. The same unit conversion applies to the USD price gate.

The first probe used the 90-day file and included current-day observations; it was superseded
by this completed-session/full-FX probe after missing-rate and incomplete-price findings.
No downloaded price-history frames were saved. Only identity and aggregate screen results remain.

### Home-exchange sample results

The completed-session probe took **155.88 s**, peaked at **233.26 MiB** in a
512 MiB / 1 CPU container, and completed all 45 candidates. There were 42 probe passes, one
filter exclusion and two unavailable candidate symbols. No rate-limit exception was recorded.

| Country | Candidate Yahoo symbols | Initial probe passes / candidates |
|---|---|---:|
| US | `AAPL`, `MSFT` | 2/2 |
| JP | `7203.T`, `6758.T` | 2/2 |
| GB | `SHEL.L`, `BP.L` | 2/2 |
| CA | `RY.TO`, `ABX.TO` | 2/2 |
| FR | `MC.PA`, `TTE.PA` | 2/2 |
| CH | `NESN.SW`, `ROG.SW` | 1/2 |
| DE | `SAP.DE`, `SIE.DE` | 2/2 |
| AU | `BHP.AX`, `CSL.AX` | 2/2 |
| NL | `ASML.AS`, `INGA.AS` | 2/2 |
| ES | `IBE.MC`, `SAN.MC` | 2/2 |
| SE | `VOLV-B.ST`, `ERIC-B.ST` | 2/2 |
| IL | `TEVA.TA`, `ESLT.TA` | 2/2 |
| BE | `ABI.BR`, `UCB.BR` | 2/2 |
| DK | `NOVO-B.CO`, `MAERSK-B.CO` | 2/2 |
| FI | `NOKIA.HE`, `KNEBV.HE` | 2/2 |
| NO | `EQNR.OL`, `DNB.OL` | 2/2 |
| AT | `OMV.VI`, `VOE.VI` | 2/2 |
| IE | `AIBG.IR`, `BIRG.IR` | 1/2 |
| SG | `D05.SI` | 1/1 |
| IT | `ENEL.MI`, `ENI.MI` | 2/2 |
| PT | `EDP.LS`, `GALP.LS` | 2/2 |
| NZ | `AIR.NZ`, `CEN.NZ` | 1/2 |
| HK | `1299.HK`, `0992.HK` | 2/2 |

All 43 candidates with usable metadata/history matched name, country and home exchange. Each had
63 valid OHLC sessions through 2026-11-24 and complete FX coverage. AIR.NZ failed the liquidity,
USD-price and zero-return-day checks; its median EUR traded value was about EUR 168,255.
CEN.NZ passed with about EUR 3.83m median traded value. JPY, AUD, SEK, DKK, NOK, ILA, SGD, NZD,
HKD and GBp data were exercised, alongside USD, CAD, CHF and EUR. The home-market examples show
that a thin Frankfurt line does not by itself make a company illiquid; the old country-fill
estimates are superseded, and actual fill counts remain unknown until the Phase 3 screen.

ROG.SW and AIBG.IR were unavailable and were not accepted by guessing. The following primary
sources identify the candidate corrections for a separate two-symbol follow-up:

- [Roche’s security table](https://www.roche.com/investors/bonds) identifies `CH1499059983`
  as participation certificate **ROP**, distinct from bearer share `CH0012032113` (**RO**).
  [Roche’s transition notice](https://www.roche.com/investors/updates/inv-update-2026-03-16)
  dates the ROG → ROP change to 17 March 2026. This is an additional mapping issue for Phase 2,
  not permission to merge the two Roche share classes or fabricate earlier trading history.
- [Euronext’s regulated-equity list](https://live.euronext.com/pt/products/equities/regulated/list)
  gives AIB Group, `IE00BF0L3536`, the Dublin symbol **A5G**; the attempted AIBG.IR was unverified.
- [EDP’s admission notice](https://edp.com/en/investors/investor-information/market-notifications/admission-trading-shares-edp)
  confirms `PTEDP0AM0009` on Euronext Lisbon despite the shortened Yahoo name “EDP, S.A.”.

The issuer/exchange-based follow-up succeeded for **ROP.SW and A5G.IR**: each matched name,
country and exchange, had 63 valid sessions with complete FX and passed the probe. Median EUR
traded values were about EUR 399.49m and EUR 48.67m, respectively. The two-symbol follow-up took
7.56 s with 233.64 MiB peak memory, including a fresh full ECB XML parse.
With those two corrected candidate symbols, **44 of the 45 selected ISINs pass the audit probe**;
AIR.NZ remains excluded. These are evidence for later mapping work, not saved runtime overrides.

### Split-adjustment follow-up

Three full-history probes used `auto_adjust=False, actions=True`, excluded 2026-11-25 and
kept their history only in memory. TBT and SQQQ are diagnostic ETFs, not stock-universe candidates.

| Symbol | Valid price sessions | Reverse splits | Nonpositive lows | Observation |
|---|---:|---:|---:|---|
| LOBO | 673 | 1 | 0 | 2026-11-17 factor 0.1; after/before adjusted-close ratio 1.0541; split-only all-time max about 57.90 |
| TBT | 4,657 | 1 | 0 | 2012-10-05 factor 0.25; close ratio 1.0250 |
| SQQQ | 4,223 | 8 | 0 | Four 0.25 events (2012, 2014, 2017, 2019) and four 0.2 events (2020, 2022, 2024, 2025); transition close ratios 0.9531–1.0744 |

No missing OHLC remained in these cleaned price frames. The near-continuous adjusted-close
ratios support that Yahoo already adjusts historical prices for splits with `auto_adjust=False`;
do not apply split factors a second time. They do not independently prove each all-time extreme.
SQQQ’s split-only maximum is about 13 million in current share units: a large historic extreme
can result from repeated reverse splits and is not by itself evidence of a bad tick. Yahoo’s
returned values also retain small precision artifacts (LOBO maximum 57.900001525878906), so
using float64 preserves the received precision but does not reconstruct precision absent upstream.

### Verification and remaining limits

- Current CSV and server read-only counts agree; server database integrity is `ok`.
- The unchanged application’s **187 unittests passed** in 3.638 s in a read-only app-image
  container with no network, dummy keys and temporary databases.
- Quota bounds, the plan’s quota dictionary, all 45 combined sample identities, completed-session
  counts, FX coverage and the eight-split aggregate passed direct consistency checks.
- A literal configured-secret scan of the three documents and temporary audit artifacts found no
  credentials; private env and data permissions passed. `git diff --check` passed.
- This phase changes only `docs/AUDIT.md`, `docs/SOURCES.md` and `docs/PLAN_stock_db.md`.
  Execution status was **Ready for Review**; subsequent review is recorded in section 10.
  No later phase was started.
- The original 418 full-history sample remains the runtime estimate for full member refresh;
  the new six-month sample also includes identity lookups and a full FX download, so its runtime
  must not be substituted for a 1,000-member full-history benchmark.
- Full country fill counts, general home-listing resolution and production split/jump detection
  are not implemented or verified by this audit. They belong to the existing later phases.


## 10. Phase 1 review — 2026-11-25

**Done — Passed Review.** All five current Phase 1 acceptance criteria passed under the owner’s
demo-only decision. The active plan contains the criterion-by-criterion review evidence.

Independently reproduced the demo metadata counts, square-root quota allocation, bounds,
reported table and plan dictionary; all 45 combined sample identities and complete FX/session
aggregates; and the CSV findings. Fresh read-only server SQLite queries reproduce the reported
counts and pass integrity checking. The seven ticker mismatches were rechecked against current
Yahoo company metadata; [Under Armour’s issuer FAQ](https://about.underarmour.com/en/investors/resources.html)
confirms the Class A/UAA and Class C/UA distinction. The review metadata aggregate is retained
privately at `/tmp/allstocks-phase1-20261125/review_symbols.json`.

All **187 tests passed again in 3.630 s** with no network, dummy keys, read-only source and
temporary databases. Literal and encoded configured-credential scans and private-file permissions
passed; no credible security issue was found. `git diff --check` passed. Review changes are limited
to the plan’s review record/status and this report’s review record/status.

The review did not repeat the historical 418-stock price sample, inspect the provider-side IP
allowlist, exercise order permissions, call the live brokerage API or run the full candidate
screen. The report’s stated limits remain in force. Phase 2 is unblocked but was not started.
