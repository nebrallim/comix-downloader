import asyncio
import json
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_comix_api():
    try:
        import requests
    except ImportError:
        requests_stub = types.ModuleType("requests")

        class Session:
            def __init__(self):
                self.headers = {}

            def get(self, *args, **kwargs):
                raise AssertionError("requests.Session.get should not be called by these tests")

        requests_stub.Session = Session
        requests_stub.exceptions = types.SimpleNamespace(RequestException=Exception)
        sys.modules.setdefault("requests", requests_stub)

    module = importlib.import_module("src.api.comix")
    return module.ComixAPI


ComixAPI = load_comix_api()
comix_module = importlib.import_module("src.api.comix")


class ChallengePage:
    def __init__(self, *, clears: bool):
        self.challenge = True
        self.clears = clears

    async def evaluate(self, script, **kwargs):
        if script == "document.title":
            return "Just a moment..." if self.challenge else "Comix - Read Comics online for free"
        if "#challenge-running" in script:
            return self.challenge
        return not self.challenge

    async def sleep(self, _seconds):
        if self.clears:
            self.challenge = False


class StreamingInitialDataPage:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.index = 0

    async def evaluate(self, script, **kwargs):
        if "textContent" in script:
            return self.payloads[min(self.index, len(self.payloads) - 1)]
        raise AssertionError(f"Unexpected script: {script}")

    async def sleep(self, _seconds):
        self.index += 1
        await asyncio.sleep(0)


