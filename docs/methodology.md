# Methodology

## Goal

Build a CSV of Rotten Tomatoes movie pages with:

- `title`
- `year`
- `genres`
- `tomatometer`
- `audience_score`
- `url`

## Pipeline

### 1. Discover candidate movie pages

The scraper uses the Rotten Tomatoes sitemap index and filters for canonical movie URLs of the form:

`https://www.rottentomatoes.com/m/<slug>`

This avoids non-movie pages such as `/pictures`, trailers, and other non-canonical paths.

### 2. Fast parse from static HTML

The first pass reads:

- JSON-LD blocks
- the `score-board` component
- movie info rows
- embedded JSON fragments

This pass is usually enough for titles, years, genres, and many critic scores.

### 3. Browser fallback for missing scores

When audience scores or other fields are missing, the Playwright pass opens the page in a browser and reads hydrated DOM values.

### 4. Write ranked outputs

Each run produces:

- a master CSV
- a Tomatometer-ranked CSV
- an audience-ranked CSV

Optional CLI filters can constrain output rows by minimum year, Tomatometer score, and audience score.

## Reference Material

The repository also includes earlier collection variants that used:

- internal search endpoint discovery
- sitemap scraping without the browser-backed recovery step

Those scripts remain under `scripts/legacy/` for comparison and inspection.
