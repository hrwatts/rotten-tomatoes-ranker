#!/usr/bin/env python3
"""
Scrape Rotten Tomatoes by discovering movie pages via public sitemaps,
then parsing each page for title, year, genres, Tomatometer, Audience.

Outputs:
  - movies_rt.csv
  - movies_rt__by_tomatometer.csv
  - movies_rt__by_audience.csv

Usage:
  pip install requests beautifulsoup4
  python rt_database_builder_sitemap.py --out movies_rt.csv --limit 0 --workers 8
    --limit 0  => all movies
    --limit N  => first N movies (for testing)
"""

import argparse, csv, gzip, json, os, random, re, time
from dataclasses import dataclass, asdict
from typing import Iterable, List, Optional, Set
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

SITEMAP_INDEX = "https://www.rottentomatoes.com/sitemap.xml"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/125.0.0.0 Safari/537.36")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
})
TIMEOUT = 25
PER_REQUEST_SLEEP = (0.25, 0.85)
MAX_RETRIES = 3
RETRY_BASE = 1.5  # seconds

@dataclass
class MovieRow:
    title: str
    year: Optional[int]
    genres: str  # comma-separated
    tomatometer: Optional[int]
    audience_score: Optional[int]
    url: str

# --------------------- helpers ---------------------

def _sleep_brief():
    time.sleep(random.uniform(*PER_REQUEST_SLEEP))

def _backoff(i: int):
    time.sleep(RETRY_BASE * (2 ** i) + random.uniform(0, 0.6))

def _get(url: str, want_text=True):
    for i in range(MAX_RETRIES):
        try:
            _sleep_brief()
            r = SESSION.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text if want_text else r.content
            if r.status_code in (429, 500, 502, 503, 504):
                _backoff(i); continue
            return None
        except requests.RequestException:
            _backoff(i)
    return None

def _to_int(s: Optional[str]) -> Optional[int]:
    if not s: return None
    m = re.search(r"(\d{1,3})", str(s))
    if not m: return None
    v = int(m.group(1))
    return v if 0 <= v <= 100 else None

def _int_from_text(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"(\d{1,3})", s)
    if not m:
        return None
    v = int(m.group(1))
    return v if 0 <= v <= 100 else None

def _year_from(s: Optional[str]) -> Optional[int]:
    if not s: return None
    m = re.search(r"(19|20)\d{2}", str(s))
    return int(m.group(0)) if m else None

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def _walk_json_scores(obj):
    """Yield (key_path, value) for ints 0..100 found anywhere in application/json blobs."""
    stack = [([], obj)]
    while stack:
        path, cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                stack.append((path + [k], v))
        elif isinstance(cur, list):
            for i, v in enumerate(cur):
                stack.append((path + [str(i)], v))
        else:
            if isinstance(cur, (int, float)):
                iv = int(cur)
                if 0 <= iv <= 100:
                    yield "/".join(path).lower(), iv

def is_canonical_movie_url(url: str) -> bool:
    """
    Keep only canonical movie pages: https://www.rottentomatoes.com/m/<slug>
    Excludes trailers, galleries, videos, /m/<slug>/pictures, etc.
    """
    p = urlparse(url)
    parts = [seg for seg in p.path.split("/") if seg]
    if len(parts) != 2:      # e.g., /m/adrift_2018/pictures -> 3 segments
        return False
    if parts[0] != "m":
        return False
    slug = parts[1]
    if slug.endswith(("_trailer",)):
        return False
    return True

# --------------------- sitemap discovery ---------------------

def discover_movie_sitemaps() -> List[str]:
    """Return URLs of child sitemaps that contain movie pages."""
    xml = _get(SITEMAP_INDEX)
    if not xml:
        return []
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = [loc.text for loc in root.findall(".//sm:loc", ns) if loc is not None and loc.text]
    # Keep items that look like movie sitemaps (often ...movie*.xml or .xml.gz)
    return [u for u in urls if "movie" in u and u.endswith((".xml", ".xml.gz"))]

