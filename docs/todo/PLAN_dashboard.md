# Implementation Plan: Bootstrap 5 Dashboard, Price-Distance Filters and Multi-Column Sorting

Follows `docs/todo/PLAN.md` (Phases 1–4 done). This is Phase 5.

## 1. Goal
The web page looks like a clean dashboard built with plain Bootstrap 5. For every stock, it shows how far the current price is from the year and all-time highs and lows, in percent. You can:
- filter by a minimum and/or maximum for each of these 4 distances
- combine any number of filters at once (all must match)
- sort by up to 3 columns at once (for example, % below ATH descending, then % above year low ascending)
- group the table into percentage ranges of one chosen distance

## 2. Non-Goals
- No JavaScript. Bootstrap's CSS covers the layout; the JS bundle is not loaded.
- No build step, no npm, and no other CSS or JS library.
- No DB schema change, no migration and no sync change. The distances are calculated when the page is queried and are not stored.
- No live or intraday price. "Current price" means the stored `opening_price`.
- No pagination, charts, saved filters or login.

## 3. Constraints
- All `PLAN.md` §3 constraints still apply, with one change: the rule "no CSS frameworks" now allows **plain Bootstrap 5 only**. The user asked for this on 2026-11-14.
- Bootstrap is loaded from jsDelivr with an exact version, an SRI hash and `crossorigin="anonymous"`:
  ```html
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
        integrity="sha384-QWTKZyjpPEjISv5WaRU9OFeRpok6YctnYmDr5pNlyT2bRjXh0JMhjY6hW+ALEwIH" crossorigin="anonymous">
  ```
- The work touches only `app.py`, `templates/index.html`, `tests/test_app.py` and `README.md` (one line). It adds no new Python file and no new dependency.
- Keep the Python small, about 70 added lines in `app.py` (about 20 of them for multi-column sorting).

## 4. Current Understanding
- `app.py`:
  - `_query_rows(status, q, country, sort, direction)` builds parameterised SQL. `ORDER BY` is limited to the whitelist in `ALLOWED_SORTS`, and NULLs always sort last.
  - `index()` reads the query args and passes `columns`, `countries`, the sync summary and similar values to the template.
- `templates/index.html` is about 90 lines of plain HTML with inline CSS:
  - two forms: filter (GET) and Sync Now (POST)
  - one table whose headers are sort links
  - the sort links carry over `status`, `q` and `country` only
- The `stocks` columns used here are `opening_price`, `year_max`, `year_min`, `all_time_max` and `all_time_min`. Any of them can be NULL. Delisted stocks have an `opening_price` near zero, and their year values can be NULL.
- Open item from the Phase 4 review: an invalid `sort`/`dir` falls back correctly in the SQL, but the template gets the raw value, so no ▲/▼ marker is shown. This plan fixes it because it touches the same code.
- The DB holds about 10k stocks, and the page renders all of them (Phase 4 measured 0.14 s and 2.7 MB).

## 5. Design

### 5.1 Distance columns (`app.py`)
One constant defines the 4 distances. All other code (SQL, sorting, filters, headers, grouping) reads from it:

