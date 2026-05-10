#!/usr/bin/env python3
"""
Rotten Tomatoes (sitemap -> detail pages) scraper with Playwright fallback.

- Fast pass (requests + BeautifulSoup) parses title/year/genres/tomato/audience
  from the server-rendered HTML and embedded JSON.
- Heavy pass (Playwright headless) hydrates the page and reads the scoreboard,
  which is where Audience Score usually appears after JS runs.

Outputs:
  - <out>.csv (master)
  - <out>__by_tomatometer.csv
  - <out>__by_audience.csv

Examples:
  pip install playwright beautifulsoup4 requests
  playwright install chromium
  python rt_database_builder_playwright.py --out rt_movies.csv --limit 500 --workers 8 --min-year 1970 --min-tomato 60
"""

import sys
import argparse, csv, gzip, json, os, random, re, time, asyncio
from contextlib import contextmanager

# --- Critical Windows fix: use Proactor loop so asyncio subprocesses work (Playwright needs them) ---
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        # If this fails, we'll try again inside main() before running the coroutine.
        pass

from dataclasses import dataclass, asdict
from typing import Iterable, List, Optional, Set, Dict
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

# -------- Playwright (heavy) --------
try:
    from playwright.async_api import async_playwright
except Exception:
    async_playwright = None  # We'll error nicely if heavy mode is requested

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
    if not s: return None
    m = re.search(r"(\d{1,3})\s*%?", s)
    if not m: return None
    v = int(m.group(1))
    return v if 0 <= v <= 100 else None

def _year_from(s: Optional[str]) -> Optional[int]:
    if not s: return None
    m = re.search(r"(19|20)\d{2}", str(s))
    return int(m.group(0)) if m else None

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def _walk_json_scores(obj):
    """Yield (key_path, value) for ints 0..100 found anywhere in JSON blobs."""
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

def _looks_like_score_key(key_path: str) -> bool:
    """Be strict: only accept values from keys that look like score/percent/rating."""
    # Last segment match to avoid grabbing unrelated ints (e.g., flags, booleans).
    last = key_path.split("/")[-1]
    return any(tok in last for tok in ("score", "percent", "rating", "tomatometer"))

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
    xml = _get(SITEMAP_INDEX)
    if not xml:
        print("[warn] Could not fetch sitemap index.")
        return []
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        print("[warn] Could not parse sitemap index XML.")
        return []
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = [loc.text for loc in root.findall(".//sm:loc", ns) if loc is not None and loc.text]
    return [u for u in urls if "movie" in u and u.endswith((".xml", ".xml.gz"))]

def iterate_movie_urls(limit: int) -> Iterable[str]:
    maps = discover_movie_sitemaps()
    seen = set()
    count = 0

    import xml.etree.ElementTree as ET

    for sm in maps:
        content = _get(sm, want_text=False)
        if not content:
            continue
        if sm.endswith(".gz"):
            try:
                content = gzip.decompress(content)
            except OSError as e:
                print(f"[warn] Failed to decompress sitemap {sm}: {e}")
                pass
        if isinstance(content, (bytes, bytearray)):
            try:
                text = content.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                print(f"[warn] Skipping sitemap with invalid UTF-8: {sm}")
                continue
        else:
            text = content
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            print(f"[warn] Could not parse sitemap XML: {sm}")
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

# --------------------- page parsing (fast pass) ---------------------