def iterate_movie_urls(limit: int) -> Iterable[str]:
    """Yield canonical movie page URLs from all movie sitemaps."""
    maps = discover_movie_sitemaps()
    seen = set()
    count = 0

    import xml.etree.ElementTree as ET

    for sm in maps:
        content = _get(sm, want_text=False)
        if not content:
            continue

        # decompress if .gz
        if sm.endswith(".gz"):
            try:
                content = gzip.decompress(content)
            except OSError:
                pass

        text = content.decode("utf-8", errors="replace") if isinstance(content, (bytes, bytearray)) else content
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue

        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root.findall(".//sm:loc", ns):
            url = (loc.text or "").strip()
            if not url or not is_canonical_movie_url(url):
                continue
            if url not in seen:
                seen.add(url)
                yield url
                count += 1
                if limit and count >= limit:
                    return

# --------------------- page parsing ---------------------

def parse_movie_detail(html: str, url: str) -> Optional[MovieRow]:
    soup = BeautifulSoup(html, "html.parser")

    title = None
    year = None
    genres: List[str] = []
    tom = None
    aud = None
    is_movie_page: Optional[bool] = None  # set True/False when JSON-LD says so

    # --- JSON-LD (single dict, list, or @graph) ---
    def iter_jsonld():
        for tag in soup.select('script[type="application/ld+json"]'):
            raw = (tag.string or tag.get_text() or "").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            items = []
            if isinstance(data, dict) and isinstance(data.get("@graph"), list):
                items.extend(data["@graph"])
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):
                items.append(data)
            for b in items:
                if isinstance(b, dict):
                    yield b

    for b in iter_jsonld():
        t = b.get("@type")
        types = {t} if isinstance(t, str) else set(t or [])
        if "Movie" in types:
            is_movie_page = True
            title = title or b.get("name") or b.get("headline")
            for k in ("datePublished", "releaseDate", "dateCreated"):
                if not year:
                    year = _year_from(b.get(k))
            g = b.get("genre")
            if isinstance(g, str):
                genres += [x.strip() for x in g.split(",") if x.strip()]
            elif isinstance(g, list):
                genres += [x.strip() for x in g if x]
            agg = b.get("aggregateRating")
            if isinstance(agg, dict):
                tom = tom or _to_int(agg.get("ratingValue"))
        elif types:
            if any(tp in types for tp in ("TVSeries", "TVEpisode", "TVSeason")):
                is_movie_page = False

    # --- <score-board> attributes (sometimes empty in static HTML) ---
    sb = soup.find("score-board")
    if sb:
        tom = _to_int(sb.get("tomatometerscore")) or _to_int(sb.get("tomatometer")) or tom
        aud = _to_int(sb.get("audiencescore")) or aud
        # Try scoreboard year if present
        if not year:
            year = _year_from(sb.get("releaseyear") or sb.get("year"))

    # --- Movie Info blocks (classic + data-qa variants) ---
    for li in soup.select("ul.content-meta.info li.meta-row, li.meta-row, div.meta-row"):
        lab_el = li.select_one(".meta-label") or li.find(class_=re.compile(r"meta-label", re.I))
        val_el = li.select_one(".meta-value") or li.find(class_=re.compile(r"meta-value", re.I))
        lab = _clean("".join(lab_el.stripped_strings)) if lab_el else ""
        val = _clean(" ".join(val_el.stripped_strings)) if val_el else ""
        l = lab.lower()
        if "genre" in l and val:
            genres += [x.strip() for x in val.split(",") if x.strip()]
        if not year and any(k in l for k in ("release date", "premiere", "original release", "theatrical release")):
            year = _year_from(val)

    for row in soup.select('[data-qa="movie-info-item"]'):
        lab_el = row.select_one('[data-qa="movie-info-item-label"]')
        val_el = row.select_one('[data-qa="movie-info-item-value"]')
        lab = _clean(lab_el.get_text(" ", strip=True)) if lab_el else ""
        val = _clean(val_el.get_text(" ", strip=True)) if val_el else ""
        l = lab.lower()
        if "genre" in l and val:
            parts = [x.strip() for x in re.split(r",\s*", val) if x.strip()]
            genres += parts
        if not year and any(k in l for k in ("release date", "premiere", "original release", "theatrical release")):
            year = _year_from(val)

    # --- Extra year hints near scoreboard ---
    if not year:
        hint = soup.find(attrs={"data-qa": re.compile(r"scoreboard-info", re.I)})
        if hint:
            year = _year_from(hint.get_text(" ", strip=True))

    # --- Fallback: infer year from slug ..._YYYY ---
    if not year:
        mslug = re.search(r"/m/[^/\s]*[_\-]((?:19|20)\d{2})(?:$|[/?#])", url)
        if mslug:
            year = int(mslug.group(1))

    # --- Embedded JSON / DOM fallbacks for scores (multiple variants) ---

    # 1) Regex scan for common JSON layouts
    if aud is None or tom is None:
        aud_patterns = [
            r'"audienceScore"\s*:\s*\{[^}]*"(?:score|value|percent)"\s*:\s*(\d+)',
            r'"audienceAll"\s*:\s*\{[^}]*"(?:score|value|percent)"\s*:\s*(\d+)',
            r'"audienceVerified"\s*:\s*\{[^}]*"(?:score|value|percent)"\s*:\s*(\d+)',
            r'"audience(?:Score)?Percent"\s*:\s*(\d+)',
        ]
        tom_patterns = [
            r'"tomatometerScore"\s*:\s*\{[^}]*"(?:score|value|percent)"\s*:\s*(\d+)',
            r'"tomatometerAll"\s*:\s*\{[^}]*"(?:score|value|percent)"\s*:\s*(\d+)',
            r'"tomatometer(?:Score)?Percent"\s*:\s*(\d+)',
        ]

        if aud is None:
            for pat in aud_patterns:
                m = re.search(pat, html)
                if m:
                    try:
                        aud = int(m.group(1)); break
                    except ValueError:
                        pass

        if tom is None:
            for pat in tom_patterns:
                m = re.search(pat, html)
                if m:
                    try:
                        tom = int(m.group(1)); break
                    except ValueError:
                        pass

    # 2) Parse application/json blobs and walk all keys
    if aud is None or tom is None:
        for js in soup.find_all("script", attrs={"type": "application/json"}):
            raw = (js.string or js.get_text() or "").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            for key_path, val in _walk_json_scores(data):
                if aud is None and ("audience" in key_path or "popcorn" in key_path):
                    aud = int(val)
                if tom is None and ("tomatometer" in key_path or "critic" in key_path):
                    tom = int(val)
                if aud is not None and tom is not None:
                    break
            if aud is not None and tom is not None:
                break

    # 3) Read visible scoreboard text (data-qa hooks), e.g. "91%"
    if aud is None:
        el = soup.select_one('[data-qa*="audience-score"]') or soup.select_one('[data-qa="score-panel-audience-score"]')
        if el:
            aud = _int_from_text(el.get_text(" ", strip=True))
    if tom is None:
        el = soup.select_one('[data-qa*="tomatometer"]') or soup.select_one('[data-qa="tomatometer"]')
        if el:
            tom = _int_from_text(el.get_text(" ", strip=True))

    # --- Title cleanup: drop trailing "(YYYY)" if present ---
    if title:
        m = re.search(r"^(.*)\s+\((19|20)\d{2}\)$", title)
        if m:
            title = m.group(1).strip()

    # --- Normalize & return (skip explicit non-movie pages) ---
    genres = ", ".join(sorted(set([g for g in genres if g])))
    if is_movie_page is False:
        return None

    return MovieRow(
        title=title or "",
        year=year,
        genres=genres,
        tomatometer=tom,
        audience_score=aud,
        url=url,
    )

