# Limitations

## Scraping Fragility

This project depends on Rotten Tomatoes page structure and sitemap behavior. If the site changes its HTML, JSON payloads, or hydration logic, fields may degrade silently.

## Audience Score Coverage

Audience scores are harder to collect than critic scores because they are often exposed only after JavaScript runs. The Playwright fallback improves coverage, but it also increases runtime complexity and environment sensitivity.

## Environment Sensitivity

Browser automation is more environment-sensitive than the static HTML pass. For the most reliable results, run the main script directly rather than relying on notebook execution.

## Historical Output Quality

The historical outputs in `data/legacy/` include:

- non-canonical URLs
- missing critic scores
- missing audience scores

They are kept as history, not as a polished dataset.

## Terms and Rate Limits

Any real use of this scraper should consider site terms, request volume, and operational etiquette. The scripts already include light retry and sleep behavior, but that does not remove the underlying policy risk.