def parse_fast(html: str, url: str) -> MovieRow:
    soup = BeautifulSoup(html, "html.parser")
    title = None
    year = None
    genres: List[str] = []
    tom = None
    aud = None
    is_movie_page: Optional[bool] = None

    # JSON-LD
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

    # <score-board> (server-side attrs sometimes present)
    sb = soup.find("score-board")
    if sb:
        tom = _to_int(sb.get("tomatometerscore")) or _to_int(sb.get("tomatometer")) or tom
        aud = _to_int(sb.get("audiencescore")) or aud
        if not year:
            year = _year_from(sb.get("releaseyear") or sb.get("year"))

    # Movie info (classic & data-qa)
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
            genres += [x.strip() for x in re.split(r",\s*", val) if x.strip()]
        if not year and any(k in l for k in ("release date", "premiere", "original release", "theatrical release")):
            year = _year_from(val)

    if not year:
        hint = soup.find(attrs={"data-qa": re.compile(r"scoreboard-info", re.I)})
        if hint:
            year = _year_from(hint.get_text(" ", strip=True))

    # Slug fallback
    if not year:
        mslug = re.search(r"/m/[^/\s]*[_\-]((?:19|20)\d{2})(?:$|[/?#])", url)
        if mslug:
            year = int(mslug.group(1))

    # Embedded JSON fallbacks (tightened so we don't pick up booleans/flags)
    if aud is None or tom is None:
        aud_patterns = [
            r'"audienceScore"\s*:\s*\{[^}]*"(?:score|value|percent|rating)"\s*:\s*(\d+)',
            r'"audienceAll"\s*:\s*\{[^}]*"(?:score|value|percent|rating)"\s*:\s*(\d+)',
            r'"audienceVerified"\s*:\s*\{[^}]*"(?:score|value|percent|rating)"\s*:\s*(\d+)',
            r'"audience(?:Score)?Percent"\s*:\s*(\d+)',
            r'"audience(?:Score)?Rating"\s*:\s*(\d+)',
        ]
        tom_patterns = [
            r'"tomatometerScore"\s*:\s*\{[^}]*"(?:score|value|percent|rating)"\s*:\s*(\d+)',
            r'"tomatometerAll"\s*:\s*\{[^}]*"(?:score|value|percent|rating)"\s*:\s*(\d+)',
            r'"tomatometer(?:Score)?Percent"\s*:\s*(\d+)',
            r'"tomatometer(?:Score)?Rating"\s*:\s*(\d+)',
        ]
        if aud is None:
            for pat in aud_patterns:
                m = re.search(pat, html, flags=re.I)
                if m:
                    aud = _to_int(m.group(1))
                    if aud is not None: break
        if tom is None:
            for pat in tom_patterns:
                m = re.search(pat, html, flags=re.I)
                if m:
                    tom = _to_int(m.group(1))
                    if tom is not None: break

    # Last-resort JSON: only accept keys that *look like a score field*
    if aud is None or tom is None:
        for js in soup.find_all("script", attrs={"type": "application/json"}):
            raw = (js.string or js.get_text() or "").strip()
            if not raw: continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            for key_path, val in _walk_json_scores(data):
                if _looks_like_score_key(key_path):
                    if aud is None and ("audience" in key_path or "popcorn" in key_path):
                        aud = int(val)
                    if tom is None and ("tomatometer" in key_path or "critic" in key_path):
                        tom = int(val)
                if aud is not None and tom is not None:
                    break
            if aud is not None and tom is not None:
                break

    # Titles sometimes include the year in parens; strip it
    if title:
        m = re.search(r"^(.*)\s+\((19|20)\d{2}\)$", title)
        if m:
            title = m.group(1).strip()

    # Normalize genres
    genres_str = ", ".join(sorted(set([g for g in genres if g])))

    # Treat obviously bogus audience values (e.g., 0 or 1) as missing so we trigger heavy pass
    if aud is not None and aud == 0:
        aud = None

    if is_movie_page is False:
        # Explicitly not a movie page
        return MovieRow(title=title or "", year=year, genres=genres_str,
                        tomatometer=tom, audience_score=aud, url=url)
    return MovieRow(title=title or "", year=year, genres=genres_str,
                    tomatometer=tom, audience_score=aud, url=url)

# --------------------- Playwright heavy pass ---------------------