# --------------------- I/O ---------------------

def load_existing_urls(out_csv: str) -> Set[str]:
    if not os.path.exists(out_csv): return set()
    urls = set()
    with open(out_csv, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            u = (row.get("url") or "").strip()
            if u: urls.add(u)
    return urls

def append_rows(path: str, rows: List[MovieRow]):
    write_header = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["title","year","genres","tomatometer","audience_score","url"])
        if write_header: w.writeheader()
        for r in rows: w.writerow(asdict(r))

def write_rankings(out_csv: str):
    if not os.path.exists(out_csv):
        print(f"[rank] No dataset at {out_csv}"); return
    rows = []
    with open(out_csv, "r", encoding="utf-8", newline="") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            for k in ("year", "tomatometer", "audience_score"):
                v = r.get(k)
                r[k] = int(v) if v and str(v).isdigit() else None
            rows.append(r)
    stem, _ = os.path.splitext(out_csv)
    by_t = sorted(rows, key=lambda r: (r["tomatometer"] is None, -(r["tomatometer"] or -1), r["title"]))
    by_a = sorted(rows, key=lambda r: (r["audience_score"] is None, -(r["audience_score"] or -1), r["title"]))
    def dump(path, data):
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["title","year","genres","tomatometer","audience_score","url"])
            w.writeheader()
            for r in data:
                w.writerow({
                    "title": r["title"],
                    "year": r["year"] if r["year"] is not None else "",
                    "genres": r.get("genres",""),
                    "tomatometer": r["tomatometer"] if r["tomatometer"] is not None else "",
                    "audience_score": r["audience_score"] if r["audience_score"] is not None else "",
                    "url": r["url"],
                })
    tpath = f"{stem}__by_tomatometer.csv"
    apath = f"{stem}__by_audience.csv"
    dump(tpath, by_t); dump(apath, by_a)
    print(f"[rank] wrote:\n  - {tpath}\n  - {apath}")

