#!/usr/bin/env python3
"""
rt_dataset_builder.py
Build a Rotten Tomatoes dataset with title, year, genres, Tomatometer, Audience score, and URL.
- Enumerates movie URLs via the internal search endpoint for 0-9 + A-Z.
- Scrapes detail pages with multiple fallbacks.
- **Resumes** from an existing CSV (skips already present URLs).
- Produces three files:
    1) --out (master dataset; unsorted, deduped)
    2) <stem>__by_tomatometer.csv (desc)
    3) <stem>__by_audience.csv (desc)

Usage:
  python rt_dataset_builder.py --out movies_rt.csv --max-pages 50 --workers 8
  # Resume later with the same --out; it will skip already-scraped URLs.

Requires:
  pip install requests beautifulsoup4
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

# ----------------------------- Config ---------------------------------

SEARCH_ENDPOINT = "https://www.rottentomatoes.com/api/private/v2.0/search"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/125.0.0.0 Safari/537.36")

PER_REQUEST_SLEEP = (0.25, 0.8)   # polite pause between requests
MAX_RETRIES = 3
RETRY_SLEEP_BASE = 1.5            # exponential backoff base (seconds)
TIMEOUT = 20

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
})
# requests doesn't support per-session timeout; pass per-call.


# ----------------------------- Data model -----------------------------

@dataclass
class MovieRow:
    title: str
    year: Optional[int]
    genres: str  # pipe-separated
    tomatometer: Optional[int]
    audience_score: Optional[int]
    url: str


# ----------------------------- Utils ----------------------------------

def _sleep_brief():
    time.sleep(random.uniform(*PER_REQUEST_SLEEP))

def _backoff_sleep(i: int):
    # i = 0,1,2,...
    time.sleep(RETRY_SLEEP_BASE * (2 ** i) + random.uniform(0, 0.5))

def _to_int(x: Optional[str]) -> Optional[int]:
    if x is None:
        return None
    m = re.search(r"(\d{1,3})", str(x))
    if not m:
        return None
    try:
        v = int(m.group(1))
        return v if 0 <= v <= 100 else None
    except ValueError:
        return None

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def _coerce_year(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    m = re.search(r"(19|20)\d{2}", str(v))
    return int(m.group(0)) if m else None


# ----------------------------- I/O ------------------------------------

def load_existing_urls(out_csv: str) -> Set[str]:
    urls = set()
    if not os.path.exists(out_csv):
        return urls
    with open(out_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            u = (row.get("url") or "").strip()
            if u:
                urls.add(u)
    return urls

def append_rows(out_csv: str, rows: List[MovieRow], header_written: bool):
    # Write header if file did not exist
    write_header = not header_written and not os.path.exists(out_csv)
    with open(out_csv, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["title", "year", "genres", "tomatometer", "audience_score", "url"])
        if write_header:
            w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


# ----------------------------- HTTP -----------------------------------

def _get_json(url: str, params: Optional[dict] = None) -> Optional[dict]:
    for i in range(MAX_RETRIES):
        try:
            _sleep_brief()
            r = SESSION.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            # soft backoff on 429/5xx
            if r.status_code in (429, 500, 502, 503, 504):
                _backoff_sleep(i)
                continue
            return None
        except requests.RequestException:
            _backoff_sleep(i)
    return None

def _get_html(url: str) -> Optional[str]:
    for i in range(MAX_RETRIES):
        try:
            _sleep_brief()
            r = SESSION.get(url, timeout=TIMEOUT)
            if r.status_code == 200 and r.text:
                return r.text
            if r.status_code in (429, 500, 502, 503, 504):
                _backoff_sleep(i)
                continue
            return None
        except requests.RequestException:
            _backoff_sleep(i)
    return None


# ----------------------------- Discovery ------------------------------

def iterate_search_movies(max_pages_per_letter: int) -> Iterable[Tuple[str, str]]:
    alphabet = [str(d) for d in range(10)] + [chr(c) for c in range(ord('a'), ord('z') + 1)]
    for q in alphabet:
        for page in range(1, max_pages_per_letter + 1):
            data = _get_json(SEARCH_ENDPOINT, params={"q": q, "page": page})
            if not data or not data.get("movies"):
                break
            yielded = 0
            for item in data["movies"]:
                typ = (item.get("type") or item.get("ogType") or item.get("contentType") or "").lower()
                if "movie" not in typ:
                    continue
                url = item.get("url") or item.get("url__") or item.get("relativeUrl")
                title = item.get("name") or item.get("title") or item.get("movieTitle")
                if not url or not title:
                    continue
                if url.startswith("/"):
                    url = urljoin("https://www.rottentomatoes.com", url)
                yielded += 1
                yield (_clean(title), url)
            if yielded == 0:
                break


# ----------------------------- Parsing --------------------------------

def parse_movie_detail(html: str, url: str) -> MovieRow:
    soup = BeautifulSoup(html, "html.parser")

    title = None
    year = None
    genres: List[str] = []
    tom = None
    aud = None

    # JSON-LD
    try:
        for tag in soup.select('script[type="application/ld+json"]'):
            raw = tag.string or ""
            if not raw.strip():
                continue
            ld = json.loads(raw)
            blocks = ld if isinstance(ld, list) else [ld]
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("@type") in ("Movie", "CreativeWork"):
                    title = title or b.get("name")
                    year = year or _coerce_year(b.get("datePublished") or b.get("releaseDate") or b.get("dateCreated"))
                    g = b.get("genre")
                    if isinstance(g, str):
                        genres += [ _clean(x) for x in g.split(",") if x.strip() ]
                    elif isinstance(g, list):
                        genres += [ _clean(x) for x in g if x ]
                    agg = b.get("aggregateRating")
                    if isinstance(agg, dict):
                        tom = tom or _to_int(agg.get("ratingValue"))
    except Exception:
        pass

    # score-board web component
    sb = soup.find("score-board")
    if sb:
        tom = _to_int(sb.get("tomatometerscore")) or tom or _to_int(sb.get("tomatometer"))
        aud = _to_int(sb.get("audiencescore")) or aud

    # Movie Info panel
    for li in soup.select("ul.content-meta.info li.meta-row"):
        lab_el = li.select_one(".meta-label")
        val_el = li.select_one(".meta-value")
        lab = _clean("".join(lab_el.stripped_strings)) if lab_el else ""
        val = _clean(" ".join(val_el.stripped_strings)) if val_el else ""
        l = lab.lower()
        if "genre" in l and val:
            genres += [x.strip() for x in val.split(",") if x.strip()]
        if ("release date" in l or "premiere" in l) and not year:
            year = _coerce_year(val)

    # Embedded JSON fallbacks
    mr = re.search(r"movieReview\s*=\s*(\{.*?\});", html, re.DOTALL)
    if mr:
        try:
            j = json.loads(mr.group(1))
            title = title or j.get("title") or j.get("movieTitle")
            year = year or _coerce_year(j.get("theaterReleaseDate") or j.get("dvdReleaseDate"))
        except Exception:
            pass

    fd = re.search(r"root\.RottenTomatoes\.context\.fandangoData\s*=\s*(\{.*?\});", html, re.DOTALL)
    if fd:
        try:
            j = json.loads(fd.group(1))
            title = title or j.get("name") or j.get("title")
            year = year or _coerce_year(j.get("releaseYear") or j.get("theaterReleaseDate") or j.get("streamingReleaseDate"))
        except Exception:
            pass

    # Fallback title
    if not title:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            title = _clean(h1.get_text())
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            title = _clean(og["content"])

    genres = "|".join(sorted(set([g for g in genres if g])))

    return MovieRow(
        title=title or "",
        year=year,
        genres=genres,
        tomatometer=tom,
        audience_score=aud,
        url=url
    )


# ----------------------------- Pipeline --------------------------------

def scrape_and_build(out_csv: str, max_pages: int, workers: int, flush_every: int = 200):
    # Load already-scraped URLs to support resume
    already = load_existing_urls(out_csv)
    header_written = os.path.exists(out_csv)
    print(f"[init] Resuming with {len(already)} URLs already in {out_csv}.")

    discovered = []
    seen = set(already)
    for title, url in iterate_search_movies(max_pages_per_letter=max_pages):
        if url not in seen:
            seen.add(url)
            discovered.append((title, url))

    print(f"[discover] New candidates: {len(discovered)} (total known {len(seen)}).")

    batch: List[MovieRow] = []
    done = 0

    def fetch(url: str) -> Optional[MovieRow]:
        html = _get_html(url)
        if not html:
            return None
        try:
            return parse_movie_detail(html, url)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch, url): url for _, url in discovered}
        for f in as_completed(futures):
            row = f.result()
            done += 1
            if row and row.title:
                batch.append(row)
            if done % 100 == 0:
                print(f"[progress] parsed {done}/{len(discovered)} (+{len(batch)} ready)")

            if len(batch) >= flush_every:
                append_rows(out_csv, batch, header_written)
                header_written = True
                print(f"[flush] wrote {len(batch)} rows -> {out_csv}")
                batch.clear()

    if batch:
        append_rows(out_csv, batch, header_written)
        print(f"[final flush] wrote {len(batch)} rows -> {out_csv}")

    print("[done] scrape complete.")


def write_rankings(out_csv: str):
    # Read master and emit two sorted leaderboards
    if not os.path.exists(out_csv):
        print(f"[rank] No dataset at {out_csv}")
        return

    rows = []
    with open(out_csv, "r", encoding="utf-8", newline="") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            # normalize ints
            for k in ("year", "tomatometer", "audience_score"):
                v = r.get(k)
                r[k] = int(v) if (v and v.isdigit()) else None
            rows.append(r)

    stem, ext = os.path.splitext(out_csv)
    by_tom = sorted(rows, key=lambda r: (r["tomatometer"] is None, -(r["tomatometer"] or -1), r["title"]))
    by_aud = sorted(rows, key=lambda r: (r["audience_score"] is None, -(r["audience_score"] or -1), r["title"]))

    def dump(path: str, data: List[dict]):
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["title", "year", "genres", "tomatometer", "audience_score", "url"])
            w.writeheader()
            for r in data:
                w.writerow({
                    "title": r["title"],
                    "year": r["year"] if r["year"] is not None else "",
                    "genres": r.get("genres", ""),
                    "tomatometer": r["tomatometer"] if r["tomatometer"] is not None else "",
                    "audience_score": r["audience_score"] if r["audience_score"] is not None else "",
                    "url": r["url"],
                })

    path_t = f"{stem}__by_tomatometer.csv"
    path_a = f"{stem}__by_audience.csv"
    dump(path_t, by_tom)
    dump(path_a, by_a)

    print(f"[rank] wrote:\n  - {path_t}\n  - {path_a}")


# ----------------------------- CLI ------------------------------------

def main():
    ap = argparse.ArgumentParser(description="RT dataset builder with resume + ranked CSV outputs.")
    ap.add_argument("--out", default="movies_rt.csv", help="Master dataset CSV path")
    ap.add_argument("--max-pages", type=int, default=50, help="Max search pages per letter/number (bigger => more coverage)")
    ap.add_argument("--workers", type=int, default=8, help="Concurrent workers")
    ap.add_argument("--flush-every", type=int, default=200, help="Append to CSV every N parsed rows")
    args = ap.parse_args()

    if args.max_pages > 120:
        print("[warn] Very high --max-pages can be slow and heavy; consider chunking.", file=sys.stderr)

    scrape_and_build(out_csv=args.out, max_pages=args.max_pages, workers=args.workers, flush_every=args.flush_every)
    write_rankings(args.out)

if __name__ == "__main__":
    main()