async def fetch_with_browser(
    urls: List[str],
    per_page_timeout: int = 20000,
    max_concurrency: int = 4,
    heavy_pass_timeout_sec: float = 300.0,
    worker_timeout_sec: Optional[float] = None,
) -> Dict[str, Dict[str, Optional[int]]]:
    """
    Visit each URL in a real browser. Return mapping:
      url -> {"tomatometer": int|None, "audience_score": int|None, "year": int|None, "genres": "a, b"}
    Only fields we can reliably read will be set; others remain None.
    """
    if async_playwright is None:
        raise RuntimeError("Playwright not available. Install with: pip install playwright && playwright install chromium")

    results: Dict[str, Dict[str, Optional[int]]] = {}
    sem = asyncio.Semaphore(max_concurrency)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1.0, float(heavy_pass_timeout_sec))
    worker_timeout = worker_timeout_sec if worker_timeout_sec is not None else max(10.0, per_page_timeout / 1000.0 + 5.0)

    def empty_result() -> Dict[str, Optional[int]]:
        return {"tomatometer": None, "audience_score": None, "year": None, "genres": None}

    async def grab(page, url: str) -> Dict[str, Optional[int]]:
        await page.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "media", "font"} else route.continue_())
        await page.set_extra_http_headers({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        await page.goto(url, wait_until="domcontentloaded", timeout=per_page_timeout)
        # Give hydration a moment, but bail if nothing changes
        await page.wait_for_timeout(1200)
        # Prefer reading attributes on <score-board> (usually set post-hydration)
        sb = await page.query_selector("score-board")
        tom = aud = None
        year = None
        genres_str = None
        if sb:
            t1 = await sb.get_attribute("tomatometerscore")
            t2 = await sb.get_attribute("tomatometer")
            a1 = await sb.get_attribute("audiencescore")
            tom = _to_int(t1) or _to_int(t2)
            aud = _to_int(a1)
            # Year sometimes present
            year = _year_from(await sb.get_attribute("releaseyear") or await sb.get_attribute("year"))

        # data-qa text hooks (visible text like "87%")
        if aud is None:
            el = await page.query_selector('[data-qa*="audience-score"], [data-qa="score-panel-audience-score"]')
            if el:
                txt = _clean(await el.inner_text())
                aud = _int_from_text(txt)

        if tom is None:
            el = await page.query_selector('[data-qa*="tomatometer"], [data-qa="tomatometer"]')
            if el:
                txt = _clean(await el.inner_text())
                tom = _int_from_text(txt)

        # Movie Info (data-qa structure)
        rows = await page.query_selector_all('[data-qa="movie-info-item"]')
        if rows:
            genres_found: List[str] = []
            for r in rows:
                lab_el = await r.query_selector('[data-qa="movie-info-item-label"]')
                val_el = await r.query_selector('[data-qa="movie-info-item-value"]')
                lab = _clean(await lab_el.inner_text()) if lab_el else ""
                val = _clean(await val_el.inner_text()) if val_el else ""
                l = lab.lower()
                if "genre" in l and val:
                    genres_found += [x.strip() for x in re.split(r",\s*", val) if x.strip()]
                if year is None and any(k in l for k in ("release date", "premiere", "original release", "theatrical release")):
                    year = _year_from(val)
            if genres_found:
                genres_str = ", ".join(sorted(set(genres_found)))

        # Fallback: inspect JSON in DOM (post-hydration) — strict key matching
        if aud is None or tom is None:
            scripts = await page.query_selector_all('script[type="application/json"]')
            for s in scripts:
                try:
                    raw = _clean(await s.inner_text())
                except Exception:
                    continue
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                for key_path, val in _walk_json_scores(data):
                    if _looks_like_score_key(key_path):
                        if aud is None and ("audience" in key_path or "popcorn" in key_path):
                            aud = int(val)
                        if tom is None and ("tomatometer" in key_path or "critic" in key_path):
                            tom = int(val)
                    if aud is not None and tom is not None:
                        break
                if aud is not None and tom is not None:
                    break

        # Normalize suspicious audience scores
        if aud is not None and aud == 0:
            aud = None

        return {"tomatometer": tom, "audience_score": aud, "year": year, "genres": genres_str}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"])
        ctx = await browser.new_context(user_agent=UA, locale="en-US", timezone_id="UTC")
        try:
            async def worker(u: str):
                if loop.time() > deadline:
                    print(f"[warn] Heavy pass global timeout reached; skipping {u}")
                    results[u] = empty_result()
                    return
                async with sem:
                    page = await ctx.new_page()
                    try:
                        out = await asyncio.wait_for(grab(page, u), timeout=worker_timeout)
                    except asyncio.TimeoutError:
                        print(f"[warn] Playwright worker timeout for {u}")
                        out = empty_result()
                    except Exception as e:
                        print(f"[warn] Playwright extraction failed for {u}: {type(e).__name__}: {e}")
                        out = empty_result()
                    finally:
                        await page.close()
                results[u] = out

            await asyncio.gather(*(worker(u) for u in urls))
        finally:
            await ctx.close()
            await browser.close()

    return results

# --------------------- I/O ---------------------

def load_existing_urls(out_csv: str) -> Set[str]:
    if not os.path.exists(out_csv): return set()
    urls = set()
    with open(out_csv, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            u = (row.get("url") or "").strip()
            if u: urls.add(u)
    return urls


@contextmanager
def _file_lock(path: str, timeout_sec: float = 30.0, poll_sec: float = 0.1):
    lock_path = f"{path}.lock"
    start = time.time()
    fd = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            if (time.time() - start) >= timeout_sec:
                raise TimeoutError(f"Timed out waiting for file lock: {lock_path}")
            time.sleep(poll_sec)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass

def append_rows(path: str, rows: List[MovieRow]):
    if not rows:
        return

    fieldnames = ["title", "year", "genres", "tomatometer", "audience_score", "url"]
    tmp_path = f"{path}.tmp"

    with _file_lock(path):
        by_url: Dict[str, Dict[str, object]] = {}

        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", newline="") as in_f:
                reader = csv.DictReader(in_f)
                for existing in reader:
                    u = (existing.get("url") or "").strip()
                    if not u:
                        continue
                    by_url[u] = {
                        "title": existing.get("title", ""),
                        "year": _to_optional_int(existing.get("year")),
                        "genres": existing.get("genres", ""),
                        "tomatometer": _to_optional_int(existing.get("tomatometer")),
                        "audience_score": _to_optional_int(existing.get("audience_score")),
                        "url": u,
                    }

        for row in rows:
            by_url[row.url] = asdict(row)

        with open(tmp_path, "w", encoding="utf-8", newline="") as out_f:
            writer = csv.DictWriter(out_f, fieldnames=fieldnames)
            writer.writeheader()
            for value in by_url.values():
                writer.writerow(value)

        os.replace(tmp_path, path)


def _to_optional_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return None

def write_rankings(out_csv: str):
    if not os.path.exists(out_csv):
        print(f"[rank] No dataset at {out_csv}"); return
    rows = []
    with open(out_csv, "r", encoding="utf-8", newline="") as f:
        rdr = csv.DictReader(f)
        required = {"title", "year", "genres", "tomatometer", "audience_score", "url"}
        fieldnames = set(rdr.fieldnames or [])
        missing = sorted(required - fieldnames)
        if missing:
            print(f"[rank] Missing required columns in {out_csv}: {', '.join(missing)}")
            return
        for r in rdr:
            rows.append({
                "title": r.get("title", ""),
                "year": _to_optional_int(r.get("year")),
                "genres": r.get("genres", ""),
                "tomatometer": _to_optional_int(r.get("tomatometer")),
                "audience_score": _to_optional_int(r.get("audience_score")),
                "url": r.get("url", ""),
            })
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

def should_keep(row: MovieRow, min_year: Optional[int], min_tomato: Optional[int], min_audience: Optional[int] = None) -> bool:
    if min_year is not None and (row.year is None or row.year < min_year):
        return False
    if min_tomato is not None and (row.tomatometer is None or row.tomatometer < min_tomato):
        return False
    if min_audience is not None and (row.audience_score is None or row.audience_score < min_audience):
        return False
    return True


def row_needs_heavy(row: MovieRow, min_year: Optional[int], min_tomato: Optional[int], min_audience: Optional[int] = None) -> bool:
    if row.audience_score is None or row.audience_score == 0:
        return True
    if min_year is not None and row.year is None:
        return True
    if min_tomato is not None and row.tomatometer is None:
        return True
    if min_audience is not None and row.audience_score is None:
        return True
    return False

async def main_async(args):
    already = load_existing_urls(args.out)
    print(f"[init] Resuming with {len(already)} URLs already in {args.out}.")
    urls = [u for u in iterate_movie_urls(args.limit) if u not in already]
    print(f"[discover] New candidates: {len(urls)} (total known {len(urls)+len(already)}).")

    # 1) FAST PASS (requests+BS)
    rows_buf: List[MovieRow] = []
    checkpointed_urls: Set[str] = set()
    checkpoint_buf: List[MovieRow] = []
    done = 0

    def flush_checkpoint_buffer(force: bool = False):
        if args.checkpoint_every <= 0:
            return
        if not checkpoint_buf:
            return
        if not force and len(checkpoint_buf) < args.checkpoint_every:
            return

        append_rows(args.out, checkpoint_buf)
        for r in checkpoint_buf:
            checkpointed_urls.add(r.url)
        print(f"[checkpoint] wrote {len(checkpoint_buf)} stable rows to {args.out}")
        checkpoint_buf.clear()

    def fetch_fast(u: str) -> Optional[MovieRow]:
        html = _get(u)
        if not html: return None
        try:
            return parse_fast(html, u)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(fetch_fast, u) for u in urls]
        for f in as_completed(futs):
            done += 1
            row = f.result()
            if row and row.title:
                rows_buf.append(row)
                if should_keep(row, args.min_year, args.min_tomato, args.min_audience) and not row_needs_heavy(row, args.min_year, args.min_tomato, args.min_audience):
                    checkpoint_buf.append(row)
                    flush_checkpoint_buffer()
            if done % 100 == 0:
                print(f"[progress fast] parsed {done}/{len(urls)} (+{len(rows_buf)} ready)")

    flush_checkpoint_buffer(force=True)

    # 2) Identify rows needing heavy pass
    need_heavy: List[str] = []
    for r in rows_buf:
        # Heavy only if audience missing/suspicious or (min filters require data we lack)
        if row_needs_heavy(r, args.min_year, args.min_tomato, args.min_audience):
            need_heavy.append(r.url)

    print(f"[heavy] To visit with browser: {len(need_heavy)}")

    # 3) HEAVY PASS (Playwright) – fill missing bits
    if need_heavy:
        updated = await fetch_with_browser(
            need_heavy,
            per_page_timeout=args.per_page_timeout,
            max_concurrency=args.heavy_concurrency,
            heavy_pass_timeout_sec=args.heavy_pass_timeout_sec,
        )
        m = {r.url: r for r in rows_buf}
        for u, vals in updated.items():
            r = m.get(u)
            if not r: continue
            if r.tomatometer is None and vals.get("tomatometer") is not None:
                r.tomatometer = vals["tomatometer"]
            if (r.audience_score is None or r.audience_score == 0) and vals.get("audience_score") is not None:
                r.audience_score = vals["audience_score"]
            if r.year is None and vals.get("year") is not None:
                r.year = vals["year"]
            if (not r.genres) and vals.get("genres"):
                r.genres = vals["genres"]

    # 4) FILTER, WRITE
    kept = [r for r in rows_buf if should_keep(r, args.min_year, args.min_tomato, args.min_audience)]
    pending_write = [r for r in kept if r.url not in checkpointed_urls]
    if pending_write:
        append_rows(args.out, pending_write)
        print(f"[write] wrote {len(pending_write)} rows to {args.out}")

    print("[done] scrape complete.")
    write_rankings(args.out)

def main():
    ap = argparse.ArgumentParser(description="RT sitemap-based scraper with Playwright fallback.")
    ap.add_argument("--out", default="rt_movies.csv", help="Master CSV path")
    ap.add_argument("--limit", type=int, default=0, help="Limit number of movie URLs (0 = all)")
    ap.add_argument("--workers", type=int, default=8, help="Threads for fast pass")
    ap.add_argument("--min-year", type=int, default=None, help="Keep only movies released on/after this year")
    ap.add_argument("--min-tomato", type=int, default=None, help="Keep only movies with Tomatometer >= this")
    ap.add_argument("--min-audience", type=int, default=None, help="Keep only movies with Audience Score >= this")
    ap.add_argument("--per-page-timeout", type=int, default=20000, help="Playwright per-page timeout (ms)")
    ap.add_argument("--heavy-concurrency", type=int, default=4, help="Concurrent Playwright tabs")
    ap.add_argument("--heavy-pass-timeout-sec", type=float, default=300.0, help="Global timeout budget for heavy pass (seconds)")
    ap.add_argument("--checkpoint-every", type=int, default=200, help="Checkpoint stable rows every N writes during fast pass (0 disables)")
    args = ap.parse_args()

    if async_playwright is None:
        print("[warn] Playwright not installed. We'll still run fast pass; audience score may be missing.")
        print("       To enable heavy pass: pip install playwright && playwright install chromium")

    # Ensure Proactor policy right before running async (some environments override policies).
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n[abort] interrupted by user", file=sys.stderr)


if __name__ == "__main__":
    main()