# --------------------- pipeline ---------------------

def main():
    ap = argparse.ArgumentParser(description="RT sitemap-based scraper (resume + rankings).")
    ap.add_argument("--out", default="movies_rt.csv", help="Master CSV path")
    ap.add_argument("--limit", type=int, default=0, help="Limit number of movie URLs (0 = all)")
    ap.add_argument("--workers", type=int, default=8, help="Concurrent workers")
    ap.add_argument("--flush-every", type=int, default=200, help="Append every N parsed rows")
    args = ap.parse_args()

    already = load_existing_urls(args.out)
    print(f"[init] Resuming with {len(already)} URLs already in {args.out}.")
    urls = [u for u in iterate_movie_urls(args.limit) if u not in already]
    print(f"[discover] New candidates: {len(urls)} (total known {len(urls)+len(already)}).")

    rows_buf: List[MovieRow] = []
    done = 0

    def fetch(u: str) -> Optional[MovieRow]:
        html = _get(u)
        if not html: return None
        try:
            return parse_movie_detail(html, u)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(fetch, u) for u in urls]
        for f in as_completed(futs):
            done += 1
            row = f.result()
            if row and row.title:
                rows_buf.append(row)
            if done % 100 == 0:
                print(f"[progress] parsed {done}/{len(urls)} (+{len(rows_buf)} ready)")
            if len(rows_buf) >= args.flush_every:
                append_rows(args.out, rows_buf)
                print(f"[flush] wrote {len(rows_buf)} rows -> {args.out}")
                rows_buf.clear()

    if rows_buf:
        append_rows(args.out, rows_buf)
        print(f"[final flush] wrote {len(rows_buf)} rows -> {args.out}")

    print("[done] scrape complete.")
    write_rankings(args.out)

if __name__ == "__main__":
    main()