class ComixChapterTests(unittest.TestCase):
    def test_wait_for_initial_data_ignores_truncated_stream(self):
        manga_code = "mn6wm"
        complete = json.dumps({
            "queries": {
                '["manga", "detail", "mn6wm"]': {
                    "id": 42,
                    "hasChapters": True,
                }
            }
        })
        page = StreamingInitialDataPage([
            complete[:19],
            complete[:97],
            complete,
        ])

        result = asyncio.run(comix_module._wait_for_initial_data(
            page,
            operation="manga details",
            manga_code=manga_code,
            timeout=0.5,
        ))

        self.assertEqual(result["queries"]['["manga", "detail", "mn6wm"]']["id"], 42)
        self.assertTrue(ComixAPI._manga_has_chapters(result, manga_code))

    def test_wait_for_initial_data_reports_persistent_truncation(self):
        page = StreamingInitialDataPage(['{"queries":{"manga":'])

        with self.assertRaisesRegex(
            comix_module.MangaInfoFetchError,
            r"initial data did not become valid.*last payload length: 20.*position",
        ):
            asyncio.run(comix_module._wait_for_initial_data(
                page,
                operation="manga details",
                manga_code="mn6wm",
                timeout=0.02,
            ))

    def test_wait_for_initial_data_requires_requested_detail_query(self):
        wrong_query = json.dumps({
            "queries": {
                '["manga", "detail", "other"]': {"id": 1},
            }
        })
        page = StreamingInitialDataPage([wrong_query])

        with self.assertRaisesRegex(comix_module.MangaInfoFetchError, "detail query for mn6wm"):
            asyncio.run(comix_module._wait_for_initial_data(
                page,
                operation="manga details",
                manga_code="mn6wm",
                timeout=0.02,
            ))

    def test_get_manga_info_maps_validated_parsed_payload(self):
        payload = {
            "queries": {
                '["manga", "detail", "mn6wm"]': {
                    "id": 42,
                    "hid": "hash42",
                    "title": "Validated Manga",
                    "url": "/title/mn6wm-validated-manga",
                }
            }
        }

        async def fetch(_cls, _manga_code, _headless):
            return payload

        with patch.object(ComixAPI, "_get_manga_info_async", new=classmethod(fetch)):
            manga = ComixAPI.get_manga_info("mn6wm", headless=True)

        self.assertEqual(manga.manga_id, 42)
        self.assertEqual(manga.hash_id, "hash42")
        self.assertEqual(manga.title, "Validated Manga")

    def test_get_manga_info_preserves_actionable_fetch_error(self):
        error = comix_module.MangaInfoFetchError("stream was incomplete")
        async def fail_fetch(_cls, _manga_code, _headless):
            raise error

        with patch.object(ComixAPI, "_get_manga_info_async", new=classmethod(fail_fetch)):
            with self.assertRaisesRegex(comix_module.MangaInfoFetchError, "stream was incomplete"):
                ComixAPI.get_manga_info("mn6wm", headless=True)

    def test_waits_for_transient_cloudflare_challenge_in_headless_mode(self):
        page = ChallengePage(clears=True)

        asyncio.run(comix_module._wait_for_comix_page(
            page,
            "Boolean(document.getElementById('initial-data'))",
            headless=True,
            operation="discovery",
            timeout=0.1,
        ))

        self.assertFalse(page.challenge)

    def test_cloudflare_timeout_is_actionable(self):
        page = ChallengePage(clears=False)

        with self.assertRaisesRegex(RuntimeError, "Cloudflare verification did not complete"):
            asyncio.run(comix_module._wait_for_comix_page(
                page,
                "Boolean(document.getElementById('initial-data'))",
                headless=True,
                operation="discovery",
                timeout=0.01,
            ))

    def test_not_found_reader_route_fails_immediately(self):
        class MissingPage:
            async def evaluate(self, script, **kwargs):
                if script == "document.title":
                    return "Comix"
                if "document.body" in script:
                    return True
                if "#challenge-running" in script:
                    return False
                return False

            async def sleep(self, _seconds):
                raise AssertionError("not-found route should not wait for timeout")

        with self.assertRaises(comix_module.ComixPageUnavailableError):
            asyncio.run(comix_module._wait_for_comix_page(
                MissingPage(),
                "document.querySelectorAll('.rpage-page').length > 0",
                headless=True,
                operation="chapter reader",
                timeout=1,
                unavailable_script="Boolean(document.body)",
            ))

    def test_reader_ready_succeeds_even_if_unavailable_script_is_present(self):
        class ReadyPage:
            async def evaluate(self, script, **kwargs):
                if script == "document.title":
                    return "Comix"
                if "querySelectorAll" in script:
                    return True
                if "#challenge-running" in script:
                    return False
                if "initial-data" in script or "document.body" in script:
                    return True
                return False

            async def sleep(self, _seconds):
                pass

        # Should complete without error because ready_script evaluated to True
        asyncio.run(comix_module._wait_for_comix_page(
            ReadyPage(),
            "document.querySelectorAll('.rpage-page').length > 0",
            headless=True,
            operation="chapter reader",
            timeout=1,
            unavailable_script="Boolean(document.body)",
        ))

    def test_reader_service_reuses_browser_and_closes_it(self):
        class Browser:
            def __init__(self):
                self.closed = 0
                self.stopped = 0

            async def aclose(self):
                self.closed += 1

            def stop(self):
                self.stopped += 1

        browser = Browser()
        reports = []

        async def start(_headless):
            return browser

        async def fetch(_cls, chapter_id, _slug, _number, _headless, browser=None):
            reports.append((chapter_id, browser))
            return comix_module.ChapterImageFetchReport(["data:image/webp;base64,AA=="], 1)

        with patch.object(comix_module, "_start_comix_browser", new=start), \
                patch.object(ComixAPI, "_get_chapter_images_async", new=classmethod(fetch)):
            service = comix_module.ChapterReaderService(True)
            try:
                service.fetch(1, "example", "1")
                service.fetch(2, "example", "2")
            finally:
                service.close()

        self.assertEqual([item[0] for item in reports], [1, 2])
        self.assertIs(reports[0][1], reports[1][1])
        self.assertEqual(browser.closed, 1)
        self.assertEqual(browser.stopped, 1)

    def test_title_containing_moment_is_not_misclassified(self):
        class NormalPage(ChallengePage):
            async def evaluate(self, script, **kwargs):
                if script == "document.title":
                    return "A Moment in Time"
                if "#challenge-running" in script:
                    return False
                return True

        asyncio.run(comix_module._wait_for_comix_page(
            NormalPage(clears=False),
            "Boolean(document.getElementById('initial-data'))",
            headless=True,
            operation="manga details",
            timeout=0.1,
        ))

    def test_cookie_save_is_atomic_and_uses_canonical_path(self):
        class CookieJar:
            async def save(self, path, pattern=".*"):
                Path(path).write_bytes(b"cookies")

        class Browser:
            cookies = CookieJar()

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cf_cookies.dat"
            with patch.object(comix_module, "_CANONICAL_COOKIE_FILE", target):
                asyncio.run(comix_module._save_comix_cookies(Browser()))

            self.assertEqual(target.read_bytes(), b"cookies")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_normalizes_manga_summary_shape(self):
        summary = ComixAPI._normalize_manga_summary({
            "id": 42,
            "hid": "abc12",
            "title": "Example Manga",
            "type": "manhwa",
            "status": "releasing",
            "year": "2025",
            "latestChapter": 18,
            "ratedAvg": "8.7",
            "contentRating": "erotica",
            "poster": {"medium": "https://static.example/cover.jpg"},
            "url": "/title/abc12-example-manga",
        })

        self.assertEqual(summary.manga_id, 42)
        self.assertEqual(summary.manga_code, "abc12")
        self.assertEqual(summary.title, "Example Manga")
        self.assertEqual(summary.poster_url, "https://static.example/cover.jpg")
        self.assertEqual(summary.year, 2025)
        self.assertEqual(summary.latest_chapter, "18")
        self.assertEqual(summary.rated_avg, 8.7)
        self.assertEqual(summary.content_rating, "erotica")
        self.assertEqual(summary.canonical_url, "https://comix.to/title/abc12-example-manga")

    def test_normalizes_manga_summary_from_url_when_hash_is_missing(self):
        summary = ComixAPI._normalize_manga_summary({
            "title": "URL Only",
            "url": "https://comix.to/title/u77-url-only",
        })

        self.assertEqual(summary.manga_code, "u77")
        self.assertEqual(summary.canonical_url, "https://comix.to/title/u77-url-only")

    def test_rejects_manga_summary_without_identity(self):
        self.assertIsNone(ComixAPI._normalize_manga_summary({"title": "No URL"}))
        self.assertIsNone(ComixAPI._normalize_manga_summary({"hid": "abc"}))

    def test_normalizes_manga_browse_page_and_deduplicates(self):
        page = ComixAPI._normalize_manga_browse_page({
            "items": [
                {"hid": "a1", "title": "One", "url": "/title/a1-one"},
                {"hid": "a1", "title": "Duplicate", "url": "/title/a1-duplicate"},
                {"hid": "b2", "title": "Two", "url": "/title/b2-two"},
            ],
            "meta": {"page": 2, "lastPage": 5, "total": 41},
        }, requested_page=2)

        self.assertEqual([item.manga_code for item in page.items], ["a1", "b2"])
        self.assertEqual(page.page, 2)
        self.assertEqual(page.last_page, 5)
        self.assertEqual(page.total, 41)
        self.assertTrue(page.has_next)
        self.assertTrue(page.has_previous)

    def test_extracts_homepage_highlights(self):
        initial_data = {
            "queries": {
                '["manga", "top", {"type": "trending", "days": 1, "limit": 50}]': [
                    {"hid": "t1", "title": "Trend", "url": "/title/t1-trend"},
                ],
                '["manga", "list", {"scope": "hot", "order": {"chapter_updated_at": "desc"}}]': {
                    "items": [
                        {"hid": "l1", "title": "Latest", "url": "/title/l1-latest"},
                    ]
                },
            }
        }

        result = ComixAPI._extract_discovery_highlights(initial_data, limit=8)

        self.assertEqual(result["trending"][0]["hid"], "t1")
        self.assertEqual(result["latest"]["items"][0]["hid"], "l1")

    def test_extract_highlights_falls_back_to_first_rails(self):
        initial_data = {
            "queries": {
                '["manga", "top", {"type": "follows"}]': [
                    {"hid": "t1", "title": "Top"},
                ],
                '["manga", "list", {"order": {"created_at": "desc"}}]': {
                    "items": [{"hid": "l1", "title": "List"}]
                },
            }
        }

        result = ComixAPI._extract_discovery_highlights(initial_data, limit=8)

        self.assertEqual(result["trending"][0]["hid"], "t1")
        self.assertEqual(result["latest"]["items"][0]["hid"], "l1")

    def test_extract_highlights_rejects_missing_catalog(self):
        with self.assertRaisesRegex(RuntimeError, "did not include"):
            ComixAPI._extract_discovery_highlights({"queries": {}}, limit=8)

    def test_browse_api_reuses_observed_request_and_unwraps_result(self):
        class BrowsePage:
            def __init__(self):
                self.calls = []

            async def evaluate(self, script, **kwargs):
                self.calls.append((script, kwargs))
                if "performance.getEntriesByType" in script:
                    return "https://comix.to/api/v1/manga?page=2&keyword=one&_=token"
                return json.dumps({
                    "status": 200,
                    "body": json.dumps({
                        "status": "ok",
                        "result": {"items": [{"hid": "a1"}], "meta": {"page": 2}},
                    }),
                })

            async def sleep(self, _seconds):
                pass

        page = BrowsePage()
        result = asyncio.run(ComixAPI._fetch_browse_api(
            page, keyword="one", page_number=2
        ))

        self.assertEqual(result["items"][0]["hid"], "a1")
        self.assertIn('const keyword = "one"', page.calls[0][0])
        self.assertIn("const pageNumber = 2", page.calls[0][0])
        self.assertEqual(
            page.calls[1][1], {"await_promise": True, "return_by_value": True}
        )

    def test_browse_api_reports_http_error(self):
        class BrowsePage:
            async def evaluate(self, script, **kwargs):
                if "performance.getEntriesByType" in script:
                    return "https://comix.to/api/v1/manga?page=1&_=token"
                return json.dumps({"status": 403, "body": "Missing token"})

            async def sleep(self, _seconds):
                pass

        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            asyncio.run(ComixAPI._fetch_browse_api(
                BrowsePage(), keyword="", page_number=1
            ))

    def test_normalizes_dom_row(self):
        row = ComixAPI._normalize_chapter_dom_row({
            "href": "/title/y86v-i-became-a-level-999-demon-queen/1537020-chapter-1",
            "title": "Opening",
            "group": "",
            "group_official": True,
        })

        self.assertEqual(row["chapter_id"], 1537020)
        self.assertEqual(row["number"], "1")
        self.assertEqual(row["title"], "Opening")
        self.assertEqual(row["group_name"], "Official")

    def test_dedupes_chapter_rows_by_id(self):
        rows = ComixAPI._dedupe_chapter_rows([
            {"chapter_id": 1, "number": "1"},
            {"chapter_id": 1, "number": "1 duplicate"},
            {"chapter_id": 2, "number": "2"},
        ])

        self.assertEqual(rows, [
            {"chapter_id": 1, "number": "1"},
            {"chapter_id": 2, "number": "2"},
        ])

    def test_wait_for_chapter_rows_waits_for_new_first_row(self):
        class PagedPage:
            def __init__(self):
                self.calls = 0

            async def evaluate(self, script, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return json.dumps([{"href": "/title/x/1-chapter-1"}])
                return json.dumps([{"href": "/title/x/2-chapter-2"}])

            async def sleep(self, _seconds):
                pass

        rows = asyncio.run(comix_module._wait_for_chapter_rows(
            PagedPage(),
            previous_first_href="/title/x/1-chapter-1",
            timeout=1,
        ))

        self.assertEqual(rows[0]["href"], "/title/x/2-chapter-2")

    def test_waf_challenge_persists_pass_cookie(self):
        class Cookies:
            passed = False

            async def get_all(self):
                if self.passed:
                    return [types.SimpleNamespace(name="waf_pass")]
                return []

        class Browser:
            cookies = Cookies()

        class ChallengePage:
            url = None

            async def get(self, url):
                self.url = url
                Browser.cookies.passed = True

            async def sleep(self, _seconds):
                pass

        page = ChallengePage()
        saved = []

        async def save(_browser):
            saved.append(True)

        with patch.object(comix_module, "_save_comix_cookies", new=save):
            passed = asyncio.run(comix_module._solve_comix_waf_challenge(
                Browser(), page, headless=True
            ))

        self.assertTrue(passed)
        self.assertEqual(page.url, comix_module._WAF_CHALLENGE_URL)
        self.assertEqual(saved, [True])

    def test_waf_challenge_is_skipped_when_pass_exists(self):
        class Cookies:
            async def get_all(self):
                return [types.SimpleNamespace(name="waf_pass")]

        class Browser:
            cookies = Cookies()

        class ChallengePage:
            async def get(self, url):
                raise AssertionError("challenge should not open")

        self.assertFalse(asyncio.run(comix_module._solve_comix_waf_challenge(
            Browser(), ChallengePage(), headless=True
        )))

    def test_chapter_images_extracts_thin_slices(self):
        class MockTab:
            def __init__(self):
                self.evaluated_scripts = []

            async def send(self, _cmd):
                pass

            async def evaluate(self, script, **kwargs):
                self.evaluated_scripts.append(script)
                if "querySelectorAll('.rpage-page').length" in script:
                    return 2
                if 'data-page="1"' in script:
                    if "is-loading" in script:
                        return json.dumps({"type": "img", "src": "https://cdn.example/p1.webp"})
                    return "https://cdn.example/p1.webp"
                if 'data-page="2"' in script:
                    if "is-loading" in script:
                        # Emulating thin slice like 1280x10
                        return json.dumps({"type": "img", "src": "https://cdn.example/p2_thin_slice.webp"})
                    return "https://cdn.example/p2_thin_slice.webp"
                return True

            async def sleep(self, _seconds):
                pass

        class MockBrowser:
            def __init__(self):
                self.main_tab = MockTab()
                self.cookies = types.SimpleNamespace(save=self._save)

            async def _save(self, *args, **kwargs):
                pass

            async def get(self, _url):
                return self.main_tab

        browser = MockBrowser()
        with patch.object(comix_module, "_wait_for_comix_page", return_value=None), \
             patch.object(comix_module, "_publish_comix_session", return_value=None), \
             patch.object(comix_module, "_save_comix_cookies", return_value=None):
            report = asyncio.run(ComixAPI._get_chapter_images_async(
                123, "test-manga", "1", True, browser=browser
            ))

        self.assertEqual(report.image_urls, ["https://cdn.example/p1.webp", "https://cdn.example/p2_thin_slice.webp"])
        self.assertEqual(report.page_count, 2)
        self.assertEqual(report.failed_pages, [])
        self.assertEqual(report.skipped_pages, [])


if __name__ == "__main__":
    unittest.main()
