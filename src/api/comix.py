"""
Comix.to API wrapper for manga information and chapter data.
"""

import json
import re
import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from ..utils.logger import get_logger
from ..utils.nodriver_browser import start_browser
from ..utils.nodriver_compat import load_cdp_network, load_cdp_page
from ..utils.comix_session import publish_comix_credentials

logger = get_logger(__name__)

# Global lock to synchronize browser creation and cookie loading/saving across threads
_browser_lock = threading.Lock()

_CANONICAL_COOKIE_FILE = Path(__file__).resolve().parents[2] / "cf_cookies.dat"
_CLOUDFLARE_TIMEOUT_SECONDS = 60.0
_CLOUDFLARE_POLL_SECONDS = 0.25
_CLOUDFLARE_TITLE = "just a moment..."
_INITIAL_DATA_TIMEOUT_SECONDS = 15.0
_INITIAL_DATA_POLL_SECONDS = 0.05
_CHAPTER_EXTRACTION_MIN_SECONDS = 180.0
_CHAPTER_EXTRACTION_MAX_SECONDS = 900.0
_BROWSE_API_TIMEOUT_SECONDS = 30.0
_CHAPTER_LIST_TIMEOUT_SECONDS = 15.0
_CHAPTER_PAGE_TIMEOUT_SECONDS = 10.0
_WAF_COOKIE_NAME = "waf_pass"
_WAF_CHALLENGE_URL = "https://comix.to/@waf/challenge"
_WAF_TIMEOUT_SECONDS = 60.0

_CHAPTER_ROWS_JS = """(() => {
    const rows = Array.from(document.querySelectorAll('.mchap-item')).map(li => {
        const a = li.querySelector('.mchap-row__primary');
        const ch = li.querySelector('.mchap-row__ch');
        const ti = li.querySelector('.mchap-row__title');
        const gp = li.querySelector('.mchap-row__group');
        return {
            href: a ? a.getAttribute('href') : null,
            chap_label: ch ? ch.textContent.trim() : null,
            title: ti ? ti.textContent.trim() : null,
            group: gp ? (gp.querySelector('span') ? gp.querySelector('span').textContent.trim() : gp.textContent.trim()) : null,
            group_official: gp ? gp.classList.contains('is-official') : false,
        };
    });
    return JSON.stringify(rows);
})()"""


class MangaInfoFetchError(RuntimeError):
    """Raised when Comix does not expose a complete manga detail payload."""


class ComixPageUnavailableError(RuntimeError):
    """Raised when a valid browser response is a terminal not-found page."""


def _cookie_file_candidates() -> list[Path]:
    """Return canonical and legacy cookie locations without duplicating paths."""
    candidates = [_CANONICAL_COOKIE_FILE]
    legacy = Path.cwd() / "cf_cookies.dat"
    try:
        same_file = legacy.resolve() == _CANONICAL_COOKIE_FILE.resolve()
    except OSError:
        same_file = legacy == _CANONICAL_COOKIE_FILE
    if not same_file:
        candidates.append(legacy)
    return candidates


async def _start_comix_browser(headless: bool):
    """Start a browser and load the shared Comix cookie jar if available."""
    _browser_lock.acquire()
    try:
        browser = await start_browser(headless)
        for cookie_file in _cookie_file_candidates():
            if not cookie_file.exists():
                continue
            try:
                await browser.cookies.load(str(cookie_file))
                logger.info("Loaded cookies from %s", cookie_file)
                break
            except Exception as exc:
                logger.warning("Failed loading cookies from %s: %s", cookie_file, exc)
        return browser
    finally:
        _browser_lock.release()


