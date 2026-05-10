from pathlib import Path
import csv
import asyncio
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import rt_database_builder_playwright as builder


def test_is_canonical_movie_url_accepts_expected_shape():
    assert builder.is_canonical_movie_url("https://www.rottentomatoes.com/m/example_movie")


def test_is_canonical_movie_url_rejects_non_canonical_paths():
    assert not builder.is_canonical_movie_url("https://www.rottentomatoes.com/m/example_movie/pictures")
    assert not builder.is_canonical_movie_url("https://www.rottentomatoes.com/tv/example_show")
    assert not builder.is_canonical_movie_url("https://www.rottentomatoes.com/m/example_trailer")


def test_parse_fast_extracts_core_fields_from_html():
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Movie",
          "name": "Example Movie (2021)",
          "datePublished": "2021-01-01",
          "genre": ["Drama", "Action"]
        }
        </script>
      </head>
      <body>
        <score-board tomatometerscore="91" audiencescore="88" releaseyear="2021"></score-board>
      </body>
    </html>
    """
    row = builder.parse_fast(html, "https://www.rottentomatoes.com/m/example_movie")

    assert row.title == "Example Movie"
    assert row.year == 2021
    assert row.tomatometer == 91
    assert row.audience_score == 88
    assert row.genres == "Action, Drama"


def test_should_keep_applies_filters_consistently():
    row = builder.MovieRow(
        title="Example",
        year=2019,
        genres="Drama",
        tomatometer=85,
        audience_score=81,
        url="https://www.rottentomatoes.com/m/example",
    )

    assert builder.should_keep(row, min_year=2018, min_tomato=80)
    assert not builder.should_keep(row, min_year=2020, min_tomato=80)
    assert not builder.should_keep(row, min_year=2018, min_tomato=90)


def test_should_keep_applies_min_audience():
    row = builder.MovieRow(
        title="Example",
        year=2019,
        genres="Drama",
        tomatometer=85,
        audience_score=81,
        url="https://www.rottentomatoes.com/m/example",
    )

    assert builder.should_keep(row, min_year=None, min_tomato=None, min_audience=80)
    assert not builder.should_keep(row, min_year=None, min_tomato=None, min_audience=82)


def test_parse_fast_keeps_audience_score_of_one_percent():
    html = """
    <html>
        <head>
            <script type="application/ld+json">
            {"@type": "Movie", "name": "One Percent", "datePublished": "2020-01-01"}
            </script>
        </head>
        <body>
            <score-board tomatometerscore="52" audiencescore="1" releaseyear="2020"></score-board>
        </body>
    </html>
    """
    row = builder.parse_fast(html, "https://www.rottentomatoes.com/m/one_percent")
    assert row.audience_score == 1


def test_discover_movie_sitemaps_returns_empty_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(builder, "_get", lambda *args, **kwargs: None)
    assert builder.discover_movie_sitemaps() == []


def test_write_rankings_handles_missing_required_columns(tmp_path):
    out_csv = tmp_path / "broken.csv"
    out_csv.write_text("title,url\nAlpha,https://www.rottentomatoes.com/m/alpha\n", encoding="utf-8")

    builder.write_rankings(str(out_csv))

    assert not (tmp_path / "broken__by_tomatometer.csv").exists()
    assert not (tmp_path / "broken__by_audience.csv").exists()


def test_row_needs_heavy_detects_missing_fields_for_active_filters():
    row = builder.MovieRow(
        title="Needs Heavy",
        year=None,
        genres="Drama",
        tomatometer=None,
        audience_score=None,
        url="https://www.rottentomatoes.com/m/needs_heavy",
    )

    assert builder.row_needs_heavy(row, min_year=2000, min_tomato=70, min_audience=60)


def test_row_needs_heavy_false_for_complete_row():
    row = builder.MovieRow(
        title="Stable",
        year=2020,
        genres="Drama",
        tomatometer=90,
        audience_score=87,
        url="https://www.rottentomatoes.com/m/stable",
    )

    assert not builder.row_needs_heavy(row, min_year=2000, min_tomato=70, min_audience=60)


def test_append_rows_upserts_existing_url(tmp_path):
    out_csv = tmp_path / "rt_movies.csv"

    builder.append_rows(
        str(out_csv),
        [
            builder.MovieRow(
                title="Alpha",
                year=2020,
                genres="Drama",
                tomatometer=90,
                audience_score=70,
                url="https://www.rottentomatoes.com/m/alpha",
            )
        ],
    )

    builder.append_rows(
        str(out_csv),
        [
            builder.MovieRow(
                title="Alpha Updated",
                year=2020,
                genres="Drama",
                tomatometer=95,
                audience_score=75,
                url="https://www.rottentomatoes.com/m/alpha",
            )
        ],
    )

    with out_csv.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["title"] == "Alpha Updated"
    assert rows[0]["tomatometer"] == "95"


class _FakeRouteRequest:
    resource_type = "document"


class _FakeRoute:
    def __init__(self):
        self.request = _FakeRouteRequest()

    def abort(self):
        return None

    def continue_(self):
        return None


class _FakePage:
    def __init__(self, mode: str):
        self.mode = mode

    async def route(self, _pattern, handler):
        handler(_FakeRoute())

    async def set_extra_http_headers(self, _headers):
        return None

    async def goto(self, _url, wait_until=None, timeout=None):
        if self.mode == "error":
            raise RuntimeError("boom")
        if self.mode == "slow":
            await asyncio.sleep(0.05)
        return None

    async def wait_for_timeout(self, _ms):
        return None

    async def query_selector(self, _selector):
        return None

    async def query_selector_all(self, _selector):
        return []

    async def close(self):
        return None


class _FakeContext:
    def __init__(self, mode: str):
        self.mode = mode

    async def new_page(self):
        return _FakePage(self.mode)

    async def close(self):
        return None


class _FakeBrowser:
    def __init__(self, mode: str):
        self.mode = mode

    async def new_context(self, **_kwargs):
        return _FakeContext(self.mode)

    async def close(self):
        return None


class _FakeChromium:
    def __init__(self, mode: str):
        self.mode = mode

    async def launch(self, **_kwargs):
        return _FakeBrowser(self.mode)


class _FakePlaywright:
    def __init__(self, mode: str):
        self.chromium = _FakeChromium(mode)


class _FakePlaywrightManager:
    def __init__(self, mode: str):
        self.mode = mode

    async def __aenter__(self):
        return _FakePlaywright(self.mode)

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_fetch_with_browser_logs_extraction_failure(monkeypatch, capsys):
    monkeypatch.setattr(builder, "async_playwright", lambda: _FakePlaywrightManager("error"))

    url = "https://www.rottentomatoes.com/m/fail_case"
    result = asyncio.run(
        builder.fetch_with_browser(
            [url],
            per_page_timeout=100,
            max_concurrency=1,
            worker_timeout_sec=0.2,
            heavy_pass_timeout_sec=10,
        )
    )

    captured = capsys.readouterr()
    assert "Playwright extraction failed" in captured.out
    assert result[url]["tomatometer"] is None
    assert result[url]["audience_score"] is None


def test_fetch_with_browser_logs_worker_timeout(monkeypatch, capsys):
    monkeypatch.setattr(builder, "async_playwright", lambda: _FakePlaywrightManager("slow"))

    url = "https://www.rottentomatoes.com/m/slow_case"
    result = asyncio.run(
        builder.fetch_with_browser(
            [url],
            per_page_timeout=100,
            max_concurrency=1,
            worker_timeout_sec=0.01,
            heavy_pass_timeout_sec=10,
        )
    )

    captured = capsys.readouterr()
    assert "Playwright worker timeout" in captured.out
    assert result[url]["tomatometer"] is None
    assert result[url]["audience_score"] is None


def test_append_load_and_rankings_smoke(tmp_path):
    out_csv = tmp_path / "rt_movies.csv"

    rows = [
        builder.MovieRow(
            title="Alpha",
            year=2020,
            genres="Drama",
            tomatometer=95,
            audience_score=80,
            url="https://www.rottentomatoes.com/m/alpha",
        ),
        builder.MovieRow(
            title="Beta",
            year=2018,
            genres="Comedy",
            tomatometer=80,
            audience_score=92,
            url="https://www.rottentomatoes.com/m/beta",
        ),
        builder.MovieRow(
            title="Gamma",
            year=2017,
            genres="Action",
            tomatometer=None,
            audience_score=None,
            url="https://www.rottentomatoes.com/m/gamma",
        ),
    ]

    builder.append_rows(str(out_csv), rows)

    seen = builder.load_existing_urls(str(out_csv))
    assert seen == {
        "https://www.rottentomatoes.com/m/alpha",
        "https://www.rottentomatoes.com/m/beta",
        "https://www.rottentomatoes.com/m/gamma",
    }

    builder.write_rankings(str(out_csv))

    by_tomato = tmp_path / "rt_movies__by_tomatometer.csv"
    by_audience = tmp_path / "rt_movies__by_audience.csv"
    assert by_tomato.exists()
    assert by_audience.exists()

    with by_tomato.open("r", encoding="utf-8", newline="") as f:
        rows_t = list(csv.DictReader(f))
    with by_audience.open("r", encoding="utf-8", newline="") as f:
        rows_a = list(csv.DictReader(f))

    assert rows_t[0]["title"] == "Alpha"
    assert rows_a[0]["title"] == "Beta"