```python
DISTANCES = {
    "pct_year_max":     ("% below Year High",     "(year_max - opening_price) * 100.0 / NULLIF(year_max, 0)"),
    "pct_year_min":     ("% above Year Low",      "(opening_price - year_min) * 100.0 / NULLIF(year_min, 0)"),
    "pct_all_time_max": ("% below All-Time High", "(all_time_max - opening_price) * 100.0 / NULLIF(all_time_max, 0)"),
    "pct_all_time_min": ("% above All-Time Low",  "(opening_price - all_time_min) * 100.0 / NULLIF(all_time_min, 0)"),
}
```
- Highs measure how far the price is **below** them, and lows how far it is **above** them. So a value of 0 means the price is at that high or low, and larger means further away. A negative value means the price is outside the stored range (for example, today's open is above the year high).
- The SQL is wrapped in a subquery so the WHERE and ORDER BY can use the aliases:
  `SELECT * FROM (SELECT <existing columns>, <expr> AS pct_year_max, ... FROM stocks) WHERE ... ORDER BY ...`
- If a high or low is NULL or 0, the result is NULL. The page shows "—" for it, and it sorts last.
- `ALLOWED_SORTS |= set(DISTANCES)`. The header list gets the 4 distance columns from `DISTANCES`.

### 5.2 Min/max filters
- There are 8 optional query parameters: `min_<key>` and `max_<key>` for each key in `DISTANCES`, for example `min_pct_all_time_max=50&max_pct_year_min=10`.
- Both bounds include the limit: `min` means `>=` and `max` means `<=`. All active bounds are combined with AND.
- Values are parsed with `float()` and must pass `math.isfinite`. Empty, non-numeric, `nan` or `inf` values are ignored, the same way as the existing status and country filters, and never cause an error.
- If min is greater than max, the result is empty, as the user asked. The values are not swapped.
- Only the column names from `DISTANCES` go into the SQL text. The values are passed as `?` parameters.
- A row with a NULL distance never matches a bound on that distance. This is normal SQL behaviour.

### 5.3 Group by
- The `group` parameter must be one of the `DISTANCES` keys. Any other value is ignored, and the table stays ungrouped.
- The ranges are fixed, each including its lower limit and excluding its upper limit: `< 0%`, `0–10%`, `10–25%`, `25–50%`, `50–100%`, `≥ 100%`, plus `n/a` for NULL.
  ```python
  BUCKETS = [(None, 0), (0, 10), (10, 25), (25, 50), (50, 100), (100, None)]
  ```
- A small helper `_group_rows(rows, key)` splits the rows (already filtered and sorted in SQL) into these ranges, keeping their order and dropping empty ranges. Grouping is done in Python because SQL sorts by the chosen column, not by range.
- `index()` always passes `groups`, a list of `(label, rows)`. With no grouping, this is `[(None, rows)]`, so the template has a single loop.

### 5.4 Multi-column sorting
- **URL format:** `sort` and `dir` become repeated parameters, paired by position, for example `?sort=pct_all_time_max&dir=desc&sort=pct_year_min&dir=asc`. Existing single-sort URLs (`?sort=ticker&dir=desc`) keep working unchanged.
- **Parsing** (`_parse_sorts(args)` in `app.py`):
  - Read `request.args.getlist("sort")` and `getlist("dir")` and pair them by position. A missing or invalid `dir` becomes `asc`.
  - Drop a level if its column is empty or not in `ALLOWED_SORTS`. Also drop a column that already appeared at a higher level.
  - Keep at most `MAX_SORTS = 3` levels. With no valid level, fall back to `[("ticker", "asc")]`.
  - The result is a list of `(column, direction)` pairs. Only these checked values are passed on.
- **SQL:** for each level, `ORDER BY <col> IS NULL, <col> <dir>`, joined in order, then `ticker` as the final tie-breaker. NULLs stay last at every level. Column names and directions come only from the whitelist, never from raw input.
- **Clicking a column header** (`_header_sorts(sorts, col)`, which builds the new list for each header link):
  - On the primary column, flip its direction and keep the other levels.
  - On any other column, make it the primary sort (ascending, or its current direction if it is already a lower level), move the earlier levels down one place, remove the duplicate, and keep at most 3 levels.
- **Header markers:** each sorted column shows its direction and level, for example `▼1`, `▲2`. Unsorted columns show nothing. With a single level, only the arrow is shown, as today.
- **Sort controls in the filter card:** 3 rows of "Sort by / then by / then by". Each row has a column `form-select` (with an empty "—" option plus all `columns`) and an asc/desc `form-select`. All 3 pairs are always submitted, so the positions stay aligned. Empty levels are dropped by `_parse_sorts`. The controls are pre-filled with the current levels, so **Apply** keeps the sort.
- **With grouping:** within each range, rows follow the full multi-level sort.

### 5.5 Keep filters and sorts across links
- `index()` builds one `filters` dict holding only the parameters that are set: `status`, `q`, `country`, `group` and every valid `min_*`/`max_*` (written back with `:g`).
- Header links use `url_for('index', sort=[cols...], dir=[dirs...], **filters)`, built from `_header_sorts`. Flask writes lists as repeated parameters.
- The template only gets the checked sort list, which fixes the open Phase 4 item.

### 5.6 Template layout (`templates/index.html`, Bootstrap 5 classes only)
- **Navbar** (`navbar bg-body-tertiary`): the title on the left. On the right, the sync status badge (`text-bg-warning` while running, `text-bg-success` for OK, `text-bg-danger` for errors) and the **Sync Now** POST form (`btn btn-primary btn-sm`). The full summary goes in the badge's `title` attribute.
- **Stat cards** (`row g-3`, `col-6 col-md-3`, `card`): Shown, Active, New this year and Delisted/Zero. They count the filtered rows, using `collections.Counter` in `index()`.
- **Filter card** (one GET form):
  - Row 1: Search, Status and Country (`form-control`/`form-select`).
  - Row 2: "Distance from current price". One `input-group` per distance: `[label] [min] – [max] %`, using `type="number" step="any"` with the placeholders `min`/`max`.
  - Row 3: the 3 sort levels (§5.4).
  - Row 4: the Group by `form-select` (None plus the 4 distances), an **Apply** button and a **Reset** link to `/` (which also clears the sort).
- **Group summary** (only when grouped): one `badge` link per range showing `label · count`, which jumps to `#group-N`.
- **Table card**:
  - `table table-sm table-striped table-hover` inside `table-responsive` with `max-height: 75vh`.
  - The header sticks to the top (`position: sticky; top: 0`, the only custom CSS).
  - Number columns are right-aligned (`text-end`).
  - When grouped, each range starts with a full-width header row (`table-secondary`, `id="group-N"`, `label · count`).
  - Status shows as a badge: ACTIVE in `success`, NEW_* in `info` and DELISTED_OR_ZERO in `secondary`.
  - Distances are shown as `12.3%`. Missing values show as `—` in `text-body-secondary`.
- Below the table is the row count. A Jinja macro formats the 8 number columns so the formatting is written once.
- The page uses the light theme only. Following the system's dark mode in Bootstrap 5.3 needs JS, so it is left out.

## 6. Security Considerations
- SQL: column names come only from the `ALLOWED_SORTS`/`DISTANCES` whitelists, sort directions only from `asc`/`desc`, and bound values are always `?` parameters. The number of sort levels is capped at 3, so a long URL cannot build a huge `ORDER BY`.
- Numbers are parsed with `float` plus `isfinite`. No user input is ever placed in the SQL text.
- The template keeps autoescaping and adds no `|safe`. All links are built with `url_for`.
- The third-party CSS is pinned and checked with SRI, so a changed file on the CDN is blocked. No third-party JS is loaded.
- The CDN dependency means the page needs internet to look right. Without it, the page still works but is unstyled. The sync already needs internet, so this is acceptable.
- No new routes and no new data are exposed. The app stays bound to `127.0.0.1`.

## 7. Implementation Phases

### Phase 5: Bootstrap 5 dashboard and distance filters
**Status:** Done — rework complete
**Depends on:** Phase 4
#### Rework (2026-11-14)
- Must fix:
  1. Page size fixed. Jinja `trim_blocks`/`lstrip_blocks` are on, the `fmt` macro has whitespace control, and each row is written on one line. With 10,300 rows the page is **4.50 MB** (was 7.44 MB) and loads in 0.36–0.41 s. The rest of the growth over Phase 4's 2.7 MB comes from the 4 extra columns, the `text-end` classes and the status badges.
  2. The duplicate sync alert block is removed. The navbar badge is the only sync summary, and the test checks that it appears exactly once.
  3. Level markers fixed: a single level shows `▲`/`▼`, and 2 or more levels show `▲1`, `▼2` and so on.
  4. New tests:
     - `test_level_markers`
     - `test_sort_controls_prefilled`
     - `test_header_links_keep_sort_levels_and_filters` (parses each header link and checks the sort levels after clicking the primary column and another column, plus `status`, `q`, `country` and `min_*`)

     Weak tests were strengthened: the 4th level is checked as ignored, a duplicate column is checked as used once, and the old single-sort URL is checked for its actual descending order.
- Should fix:
  5. Only the number columns are right-aligned (`numeric_cols`).
  6. The sticky header style lives only in the `th.sticky` CSS rule.
  7. `_query_rows` accepts only `>=`/`<=` as `op`.
  8. The template loops over `distance_labels`, built from `DISTANCES`, instead of matching the `pct_` prefix.
- Optional items done:
  - The search input is now `-sm`.
  - `navbar-light` is removed.
  - The unused `counts` template variable is removed.
- Fixes found in the browser check (not visible in tests):
  - The table was squeezed into Bootstrap's fixed-width `container`, so cells wrapped and rows became 3 lines tall. The page now uses `container-fluid`, and the table has `text-nowrap` and scrolls sideways inside its card.
  - The Status, Country, sort and group dropdowns showed only "All" or "—" with no visible label. Every control now has a visible label linked with `for`/`id`.
  - On phones, each sort column and its Order now share one row.
  - Header links and group badges are no longer underlined.
- Not changed (optional): the trailing default `ticker` level after a header click, and the second check of sort levels in `_query_rows`.
- **Data issue found, not part of Phase 5:** the live DB holds impossible values from yfinance, for example an all-time high of `737553779962280290939461173248.00` and an all-time low of `-728702185293733075060628201144320.00` (`AKTAd_EQ`, `CISS_US_EQ`, `AYTU_US_EQ`). They make those columns very wide and their distances meaningless, so they rank near the top of "% below All-Time High". A later sync phase should reject non-positive or absurd prices.
- Verification:
  - Local `python3 -m unittest`: 80 tests, OK.
  - `docker compose run --rm app python -m unittest`: OK.
  - `docker compose up -d --build`: `Up (healthy)`, and `/health` returns `ok`.
  - Live pages (996 rows) return 200, including grouped pages, 2-level sorts, and country plus min/max filters.
  - Headless Chrome screenshots at 1600 px and 390 px wide were checked after the fixes.
  - Security grep of the diff: no `|safe`, no `<script>`, and no request values in SQL f-strings.
#### Review (2026-11-14)
- Not yet passed. Everything planned is implemented and works, and the diff stays inside the planned files (`app.py`, the template, the tests, `README.md`). The items below must be fixed first.
- Verification run by the reviewer:
  - Local `python3 -m unittest`: 77 tests, OK.
  - Timing with 10,300 synthetic rows. Every request is under the 2 s target:

    | Request | Time |
    |---|---|
    | `/` | 0.47 s |
    | `?group=pct_all_time_max` | 0.44 s |
    | 3 sort levels plus grouping | 0.47 s |
  - Not yet run: `docker compose run --rm app python -m unittest`, `docker compose up -d --build`, and the browser check at desktop and phone width.
- Must fix:
  1. **The page grew from 2.7 MB to 7.44 MB.** About 42 % of it is whitespace. The `fmt` macro and the `{% for %}`/`{% if %}` tags print newlines and indentation in every cell, for example `<td class="text-end">\n  423.87\n  \n</td>`. Fix: use whitespace control (`{%- … -%}`) in the macro and the row loops, or set `app.jinja_env.trim_blocks = lstrip_blocks = True`. Then measure the size again.
  2. **The sync summary appears twice.** The navbar badge shows it, and the old `alert alert-info` block under the navbar repeats it. The plan (§5.6) puts it only in the badge, with the details in its `title`. Remove the alert block.
  3. **Level markers don't match §5.4.** With 2 or more levels, the primary column shows a bare `▲`/`▼` instead of `▲1`/`▼1`. Only a single-level sort should show the bare arrow.
  4. **Some acceptance criteria have no test:**
     - the level markers (`▼1`, `▲2`)
     - the sort controls pre-filled from the URL
     - header links that keep every sort level together with `status`, `q` and `country` (the route test only checks `group` and one `min_*` value)
- Should fix:
  5. Every `<th>` has `text-end`, but Ticker, Name, Exchange, Status and Updated are left-aligned in the body, so their headers sit in the wrong place. Right-align only the number columns.
  6. The sticky header CSS is written twice: as a `th.sticky` rule and again inline on every `<th>`. Keep the CSS rule only.
  7. `_query_rows` puts `op` from `bounds` into the SQL without checking it. Today only `index()` calls it, with `>=`/`<=`, but add `op in (">=", "<=")` next to the existing `key in DISTANCES` check for defence in depth (§6).
  8. The template picks the distance columns with `key.startswith("pct_")` in 3 places. Pass `DISTANCES` (or its keys) to the template and loop over that, so all code reads from the one constant (§5.1).
- Optional:
  - The default `ticker` level stays on as level 2 after you click a header, so Ticker shows `▲2`. Ticker is already the final tie-breaker, so a trailing default `ticker` level could be dropped.
  - `_query_rows` checks the sort levels again even though `_parse_sorts` already did. One check would be enough.
  - The search input is full size, while the other controls use `-sm`.
  - `navbar-light` is deprecated in Bootstrap 5.3 and has no effect next to `bg-body-tertiary`.
- Security: no issues found.
  - Sort columns, directions and distance keys come only from whitelists, and the bound values are `?` parameters.
  - Autoescaping is kept, there is no `|safe`, and all links are built with `url_for`.
  - The page has the pinned CSS with SRI and no `<script>` tag.
#### Goal
Section 1 above.
#### Scope
- `app.py`: add `DISTANCES`, `BUCKETS`, the subquery with the distance columns, min/max filtering, `_parse_pct`, `_parse_sorts`, `_header_sorts`, multi-level `ORDER BY`, `_group_rows`, `filters` and the status counts.
- `templates/index.html`: rewrite it with Bootstrap 5 as described in §5.6, including the sort controls and level markers.
- `tests/test_app.py`: the new tests listed below.
- `README.md`: one line about the distance filters, multi-column sorting and grouping.
#### Do Not Change
`sync.py`, the DB schema, the scheduler, `/sync`, `/health`, the Docker files, `requirements.txt`, and the existing behaviour of status, search and country filters.
#### Reuse First
The `_query_rows` way of building conditions, the `ALLOWED_SORTS` whitelist, the NULLs-last `ORDER BY`, the existing `columns` list, `EXCHANGE_COUNTRIES`, and the existing test fixtures (their price values already give known distances).
#### Steps
1. Add `DISTANCES`/`BUCKETS` and extend `ALLOWED_SORTS` and `columns`. (Done)
2. Wrap the SELECT in the subquery and add the min/max conditions to `_query_rows(..., bounds=None)`, where `bounds` is a list of `(key, op, value)` with `op` either `>=` or `<=`. (Done)
3. Add `_parse_sorts` and `_header_sorts`, and change `_query_rows` to take the sort list and build the multi-level `ORDER BY`. (Done)
4. In `index()`: parse the bounds, `group` and the sort list, build `filters`, the header links and `groups`, and count the statuses. (Done)
5. Rewrite the template (§5.6). (Done)
6. Write the tests, then run them locally and in Docker. (Done — 80/80 in both)
7. Repeat the timing check with 10.3k synthetic rows, both ungrouped and grouped. (Done — 4.50 MB, under 0.41 s)
8. Check the page in a browser at desktop and phone width. (Done — headless Chrome screenshots)
9. Do the security check. (Done)
#### Acceptance Criteria
- Expected distances for the fixture AAPL (open 100, year 90–110, ATH 150, ATL 80): below Year High 9.1%, above Year Low 11.1%, below ATH 33.3%, above ATL 25.0%.
- `?min_pct_all_time_max=30` includes AAPL. `?min_pct_all_time_max=34` excludes it.
- `?max_pct_year_min=11.2` includes AAPL. `?max_pct_year_min=11` excludes it.
- A min and a max on the same distance work together as a range. Bounds on different distances are combined with AND.
- `?min_pct_year_max=abc`, `=nan`, `=inf` and `=` are each ignored: the page returns 200 and shows all rows.
- A row with NULL `year_max` (XYZ_EQ) shows "—" and is excluded by any bound on `pct_year_max`. It sorts last whether ascending or descending.
- Every distance column has a header sort link (the existing `test_every_column_header_is_a_sort_link` covers this through `ALLOWED_SORTS`).
- Sort links keep `status`, `q`, `country`, `group` and all `min_*`/`max_*` values.
- `?group=pct_all_time_max` shows the range header rows in the fixed order, with correct counts and no empty ranges. `?group=bogus` shows the table ungrouped.
- `?sort=bogus` shows the ▲ marker on Ticker.
- Multi-column sorting:
  - `?sort=year_max&dir=asc&sort=name&dir=desc` orders by `year_max` ascending, and rows with the same `year_max` by `name` descending. A fixture pair with equal `year_max` is added for this.
  - Three levels work. A 4th level is ignored.
  - An invalid column or direction in any level is dropped or becomes `asc`, with no error. A duplicate column is used only once.
  - NULLs are last at every level.
  - The old single-sort URL `?sort=ticker&dir=desc` still works.
  - Header links: clicking the primary column flips only its direction. Clicking another column makes it level 1 and moves the others down. Links keep every sort level and every filter.
  - The level markers (`▼1`, `▲2`) appear on the right headers.
  - The 3 sort controls are pre-filled from the URL, so Apply keeps the sort.
- The page includes the pinned Bootstrap `<link>` with its `integrity` attribute, and has no `<script>` tag.
- All existing tests pass, including the XSS escaping test.
- With 10.3k rows, `GET /` takes under 2 s, both grouped and ungrouped, and with 3 sort levels on distance columns. The new HTML size is recorded.
#### Verification
- `python3 -m unittest -v`
- `docker compose run --rm app python -m unittest`
- `docker compose up -d --build`, then a manual browser check of filters, sorting, grouping and the phone-width layout.
#### Security Check
See §6. Grep the diff for `|safe`, f-strings in SQL that contain request values, and any new `<script>`.

## 8. Decisions taken (2026-11-14)
- Template: plain Bootstrap 5.3.3 from a CDN with SRI, with no Bootstrap JS.
- Filters: both min and max bounds, both including the limit. All filters can be combined.
- Sorting: up to 3 columns at once, set from the filter card or by clicking headers.
- Current price = `opening_price`.

## 9. Open Decisions (user)
0. Should a follow-up phase clean the impossible prices coming from yfinance (see the Phase 5 rework notes)?
1. Are the ranges `<0 / 0–10 / 10–25 / 25–50 / 50–100 / ≥100 %` right, or do you want different ones?
2. Should Bootstrap be bundled locally (served by Flask from `static/`) so the page is styled without internet? This would add one file and one Dockerfile `COPY`.