async def _close_comix_browser(browser) -> None:
    """Close the CDP connection before terminating Chrome.

    nodriver's ``stop`` schedules ``aclose`` as a background task.  The task
    can be lost when the ``asyncio.run`` loop closes, leaving a child process or
    websocket behind.  Await it here and retain ``stop`` as the final fallback
    for compatible nodriver facades and test doubles.
    """
    if browser is None:
        return
    aclose = getattr(browser, "aclose", None)
    if callable(aclose):
        try:
            await asyncio.wait_for(aclose(), timeout=3.0)
        except Exception as exc:
            logger.debug("Browser connection close failed: %s", exc)
    stop = getattr(browser, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception as exc:
            logger.debug("Browser process stop failed: %s", exc)


async def _save_comix_cookies(browser) -> None:
    """Persist browser cookies atomically after a verified Comix page."""
    cookie_file = _CANONICAL_COOKIE_FILE
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = cookie_file.with_name(
        f".{cookie_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    _browser_lock.acquire()
    try:
        try:
            await browser.cookies.save(str(temporary_file), pattern=".*")
            os.replace(temporary_file, cookie_file)
            logger.info("Saved cookies to %s", cookie_file)
        except Exception as exc:
            logger.warning("Failed saving cookies to %s: %s", cookie_file, exc)
    finally:
        _browser_lock.release()
        try:
            temporary_file.unlink(missing_ok=True)
        except OSError:
            pass


async def _has_waf_pass(browser) -> bool:
    """Return whether the browser already holds Comix's WAF pass cookie."""
    try:
        cookies = await browser.cookies.get_all()
    except Exception:
        return False
    return any(getattr(cookie, "name", "") == _WAF_COOKIE_NAME for cookie in cookies)


async def _solve_comix_waf_challenge(
    browser,
    page,
    *,
    headless: bool,
    force: bool = False,
) -> bool:
    """Open Comix's Turnstile gate and wait for the resulting WAF cookie.

    Comix serves the HTML pages normally but gates the chapter API behind a
    Turnstile check.  The challenge auto-completes in a normal browser and sets
    ``waf_pass``; persisting that cookie keeps later chapter fetches fast.
    """
    if not force and await _has_waf_pass(browser):
        return False

    logger.info("Solving Comix WAF challenge (headless=%s)...", headless)
    await page.get(_WAF_CHALLENGE_URL)
    deadline = time.monotonic() + _WAF_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if await _has_waf_pass(browser):
            await _save_comix_cookies(browser)
            logger.info("Comix WAF challenge passed")
            return True
        await page.sleep(0.5)

    raise RuntimeError(
        f"Comix WAF verification did not complete within {int(_WAF_TIMEOUT_SECONDS)} seconds"
    )


async def _disable_comix_browser_cache(page) -> None:
    """Bypass stale cached 403s for Comix's tokenized API requests."""
    try:
        cdp_network = load_cdp_network()
        await page.send(cdp_network.enable())
        await page.send(cdp_network.set_cache_disabled(cache_disabled=True))
    except Exception as exc:
        logger.debug("Could not disable Comix browser cache: %s", exc)


async def _wait_for_chapter_rows(
    page,
    *,
    timeout: float = _CHAPTER_LIST_TIMEOUT_SECONDS,
    previous_first_href: Optional[str] = None,
) -> list[dict]:
    """Wait until the rendered chapter list has rows (and a new first page)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rows_str = await page.evaluate(_CHAPTER_ROWS_JS)
            rows = json.loads(rows_str) if isinstance(rows_str, str) and rows_str else []
        except Exception:
            rows = []
        if rows and (
            previous_first_href is None
            or rows[0].get("href") != previous_first_href
        ):
            return rows
        await page.sleep(0.25)
    return []


async def _publish_comix_session(browser, page) -> None:
    """Expose the verified browser identity to the GUI cover provider."""
    try:
        user_agent = await page.evaluate("navigator.userAgent")
        cookies = await browser.cookies.get_all()
        credentials = publish_comix_credentials(user_agent, cookies)
        logger.debug(
            "Published Comix image session (generation=%s, cookies=%s)",
            credentials.generation,
            len(credentials.cookies),
        )
    except Exception as exc:
        # Cover loading is an enhancement for GUI callers; API data should keep
        # working if a browser facade does not expose cookie metadata.
        logger.warning("Could not publish Comix image session: %s", exc)


async def _page_has_cloudflare_challenge(page) -> bool:
    """Identify Cloudflare's challenge page without false title matches."""
    title = await page.evaluate("document.title")
    title = title if isinstance(title, str) else ""
    if title.strip().lower() == _CLOUDFLARE_TITLE:
        return True
    try:
        marker = await page.evaluate(
            "Boolean(document.querySelector('#challenge-running, #challenge-stage, form#challenge-form'))"
        )
        return marker is True
    except Exception:
        return False


async def _wait_for_comix_page(
    page,
    ready_script: str,
    *,
    headless: bool,
    operation: str,
    timeout: float = _CLOUDFLARE_TIMEOUT_SECONDS,
    unavailable_script: str | None = None,
) -> None:
    """Wait for Cloudflare verification and operation-specific DOM readiness."""
    deadline = time.monotonic() + timeout
    saw_challenge = False
    last_title = ""
    while time.monotonic() < deadline:
        try:
            last_title = (await page.evaluate("document.title")) or ""
            challenge = await _page_has_cloudflare_challenge(page)
            saw_challenge = saw_challenge or challenge
            if unavailable_script and bool(await page.evaluate(unavailable_script)):
                raise ComixPageUnavailableError(
                    f"Comix {operation} is unavailable (the requested route was not found)"
                )
            ready = bool(await page.evaluate(ready_script))
            if ready and not challenge:
                return
        except ComixPageUnavailableError:
            raise
        except Exception:
            pass
        await page.sleep(_CLOUDFLARE_POLL_SECONDS)

    if saw_challenge:
        mode = "headless" if headless else "headful"
        raise RuntimeError(
            f"Comix Cloudflare verification did not complete within {int(timeout)} seconds "
            f"while fetching {operation} ({mode} mode; last title: {last_title!r})"
        )
    raise RuntimeError(
        f"Comix page did not become ready within {int(timeout)} seconds while fetching {operation}"
    )


def _find_manga_detail(data: object, manga_code: str) -> Optional[dict]:
    """Return the requested manga detail query from parsed initial-data."""
    if not isinstance(data, dict):
        return None

    queries = data.get("queries")
    if not isinstance(queries, dict):
        return None

    for key, value in queries.items():
        key_text = key if isinstance(key, str) else str(key)
        if (
            "manga" in key_text.lower()
            and "detail" in key_text.lower()
            and manga_code in key_text
            and isinstance(value, dict)
        ):
            return value
    return None


async def _wait_for_initial_data(
    page,
    *,
    operation: str,
    manga_code: Optional[str] = None,
    timeout: float = _INITIAL_DATA_TIMEOUT_SECONDS,
) -> dict:
    """Wait until Comix's streamed initial-data script contains valid JSON.

    The script element is inserted before its body is necessarily complete.  A
    non-empty value is therefore not a sufficient readiness signal; parsing and
    validating the expected query is the completion condition.
    """
    deadline = time.monotonic() + timeout
    last_length = 0
    last_error = "no payload observed"

    while time.monotonic() < deadline:
        try:
            raw_data = await page.evaluate(
                "document.getElementById('initial-data') ? "
                "document.getElementById('initial-data').textContent : null"
            )
            if isinstance(raw_data, str):
                last_length = len(raw_data)
                if raw_data.strip():
                    try:
                        parsed = json.loads(raw_data)
                    except json.JSONDecodeError as exc:
                        last_error = f"JSON {exc.msg} at position {exc.pos}"
                    else:
                        if not isinstance(parsed, dict):
                            last_error = f"root value is {type(parsed).__name__}, not an object"
                        elif not isinstance(parsed.get("queries"), dict):
                            last_error = "queries is missing or is not an object"
                        elif manga_code is not None and _find_manga_detail(parsed, manga_code) is None:
                            last_error = f"manga detail query for {manga_code} is not present yet"
                        else:
                            return parsed
        except Exception as exc:
            last_error = f"page read failed: {type(exc).__name__}"

        await page.sleep(_INITIAL_DATA_POLL_SECONDS)

    code_suffix = f" for {manga_code}" if manga_code else ""
    raise MangaInfoFetchError(
        f"Comix initial data did not become valid while fetching {operation}{code_suffix} "
        f"within {timeout:g} seconds (last payload length: {last_length}; {last_error})"
    )


@dataclass
class ChapterImageFetchReport:
    """Images extracted from a chapter reader page, plus extraction diagnostics."""

    image_urls: list[str]
    page_count: int = 0
    skipped_pages: list[int] = field(default_factory=list)
    failed_pages: list[int] = field(default_factory=list)
    page_numbers: list[int] = field(default_factory=list)

    @property
    def expected_image_count(self) -> int:
        if self.page_count <= 0:
            return len(self.image_urls)
        return max(0, self.page_count - len(self.skipped_pages))


def run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


class ComixAPI:
    """API wrapper for comix.to"""
    
    MAX_CHAPTER_PAGES = 200
    DISCOVERY_PAGE_LIMIT = 20
    DISCOVERY_HIGHLIGHT_LIMIT = 8
    
    @staticmethod
    def extract_manga_code(url: str) -> str:
        """
        Extract manga code from the title URL.
        Example: https://comix.to/title/93q1r-the-summoner -> 93q1r
        """
        parts = url.rstrip("/").split("/")
        last = parts[-1] if parts[-1] else parts[-2]
        code = last.split("-")[0]
        logger.debug(f"Extracted manga code: {code} from URL: {url}")
        return code

    @classmethod
    def _normalize_manga_summary(cls, item: dict):
        """Normalize a catalog item returned by Comix's manga API."""
        from ..core.models import MangaSummary

        if not isinstance(item, dict):
            return None

        title = cls._first_present(item, "title", "name")
        if not title:
            return None

        raw_url = cls._first_present(item, "url", "canonical_url", "canonicalUrl") or ""
        if raw_url.startswith("/"):
            canonical_url = f"https://comix.to{raw_url}"
        elif raw_url.startswith("http://") or raw_url.startswith("https://"):
            canonical_url = raw_url
        else:
            canonical_url = ""

        manga_code = cls._first_present(item, "hid", "hash_id", "hashId", "manga_code", "code") or ""
        if not manga_code and canonical_url:
            try:
                manga_code = cls.extract_manga_code(canonical_url)
            except (IndexError, AttributeError):
                manga_code = ""
        if not manga_code:
            return None

        poster = cls._first_present(item, "poster", "cover") or {}
        if isinstance(poster, dict):
            poster_url = cls._first_present(poster, "large", "medium", "small") or ""
        else:
            poster_url = poster if isinstance(poster, str) else ""

        manga_id = cls._first_present(item, "id", "manga_id", "mangaId")
        try:
            manga_id = int(manga_id) if manga_id is not None else None
        except (TypeError, ValueError):
            manga_id = None

        year = cls._first_present(item, "year", "startDateYear")
        try:
            year = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            year = None

        rated_avg = cls._first_present(item, "ratedAvg", "rated_avg", "score")
        try:
            rated_avg = float(rated_avg) if rated_avg not in (None, "") else None
        except (TypeError, ValueError):
            rated_avg = None

        content_rating = str(
            cls._first_present(item, "contentRating", "content_rating") or "safe"
        ).lower()

        if not canonical_url:
            canonical_url = f"https://comix.to/title/{manga_code}"

        return MangaSummary(
            manga_id=manga_id,
            manga_code=str(manga_code),
            title=str(title),
            poster_url=str(poster_url),
            manga_type=cls._first_present(item, "type", "manga_type"),
            status=cls._first_present(item, "status"),
            year=year,
            latest_chapter=str(
                cls._first_present(item, "latestChapter", "latest_chapter")
                or ""
            ),
            rated_avg=rated_avg,
            content_rating=content_rating,
            canonical_url=canonical_url,
        )

    @classmethod
    def _normalize_manga_browse_page(cls, payload: dict, requested_page: int = 1):
        """Normalize a list response into the GUI-facing browse page model."""
        from ..core.models import MangaBrowsePage

        if not isinstance(payload, dict):
            raise RuntimeError("Comix returned an invalid manga list response")

        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raw_items = payload.get("data") if isinstance(payload.get("data"), list) else []

        items = []
        seen_codes = set()
        for item in raw_items:
            summary = cls._normalize_manga_summary(item)
            if summary and summary.manga_code not in seen_codes:
                seen_codes.add(summary.manga_code)
                items.append(summary)

        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        try:
            page = max(1, int(meta.get("page", requested_page)))
        except (TypeError, ValueError):
            page = max(1, requested_page)
        try:
            last_page = max(page, int(meta.get("lastPage", page)))
        except (TypeError, ValueError):
            last_page = page + 1 if meta.get("hasNext") else page
        try:
            total = max(0, int(meta.get("total", len(items))))
        except (TypeError, ValueError):
            total = len(items)

        return MangaBrowsePage(items=items, page=page, last_page=last_page, total=total)

    @staticmethod
    def _extract_discovery_highlights(initial_data: dict, limit: int) -> dict:
        """Pull the homepage's server-rendered trending and latest rails."""
        queries = initial_data.get("queries") if isinstance(initial_data, dict) else None
        if not isinstance(queries, dict):
            raise RuntimeError("Comix homepage did not include catalog data")

        def items_from(value):
            if isinstance(value, list):
                return value
            if isinstance(value, dict) and isinstance(value.get("items"), list):
                return value["items"]
            return []

        trending = []
        latest = []
        fallback_top = []
        fallback_list = []
        for key, value in queries.items():
            try:
                parts = json.loads(key) if isinstance(key, str) else None
            except json.JSONDecodeError:
                parts = None
            if not isinstance(parts, list) or len(parts) < 3 or parts[0] != "manga":
                continue
            params = parts[2] if isinstance(parts[2], dict) else {}
            items = items_from(value)
            if parts[1] == "top":
                if params.get("type") == "trending":
                    trending = items
                if not fallback_top:
                    fallback_top = items
            elif parts[1] == "list":
                if params.get("scope") == "hot":
                    latest = items
                if not fallback_list:
                    fallback_list = items

        trending = trending or fallback_top
        latest = latest or fallback_list
        if not trending and not latest:
            raise RuntimeError("Comix homepage did not include trending or latest manga")
        return {
            "trending": trending[:limit],
            "latest": {"items": latest[:limit]},
        }

    @classmethod
    async def _fetch_browse_api(cls, page, *, keyword: str, page_number: int) -> dict:
        """Reuse the catalog request Comix's browse page made for itself."""
        deadline = time.monotonic() + _BROWSE_API_TIMEOUT_SECONDS
        api_url = ""
        while time.monotonic() < deadline:
            try:
                api_url = await page.evaluate(
                    f"""(() => {{
                        const keyword = {json.dumps(keyword)};
                        const pageNumber = {int(page_number)};
                        const entries = performance.getEntriesByType('resource')
                            .map((entry) => entry.name)
                            .filter((name) => {{
                                try {{
                                    const url = new URL(name);
                                    return url.pathname === '/api/v1/manga'
                                        && url.searchParams.get('page') === String(pageNumber)
                                        && (url.searchParams.get('keyword') || '') === keyword;
                                }} catch (error) {{
                                    return false;
                                }}
                            }});
                        return entries.length ? entries[entries.length - 1] : '';
                    }})()""",
                    return_by_value=True,
                )
            except Exception:
                api_url = ""
            if isinstance(api_url, str) and api_url:
                break
            await page.sleep(0.25)

        if not api_url:
            raise RuntimeError("Comix browse API request was not observed")

        response_str = await page.evaluate(
            f"""(async () => {{
                const response = await fetch({json.dumps(api_url)}, {{ credentials: 'same-origin' }});
                return JSON.stringify({{ status: response.status, body: await response.text() }});
            }})()""",
            await_promise=True,
            return_by_value=True,
        )
        if not isinstance(response_str, str) or not response_str:
            raise RuntimeError("Comix browse API returned no serialized result")
        try:
            response = json.loads(response_str)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Comix browse API returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise RuntimeError("Comix browse API returned an invalid response object")
        if response.get("status") != 200:
            raise RuntimeError(
                f"Comix browse API returned HTTP {response.get('status')}: "
                f"{str(response.get('body') or '')[:200]}"
            )
        try:
            payload = json.loads(response.get("body") or "")
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Comix browse API returned an invalid payload") from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise RuntimeError("Comix browse API returned an unsuccessful payload")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Comix browse API result is missing")
        return result

    @classmethod
    async def _get_discovery_async(
        cls,
        *,
        keyword: str = "",
        page_number: int = 1,
        limit: int = DISCOVERY_PAGE_LIMIT,
        highlights: bool = False,
        headless: bool,
    ) -> dict:
        browser = await _start_comix_browser(headless)

        try:
            page = await browser.get("about:blank")
            await _disable_comix_browser_cache(page)

            if highlights:
                await page.get("https://comix.to/")
                await _wait_for_comix_page(
                    page,
                    "Boolean(document.getElementById('initial-data'))",
                    headless=headless,
                    operation="discovery highlights",
                )
                initial_data = await _wait_for_initial_data(
                    page,
                    operation="discovery highlights",
                )
                await _publish_comix_session(browser, page)
                await _save_comix_cookies(browser)
                return cls._extract_discovery_highlights(initial_data, limit)

            params = {}
            if keyword:
                params["keyword"] = keyword
                params["sort"] = "relevance:desc"
            if page_number > 1:
                params["page"] = page_number
            page_url = "https://comix.to/browse"
            if params:
                page_url = f"{page_url}?{urlencode(params)}"

            await page.get(page_url)
            await _wait_for_comix_page(
                page,
                "Boolean(document.getElementById('initial-data'))",
                headless=headless,
                operation="discovery",
            )
            await _publish_comix_session(browser, page)
            await _save_comix_cookies(browser)
            result = await cls._fetch_browse_api(
                page,
                keyword=keyword,
                page_number=page_number,
            )
            return {"data": result}
        finally:
            await _close_comix_browser(browser)

    @classmethod
    def search_manga(
        cls,
        keyword: str,
        page: int = 1,
        limit: int = DISCOVERY_PAGE_LIMIT,
        headless: Optional[bool] = None,
    ):
        """Search the Comix catalog for manga summaries."""
        from ..core.models import MangaBrowsePage
        from ..utils.config import ConfigManager

        keyword = (keyword or "").strip()
        if not keyword:
            return MangaBrowsePage()
        if headless is None:
            headless = ConfigManager().get("headless", True)
        page = max(1, int(page))
        try:
            result = run_async(
                cls._get_discovery_async(
                    keyword=keyword,
                    page_number=page,
                    limit=limit,
                    highlights=False,
                    headless=headless,
                )
            )
            return cls._normalize_manga_browse_page(result.get("data", {}), page)
        except Exception:
            logger.exception("Discovery search failed for %r", keyword)
            raise

    @classmethod
    def get_manga_highlights(
        cls,
        limit: int = DISCOVERY_HIGHLIGHT_LIMIT,
        headless: Optional[bool] = None,
    ) -> dict[str, list]:
        """Fetch compact trending and latest catalog rails for the GUI."""
        from ..utils.config import ConfigManager

        if headless is None:
            headless = ConfigManager().get("headless", True)
        try:
            result = run_async(
                cls._get_discovery_async(
                    limit=limit,
                    highlights=True,
                    headless=headless,
                )
            )
            trending = result.get("trending", [])
            latest_payload = result.get("latest", {})
            if not isinstance(trending, list):
                trending = []
            if not isinstance(latest_payload, dict):
                latest_payload = {}
            latest = latest_payload.get("items", [])
            return {
                "trending": [
                    summary for item in trending
                    if (summary := cls._normalize_manga_summary(item)) is not None
                ],
                "latest": [
                    summary for item in latest
                    if (summary := cls._normalize_manga_summary(item)) is not None
                ],
            }
        except Exception:
            logger.exception("Discovery highlights failed")
            raise
    
    @classmethod
    async def _get_manga_info_async(cls, manga_code: str, headless: bool) -> dict:
        url = f"https://comix.to/title/{manga_code}"
        browser = await _start_comix_browser(headless)
        
        try:
            page = await browser.get(url)
            await _wait_for_comix_page(
                page,
                "Boolean(document.getElementById('initial-data'))",
                headless=headless,
                operation="manga details",
            )
            initial_data = await _wait_for_initial_data(
                page,
                operation="manga details",
                manga_code=manga_code,
            )
            await _publish_comix_session(browser, page)
            await _save_comix_cookies(browser)
            return initial_data
            
        finally:
            await _close_comix_browser(browser)
            
    @classmethod
    def get_manga_info(cls, manga_code: str, headless: Optional[bool] = None):
        """Fetch manga information from DOM using nodriver."""
        from ..core.models import MangaInfo
        if headless is None:
            from ..utils.config import ConfigManager
            headless = ConfigManager().get("headless", True)
            
        logger.info(f"Fetching manga info using nodriver (headless={headless}) for {manga_code}...")
        
        try:
            json_data = run_async(cls._get_manga_info_async(manga_code, headless))
        except MangaInfoFetchError as exc:
            logger.error("nodriver failed to fetch manga info for %s: %s", manga_code, exc)
            raise
        except Exception as exc:
            logger.exception("nodriver failed to fetch manga info for %s", manga_code)
            raise MangaInfoFetchError(
                f"Could not fetch manga information for {manga_code}: {exc}"
            ) from exc

        # Find the manga detail query in the json_data
        manga_detail = _find_manga_detail(json_data, manga_code)
        if not manga_detail:
            queries = json_data.get("queries", {}) if isinstance(json_data, dict) else {}
            keys = list(queries.keys()) if isinstance(queries, dict) else []
            raise MangaInfoFetchError(
                f"Comix initial data did not contain manga details for {manga_code} "
                f"(query keys: {keys})"
            )
            
        # Get alt titles safely
        alt_titles = manga_detail.get("altTitles", [])
        if not isinstance(alt_titles, list):
            alt_titles = [alt_titles] if alt_titles else []
            
        # Poster URL
        poster = manga_detail.get("poster") or {}
        poster_url = None
        if isinstance(poster, dict):
            poster_url = poster.get("large") or poster.get("medium")
            
        genres = []
        for g in manga_detail.get("genres", []):
            if isinstance(g, dict) and "title" in g:
                genres.append(g["title"])
            elif isinstance(g, str):
                genres.append(g)

        return MangaInfo(
            manga_id=manga_detail.get("id"),
            hash_id=manga_detail.get("hid"),
            title=manga_detail.get("title", "Unknown"),
            alt_titles=alt_titles,
            slug=manga_detail.get("url", "").split("/")[-1] if manga_detail.get("url") else None,
            rank=manga_detail.get("rank"),
            manga_type=manga_detail.get("type"),
            poster_url=poster_url,
            original_language=manga_detail.get("originalLanguage"),
            status=manga_detail.get("status"),
            final_chapter=str(manga_detail.get("finalChapter") or 0),
            latest_chapter=str(manga_detail.get("latestChapter") or 0),
            start_date=manga_detail.get("startDate"),
            end_date=manga_detail.get("endDate"),
            rated_avg=manga_detail.get("ratedAvg"),
            rated_count=manga_detail.get("ratedCount"),
            follows_total=manga_detail.get("followsTotal"),
            is_nsfw=manga_detail.get("contentRating") == "nsfw",
            year=manga_detail.get("year"),
            genres=genres,
            description=manga_detail.get("synopsis", "")
        )

    @staticmethod
    def _first_present(data: dict, *keys: str):
        """Return the first non-empty value from a dict."""
        for key in keys:
            value = data.get(key)
            if value is not None:
                return value
        return None

    @classmethod
    def _normalize_chapter_dom_row(cls, row: dict) -> Optional[dict]:
        """Normalize a chapter row scraped from the rendered chapter list."""
        href = row.get("href") if isinstance(row, dict) else None
        if not href:
            return None

        # Parse `/title/{slug}/{chap_id}-chapter-{chap_num}`
        match = re.match(r".*/title/[^/]+/(\d+)-chapter-(.+)$", href)
        if not match:
            return None

        chapter_id, chapter_number = match.group(1), match.group(2)
        group = row.get("group")
        if not group and row.get("group_official"):
            group = "Official"

        return {
            "chapter_id": int(chapter_id),
            "number": chapter_number,
            "title": row.get("title") or "",
            "volume": None,
            "votes": 0,
            "group_name": group,
            "pages_count": 0,
        }

    @staticmethod
    def _dedupe_chapter_rows(rows: list[dict]) -> list[dict]:
        """Keep the first row for each chapter id."""
        seen_ids = set()
        unique_rows = []
        for row in rows:
            chapter_id = row.get("chapter_id")
            if chapter_id in seen_ids:
                continue
            seen_ids.add(chapter_id)
            unique_rows.append(row)
        return unique_rows

    @staticmethod
    def _manga_has_chapters(initial_data: dict, manga_code: str) -> Optional[bool]:
        """Read hasChapters from already validated initial-data."""
        detail = _find_manga_detail(initial_data, manga_code)
        if not detail or not isinstance(detail.get("hasChapters"), bool):
            return None
        return detail["hasChapters"]

    @classmethod
    async def _get_all_chapters_async(cls, manga_code: str, headless: bool) -> list[dict]:
        url = f"https://comix.to/title/{manga_code}"
        browser = await _start_comix_browser(headless)

        try:
            page = await browser.get("about:blank")
            await _disable_comix_browser_cache(page)
            await page.get(url)

            async def load_page() -> Optional[bool]:
                await _wait_for_comix_page(
                    page,
                    "Boolean(document.getElementById('initial-data'))",
                    headless=headless,
                    operation="chapter listing",
                )
                initial_data = await _wait_for_initial_data(
                    page,
                    operation="chapter listing",
                    manga_code=manga_code,
                )
                await _publish_comix_session(browser, page)
                await _save_comix_cookies(browser)
                return cls._manga_has_chapters(initial_data, manga_code)

            has_chapters = await load_page()
            if has_chapters is False:
                return []

            if not await _has_waf_pass(browser):
                await _solve_comix_waf_challenge(browser, page, headless=headless)
                await page.get(url)
                has_chapters = await load_page()

            rows = await _wait_for_chapter_rows(page)
            if not rows:
                # A stored pass can go stale without disappearing from the jar.
                await _solve_comix_waf_challenge(
                    browser, page, headless=headless, force=True
                )
                await page.get(url)
                has_chapters = await load_page()
                rows = await _wait_for_chapter_rows(page)

            if not rows:
                if has_chapters:
                    raise RuntimeError(
                        "Comix reports chapters exist, but no chapter rows could be fetched"
                    )
                return []

            all_rows = []
            seen_ids = set()
            prev_first_href = None
            consecutive_dup_pages = 0

            for page_n in range(1, cls.MAX_CHAPTER_PAGES + 1):
                if page_n > 1:
                    await page.get(f"{url}?page={page_n}")
                    rows = await _wait_for_chapter_rows(
                        page,
                        timeout=_CHAPTER_PAGE_TIMEOUT_SECONDS,
                        previous_first_href=prev_first_href,
                    )
                if not rows:
                    break

                prev_first_href = rows[0].get("href")
                page_added = 0

                for row in rows:
                    href = row.get("href")
                    if not href:
                        continue

                    normalized = cls._normalize_chapter_dom_row(row)
                    if not normalized:
                        continue

                    if normalized["chapter_id"] in seen_ids:
                        continue

                    seen_ids.add(normalized["chapter_id"])
                    all_rows.append(normalized)
                    page_added += 1

                if page_added == 0:
                    consecutive_dup_pages += 1
                    if consecutive_dup_pages >= 2:
                        break
                else:
                    consecutive_dup_pages = 0

            all_rows = cls._dedupe_chapter_rows(all_rows)
            if not all_rows and has_chapters:
                raise RuntimeError(
                    "Comix reports chapters exist, but no chapter rows could be fetched"
                )

            return all_rows
        finally:
            await _close_comix_browser(browser)

    @classmethod
    def get_all_chapters(cls, manga_code: str, headless: Optional[bool] = None) -> list[any]:
        """Fetch all chapters for a manga from Comix's rendered chapter list."""
        from ..core.models import Chapter
        if headless is None:
            from ..utils.config import ConfigManager
            headless = ConfigManager().get("headless", True)
            
        logger.info(f"Fetching chapters using nodriver (headless={headless}) for {manga_code}...")
        
        chapters: list[Chapter] = []
        try:
            rows = run_async(cls._get_all_chapters_async(manga_code, headless))
            for row in rows:
                chapters.append(Chapter(
                    chapter_id=row["chapter_id"],
                    number=row["number"],
                    title=row["title"],
                    volume=row.get("volume"),
                    votes=row.get("votes", 0),
                    group_name=row["group_name"],
                    pages_count=row.get("pages_count", 0)
                ))
        except Exception as e:
            logger.error(f"nodriver failed to fetch chapters for {manga_code}: {e}")
            raise
            
        # Reverse the list so old chapters (low numbers) are at the beginning
        chapters.reverse()
        logger.info(f"Found {len(chapters)} chapters using nodriver")
        return chapters
    
    @classmethod
    async def _get_chapter_images_async(
        cls,
        chapter_id: int,
        manga_slug: str,
        chapter_number: str,
        headless: bool,
        browser=None,
    ) -> ChapterImageFetchReport:
        chapter_url = f"https://comix.to/title/{manga_slug}/{chapter_id}-chapter-{chapter_number}"
        owns_browser = browser is None
        if owns_browser:
            browser = await _start_comix_browser(headless)
                
        image_urls = []
        page_count = 0
        skipped_pages = []
        failed_pages = []
        page_numbers = []
        
        try:
            # Setup init script to backup original toDataURL and set localStorage reader.default preload config
            page = browser.main_tab
            try:
                cdp_page = load_cdp_page()
                await page.send(cdp_page.enable())
                init_js = """
                try {
                    window.__origToDataURL = HTMLCanvasElement.prototype.toDataURL;
                    const k = 'reader.default';
                    const cur = JSON.parse(localStorage.getItem(k) || '{}');
                    cur.preload = 'all';
                    localStorage.setItem(k, JSON.stringify(cur));
                } catch (e) {}
                """
                await page.send(cdp_page.add_script_to_evaluate_on_new_document(source=init_js))
            except Exception as e:
                logger.warning(f"Failed to setup page init script: {e}")
                
            # Now navigate directly to chapter page
            page = await browser.get(chapter_url)
            await _wait_for_comix_page(
                page,
                "document.querySelectorAll('.rpage-page').length > 0",
                headless=headless,
                operation="chapter reader",
                timeout=90.0,
                unavailable_script=(
                    "Boolean(document.body && /could not be found|page not found|404/i.test("
                    "document.body.innerText || ''))"
                ),
            )
            page_count = await page.evaluate("document.querySelectorAll('.rpage-page').length") or 0
            await _publish_comix_session(browser, page)
            await _save_comix_cookies(browser)
                
            if page_count == 0:
                logger.error(f"Chapter page had no pages in DOM: {chapter_url}")
                return ChapterImageFetchReport([], 0, [], [])
                
            # Wait for first page to begin rendering
            for _ in range(150):
                try:
                    first_ready = await page.evaluate(
                        "document.querySelector('.rpage-page[data-page=\"1\"] canvas, .rpage-page[data-page=\"1\"] img') ? true : false"
                    )
                    if first_ready:
                        break
                except Exception:
                    pass
                await page.sleep(0.2)
                
            logger.info(f"Chapter has {page_count} pages. Extracting content...")
            extraction_deadline = time.monotonic() + min(
                _CHAPTER_EXTRACTION_MAX_SECONDS,
                max(_CHAPTER_EXTRACTION_MIN_SECONDS, page_count * 5.0),
            )
            
            for page_num in range(1, page_count + 1):
                if time.monotonic() >= extraction_deadline:
                    failed_pages.extend(range(page_num, page_count + 1))
                    logger.error(
                        "Chapter extraction deadline reached after %s pages",
                        page_num - 1,
                    )
                    break
                ready = None
                for scroll_attempt in range(2):
                    if scroll_attempt == 1:
                        logger.info(f"Page {page_num} hang detected. Breaking ad layer and emulating scroll up/down.")
                        try:
                            # Break the glass layer with a real click
                            try:
                                import nodriver.cdp.input_ as cdp_input
                                await page.send(cdp_input.dispatch_mouse_event(
                                    type_="mousePressed", x=100, y=100, button=cdp_input.MouseButton.LEFT, click_count=1
                                ))
                                await page.sleep(0.05)
                                await page.send(cdp_input.dispatch_mouse_event(
                                    type_="mouseReleased", x=100, y=100, button=cdp_input.MouseButton.LEFT, click_count=1
                                ))
                            except Exception as e:
                                logger.debug(f"CDP click failed: {e}")
                                
                            await page.sleep(0.5)
                            
                            # Close any new popup/ad tabs that opened
                            if hasattr(browser, 'tabs'):
                                for tab in list(browser.tabs):
                                    if tab != page and tab != browser.main_tab:
                                        try:
                                            await tab.close()
                                        except Exception:
                                            pass
                        except Exception as e:
                            logger.debug(f"Glass layer break failed: {e}")

                        try:
                            # Emulate physical mouse scroll up events via CDP
                            for _ in range(10):
                                await page.send(cdp_input.dispatch_mouse_event(
                                    type_="mouseWheel", x=100, y=100, delta_x=0, delta_y=-400
                                ))
                                await page.sleep(0.05)
                                
                            await page.sleep(1.5)
                            
                            # Emulate physical mouse scroll down events via CDP
                            for _ in range(10):
                                await page.send(cdp_input.dispatch_mouse_event(
                                    type_="mouseWheel", x=100, y=100, delta_x=0, delta_y=400
                                ))
                                await page.sleep(0.05)
                                
                            await page.sleep(1.5)
                        except Exception as e:
                            logger.debug(f"CDP scroll failed: {e}")

                    # Scroll page element into view to trigger render/decryption
                    try:
                        await page.evaluate(
                            f"(() => {{ const el = document.querySelector('.rpage-page[data-page=\"{page_num}\"]'); if (el) el.scrollIntoView({{behavior: 'instant', block: 'center'}}); }})()"
                        )
                    except Exception:
                        pass
                        
                    # Wait for image element or canvas element to be ready (25 polls = 5s initial, 150 polls = 30s hang retry)
                    for _attempt in range(25 if scroll_attempt == 0 else 150):
                        try:
                            ready_res = await page.evaluate(
                                f"""(() => {{
                                    const el = document.querySelector('.rpage-page[data-page="{page_num}"]');
                                    if (!el) return null;
                                    const isLoading = el.classList.contains('is-loading');
                                    
                                    // Check canvas
                                    const c = el.querySelector('canvas');
                                    if (c && c.width > 10 && c.height > 10) {{
                                        if (isLoading) return null; // Wait if still loading
                                        const toDataURL = window.__origToDataURL || c.toDataURL;
                                        const data = toDataURL.call(c, 'image/webp', 0.95);
                                        if (data.length < 20000) {{
                                            return JSON.stringify({{type: 'skip'}}); // Blank/Ad canvas
                                        }}
                                        return JSON.stringify({{type: 'canvas_data', data: data}});
                                    }}
                                    
                                    // Check image
                                    const i = el.querySelector('img');
                                    if (i && i.src) {{
                                        if (i.complete) {{
                                            if (i.naturalWidth > 10 && i.naturalHeight > 10) {{
                                                return JSON.stringify({{type: 'img', src: i.src}});
                                            }}
                                            if (i.naturalWidth > 0 && i.naturalWidth <= 10) {{
                                                return JSON.stringify({{type: 'skip'}}); // 1x1 placeholder
                                            }}
                                        }}
                                    }}
                                    return null;
                                }})()"""
                            )
                            ready = json.loads(ready_res) if ready_res else None
                        except Exception:
                            ready = None
                        if ready:
                            break
                        await page.sleep(0.2)
                        
                    if ready:
                        break
                    
                if not ready:
                    logger.error(f"Page {page_num} timed out waiting for render.")
                    failed_pages.append(page_num)
                    continue
                    
                if ready.get('type') == 'skip':
                    logger.debug(f"Page {page_num} is an ad/placeholder page. Skipping.")
                    skipped_pages.append(page_num)
                    continue
                    
                if ready.get('type') == 'canvas_data':
                    image_urls.append(ready.get('data'))
                    page_numbers.append(page_num)
                    continue
                    
                # Extract image data or URL from image
                try:
                    extracted_url = await page.evaluate(
                        f"""(() => {{
                            try {{
                                const el = document.querySelector('.rpage-page[data-page="{page_num}"]');
                                if (!el) return null;
                                
                                const c = el.querySelector('canvas');
                                if (c && c.width > 0 && c.height > 0) {{
                                    const toDataURL = window.__origToDataURL || c.toDataURL;
                                    return toDataURL.call(c, 'image/webp', 0.95);
                                }}
                                
                                const i = el.querySelector('img');
                                if (i && i.src) {{
                                    if (i.src.startsWith('blob:')) {{
                                        try {{
                                            const canvas = document.createElement('canvas');
                                            canvas.width = i.naturalWidth || i.width;
                                            canvas.height = i.naturalHeight || i.height;
                                            const ctx = canvas.getContext('2d');
                                            ctx.drawImage(i, 0, 0);
                                            const toDataURL = window.__origToDataURL || canvas.toDataURL;
                                            return toDataURL.call(canvas, 'image/webp', 0.95);
                                        }} catch (e) {{
                                            return null;
                                        }}
                                    }}
                                    return i.src;
                                }}
                                return null;
                            }} catch (e) {{
                                return null;
                            }}
                        }})()"""
                    )
                except Exception as e:
                    logger.error(f"Page {page_num} extraction failed: {e}")
                    failed_pages.append(page_num)
                    continue
                    
                if extracted_url:
                    image_urls.append(extracted_url)
                    page_numbers.append(page_num)
                else:
                    logger.error(f"Page {page_num} failed to extract valid URL or data.")
                    failed_pages.append(page_num)
            
            return ChapterImageFetchReport(
                image_urls,
                page_count,
                skipped_pages,
                failed_pages,
                page_numbers,
            )
        finally:
            if owns_browser:
                await _close_comix_browser(browser)

    @classmethod
    def get_chapter_image_report(cls, chapter_id: int, manga_slug: str = None, chapter_number: str = None, headless: Optional[bool] = None) -> ChapterImageFetchReport:
        """Fetch image URLs / data URLs for a chapter with extraction diagnostics."""
        if headless is None:
            from ..utils.config import ConfigManager
            headless = ConfigManager().get("headless", True)
            
        if not manga_slug or not chapter_number:
            manga_slug = "manga"
            chapter_number = "1"
            
        chapter_url = f"https://comix.to/title/{manga_slug}/{chapter_id}-chapter-{chapter_number}"
        logger.info(f"Fetching chapter images via nodriver DOM (headless={headless}) for {chapter_url}...")
        
        report = ChapterImageFetchReport([])
        try:
            report = run_async(cls._get_chapter_images_async(chapter_id, manga_slug, chapter_number, headless))
        except Exception as e:
            logger.error(f"nodriver failed to fetch images for chapter {chapter_id}: {e}")
            
        logger.info(f"Retrieved {len(report.image_urls)} / {report.page_count} page images.")
        return report

    @classmethod
    def get_chapter_images(cls, chapter_id: int, manga_slug: str = None, chapter_number: str = None, headless: Optional[bool] = None) -> list[str]:
        """Fetch all image URLs / data URLs for a chapter using nodriver."""
        return cls.get_chapter_image_report(
            chapter_id,
            manga_slug=manga_slug,
            chapter_number=chapter_number,
            headless=headless,
        ).image_urls


class ChapterReaderService:
    """Serialize chapter extraction through one reusable browser process.

    The downloader still uses chapter workers and a shared image pool, but
    reader extraction is owned by this service so a twelve-chapter job cannot
    spawn twelve independent Chrome processes.  A dedicated event-loop thread
    keeps nodriver objects on the loop where they were created.
    """

    def __init__(self, headless: bool):
        self.headless = bool(headless)
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._browser = None
        self._thread = threading.Thread(
            target=self._run_loop,
            name="comix-chapter-reader",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("Chapter reader event loop did not start")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    async def _ensure_browser(self):
        if self._browser is None:
            self._browser = await _start_comix_browser(self.headless)
        return self._browser

    async def _fetch(self, chapter_id: int, manga_slug: str, chapter_number: str):
        # The lock is intentionally async: queued chapter tasks do not occupy
        # additional Chrome processes or threads while waiting their turn.
        if not hasattr(self, "_operation_lock"):
            self._operation_lock = asyncio.Lock()
        async with self._operation_lock:
            last_error = None
            for attempt in range(2):
                try:
                    browser = await self._ensure_browser()
                    return await ComixAPI._get_chapter_images_async(
                        chapter_id,
                        manga_slug,
                        chapter_number,
                        self.headless,
                        browser=browser,
                    )
                except Exception as exc:
                    last_error = exc
                    await _close_comix_browser(self._browser)
                    self._browser = None
                    if attempt == 0:
                        logger.warning(
                            "Chapter reader browser failed; restarting once: %s",
                            exc,
                        )
            raise last_error

    def fetch(self, chapter_id: int, manga_slug: str, chapter_number: str):
        if self._closed:
            raise RuntimeError("Chapter reader service is closed")
        future = asyncio.run_coroutine_threadsafe(
            self._fetch(chapter_id, manga_slug, chapter_number),
            self._loop,
        )
        return future.result()

    async def _shutdown(self) -> None:
        await _close_comix_browser(self._browser)
        self._browser = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
            future.result(timeout=10)
        except Exception as exc:
            logger.debug("Chapter reader shutdown failed: %s", exc)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
