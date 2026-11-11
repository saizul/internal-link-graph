"""Polite, threaded, resumable site crawler.

Design notes
------------
* Worker threads only *fetch and parse*.  All bookkeeping (frontier, seen
  set, SQLite writes, the growing :class:`SiteData`) happens on the calling
  thread, which keeps the state handling free of locks.
* Redirects are followed by the crawler itself so that every hop can be
  recorded as a page with a ``redirect_target``.
* The frontier has two priorities: URLs discovered through links are crawled
  first, URLs that are only known from the sitemap afterwards.  This way a
  small ``max_pages`` budget is spent on the linked structure first while
  orphan candidates are still visited when budget remains.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from .fetcher import DEFAULT_USER_AGENT, Fetcher, Response, UrllibFetcher
from .html_extract import PageExtract, decode_body, extract_page, normalize_robots_directives
from .models import Link, SiteData
from .ratelimit import HostRateLimiter
from .robots import RobotsCache
from .sitemap import collect_sitemap_urls
from .storage import CrawlStore
from .urlnorm import (
    DEFAULT_OPTIONS,
    NormalizeOptions,
    host_of,
    is_internal,
    normalize_url,
    origin_of,
    url_extension,
)

DEFAULT_SKIP_EXTENSIONS = frozenset(
    """
    jpg jpeg png gif webp avif svg ico bmp tif tiff css js mjs json xml rss atom pdf doc docx
    xls xlsx ppt pptx zip rar gz tar 7z exe dmg apk mp3 mp4 m4a m4v mov avi wmv webm ogg wav
    woff woff2 ttf eot otf txt csv
    """.split()
)

PRIORITY_LINK = 0
PRIORITY_SITEMAP = 1

ProgressCallback = Callable[[str, str, str], None]


class CrawlError(RuntimeError):
    """Raised for unrecoverable crawl configuration or state problems."""


@dataclass
class CrawlConfig:
    """Settings for :class:`Crawler`."""

    start_url: str
    max_pages: int = 500
    max_depth: int | None = None
    delay: float = 0.5
    workers: int = 4
    user_agent: str = DEFAULT_USER_AGENT
    respect_robots: bool = True
    include: Sequence[str] = ()
    exclude: Sequence[str] = ()
    include_subdomains: bool = False
    use_sitemap: bool = True
    sitemap_urls: Sequence[str] = ()
    max_redirects: int = 10
    normalize: NormalizeOptions = DEFAULT_OPTIONS
    state_path: str | None = None
    resume: bool = False
    max_text_chars: int = 20000
    follow_nofollow: bool = True
    skip_extensions: frozenset[str] = DEFAULT_SKIP_EXTENSIONS
    timeout: float = 15.0

    def __post_init__(self) -> None:
        if self.max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        if self.delay < 0:
            raise ValueError("delay must be >= 0")
        if self.max_depth is not None and self.max_depth < 0:
            raise ValueError("max_depth must be >= 0")


@dataclass
class FetchOutcome:
    """What a worker thread found when it fetched one frontier URL."""

    requested: str
    final_url: str
    hops: list[tuple[str, int]] = field(default_factory=list)
    response: Response | None = None
    extract: PageExtract | None = None
    blocked: bool = False
    external: bool = False
    error: str = ""


@dataclass
class CrawlStats:
    """Counters describing a finished (or interrupted) crawl."""

    pages_crawled: int = 0
    redirects: int = 0
    errors: int = 0
    blocked_by_robots: int = 0
    frontier_remaining: int = 0
    sitemap_urls: int = 0
    elapsed: float = 0.0
    resumed: bool = False

    def to_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


class Crawler:
    """Crawl one site and return a :class:`SiteData`.

    ``fetcher`` defaults to the real :class:`UrllibFetcher`; pass a
    :class:`~internal_link_graph.fetcher.DictFetcher` (or any object with a
    ``fetch(url)`` method) to crawl an in-memory site.
    """

    def __init__(
        self,
        config: CrawlConfig,
        fetcher: Fetcher | None = None,
        rate_limiter: HostRateLimiter | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        start = normalize_url(config.start_url, options=config.normalize)
        if start is None:
            raise CrawlError(f"cannot crawl {config.start_url!r}: not an http(s) URL")
        self.config = config
        self.start_url = start
        self.fetcher: Fetcher = fetcher or UrllibFetcher(
            user_agent=config.user_agent, timeout=config.timeout
        )
        self.rate_limiter = rate_limiter or HostRateLimiter(config.delay)
        self.robots = RobotsCache(self.fetcher, config.user_agent, enabled=config.respect_robots)
        self.progress = progress
        try:
            self._include = [re.compile(pattern) for pattern in config.include]
            self._exclude = [re.compile(pattern) for pattern in config.exclude]
        except re.error as exc:
            raise CrawlError(f"invalid include/exclude pattern: {exc}") from exc

        self.site = SiteData(start_url=start)
        self.stats = CrawlStats()
        self._frontier: deque[tuple[str, int]] = deque()
        self._sitemap_queue: deque[tuple[str, int]] = deque()
        self._seen: set[str] = set()
        self._done: set[str] = set()
        self._crawled = 0
        self._store: CrawlStore | None = None

    # ------------------------------------------------------------------ API

    def crawl(self) -> SiteData:
        """Run the crawl to completion (or until ``max_pages``) and return the data."""
        started = time.monotonic()
        if self.config.state_path:
            self._store = CrawlStore(self.config.state_path)
        try:
            self._initialise()
            self._run()
        finally:
            self.stats.elapsed = time.monotonic() - started
            self.stats.frontier_remaining = len(self._frontier) + len(self._sitemap_queue)
            if self._store is not None:
                self._store.close()
                self._store = None
        return self._finalise()

    def allowed_by_filters(self, url: str) -> bool:
        """Apply the include / exclude regular expressions to a URL."""
        if self._include and not any(pattern.search(url) for pattern in self._include):
            return False
        return not any(pattern.search(url) for pattern in self._exclude)

    # ------------------------------------------------------------- lifecycle

    def _initialise(self) -> None:
        store = self._store
        if store is not None and self.config.resume and store.has_state():
            stored_start = store.get_meta("start_url")
            if stored_start != self.start_url:
                raise CrawlError(
                    f"state file belongs to {stored_start!r}, not {self.start_url!r}; "
                    "use a different --state file or drop --resume"
                )
            self._resume_from(store)
            return
        if store is not None:
            store.clear()
            store.set_meta("start_url", self.start_url)
        self._seen.add(self.start_url)
        self._frontier.append((self.start_url, 0))
        if store is not None:
            store.mark_seen([self.start_url])
            store.add_frontier([(self.start_url, 0)], PRIORITY_LINK)
        if self.config.use_sitemap:
            self._seed_from_sitemaps()
        if store is not None:
            store.commit()

    def _resume_from(self, store: CrawlStore) -> None:
        self.site = store.load_site()
        self.site.start_url = self.start_url
        self._seen = store.load_seen()
        self._done = {url for url, page in self.site.pages.items() if page.crawled}
        self._crawled = sum(
            1 for page in self.site.pages.values() if page.crawled and not page.is_redirect
        )
        self._frontier.extend(store.load_frontier(PRIORITY_LINK))
        self._sitemap_queue.extend(store.load_frontier(PRIORITY_SITEMAP))
        self.stats.resumed = True
        self.stats.sitemap_urls = len(self.site.sitemap_urls)
        self._emit("resume", self.start_url, f"{self._crawled} pages already crawled")

    def _seed_from_sitemaps(self) -> None:
        sitemap_urls = list(self.config.sitemap_urls)
        if not sitemap_urls:
            sitemap_urls = self.robots.sitemaps(self.start_url)
        if not sitemap_urls:
            sitemap_urls = [origin_of(self.start_url) + "/sitemap.xml"]
        result = collect_sitemap_urls(self.fetcher, sitemap_urls)
        for sitemap_url, error in result.errors.items():
            self._emit("sitemap-error", sitemap_url, error)
        seeds: list[tuple[str, int]] = []
        for raw in result.urls:
            url = normalize_url(raw, options=self.config.normalize)
            if url is None or not self._is_internal(url) or url in self.site.sitemap_urls:
                continue
            self.site.sitemap_urls.add(url)
            self.site.page(url).in_sitemap = True
            if url == self.start_url or not self.allowed_by_filters(url):
                continue
            if self.config.max_depth is not None and self.config.max_depth < 1:
                continue
            seeds.append((url, 1))
        self._sitemap_queue.extend(seeds)
        self.stats.sitemap_urls = len(self.site.sitemap_urls)
        if self._store is not None:
            self._store.add_sitemap_urls(self.site.sitemap_urls)
            self._store.add_frontier(seeds, PRIORITY_SITEMAP)
        self._emit("sitemap", self.start_url, f"{len(self.site.sitemap_urls)} URLs")

    def _finalise(self) -> SiteData:
        site = self.site
        site.ensure_link_targets()
        requested = self.start_url
        site.start_url = site.resolve_redirect(requested)
        self.stats.pages_crawled = self._crawled
        site.meta.update(
            {
                "source": "crawl",
                "requested_start_url": requested,
                "user_agent": self.config.user_agent,
                "max_pages": self.config.max_pages,
                "max_depth": self.config.max_depth,
                "stats": self.stats.to_dict(),
            }
        )
        return site

    # ------------------------------------------------------------- main loop

    def _next_item(self) -> tuple[str, int] | None:
        while self._frontier:
            url, depth = self._frontier.popleft()
            if url in self._done:
                self._forget(url)
                continue
            return url, depth
        while self._sitemap_queue:
            url, depth = self._sitemap_queue.popleft()
            if url in self._seen:
                self._forget(url)
                continue
            # Only remembered in memory: the store marks it once it is processed, so an
            # interrupted crawl retries the URL on resume.
            self._seen.add(url)
            return url, depth
        return None

    def _forget(self, url: str) -> None:
        if self._store is not None:
            self._store.remove_frontier(url)

    def _run(self) -> None:
        pending: dict[Future[FetchOutcome], tuple[str, int]] = {}
        with ThreadPoolExecutor(
            max_workers=self.config.workers, thread_name_prefix="ilg-crawl"
        ) as pool:
            try:
                while True:
                    while (
                        len(pending) < self.config.workers
                        and self._crawled + len(pending) < self.config.max_pages
                    ):
                        item = self._next_item()
                        if item is None:
                            break
                        pending[pool.submit(self._fetch_task, item[0])] = item
                    if not pending:
                        break
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    finished = sorted(done, key=lambda future: pending[future][0])
                    for future in finished:
                        url, depth = pending.pop(future)
                        self._process(url, depth, future.result())
                        self._forget(url)
                    if self._store is not None:
                        self._store.commit()
            except BaseException:
                for future in pending:
                    future.cancel()
                # Unprocessed in-flight URLs stay in the stored frontier for --resume.
                for url, depth in pending.values():
                    self._frontier.appendleft((url, depth))
                raise

    # ---------------------------------------------------------- worker thread

    def _is_internal(self, url: str) -> bool:
        return is_internal(url, self.start_url, self.config.include_subdomains)

    def _safe_fetch(self, url: str) -> Response:
        try:
            return self.fetcher.fetch(url)
        except Exception as exc:  # a misbehaving fetcher must not kill the crawl
            return Response(url=url, status=0, error=f"{type(exc).__name__}: {exc}")

    def _fetch_task(self, url: str) -> FetchOutcome:
        outcome = FetchOutcome(requested=url, final_url=url)
        current = url
        visited = {url}
        for _ in range(self.config.max_redirects + 1):
            outcome.final_url = current
            if not self.robots.allowed(current):
                outcome.blocked = True
                return outcome
            delay = self.config.delay
            robots_delay = self.robots.crawl_delay(current)
            if robots_delay is not None:
                delay = max(delay, robots_delay)
            self.rate_limiter.wait(host_of(current), delay)
            response = self._safe_fetch(current)
            if response.is_redirect and response.location:
                target = normalize_url(
                    response.location, base=current, options=self.config.normalize
                )
                outcome.hops.append((current, response.status))
                if target is None:
                    outcome.error = f"invalid redirect target {response.location!r}"
                    outcome.final_url = response.location
                    outcome.external = True
                    return outcome
                outcome.final_url = target
                if target in visited:
                    outcome.error = "redirect loop"
                    return outcome
                if not self._is_internal(target):
                    outcome.external = True
                    return outcome
                visited.add(target)
                current = target
                continue
            outcome.response = response
            if response.status == 200 and response.is_html and response.body:
                html = decode_body(response.body, response.header("content-type"))
                outcome.extract = extract_page(
                    html,
                    current,
                    options=self.config.normalize,
                    max_text_chars=self.config.max_text_chars,
                )
            return outcome
        outcome.error = f"more than {self.config.max_redirects} redirects"
        return outcome

    # ------------------------------------------------------ coordinating side

    def _emit(self, kind: str, url: str, detail: str = "") -> None:
        if self.progress is not None:
            self.progress(kind, url, detail)

    def _save_page(self, url: str) -> None:
        if self._store is not None:
            self._store.save_page(self.site.pages[url])

    def _set_depth(self, url: str, depth: int) -> None:
        page = self.site.page(url)
        if page.depth is None or depth < page.depth:
            page.depth = depth

    def _process(self, url: str, depth: int, outcome: FetchOutcome) -> None:
        site = self.site
        chain = [hop for hop, _ in outcome.hops] + [outcome.final_url]
        for index, (hop_url, status) in enumerate(outcome.hops):
            page = site.page(hop_url)
            page.status = status
            page.crawled = True
            page.redirect_target = chain[index + 1]
            page.redirect_chain = chain[index:]
            self._set_depth(hop_url, depth)
            self._seen.add(hop_url)
            self._done.add(hop_url)
            self.stats.redirects += 1
            self._save_page(hop_url)
            self._emit("redirect", hop_url, f"{status} -> {chain[index + 1]}")
        if self._store is not None and outcome.hops:
            self._store.mark_seen(chain[:-1])

        if outcome.error and outcome.response is None:
            # Redirect loop, too many hops or an unusable Location header.
            first = site.page(url)
            first.error = outcome.error
            first.crawled = True
            self._done.add(url)
            self.stats.errors += 1
            self._save_page(url)
            self._emit("error", url, outcome.error)
            return
        if outcome.external:
            return

        final = outcome.final_url
        if outcome.hops and final in self._done:
            return
        self._seen.add(final)
        self._done.add(final)
        if self._store is not None:
            self._store.mark_seen([final])
        self._crawled += 1
        page = site.page(final)
        page.crawled = True
        self._set_depth(final, depth)

        if outcome.blocked:
            page.blocked_by_robots = True
            page.error = "blocked by robots.txt"
            self.stats.blocked_by_robots += 1
            self._save_page(final)
            self._emit("blocked", final, "robots.txt")
            return

        response = outcome.response
        assert response is not None
        page.status = response.status
        page.content_type = response.content_type
        page.error = response.error
        page.x_robots = normalize_robots_directives(response.header("x-robots-tag"))
        if response.status == 0 or response.status >= 400:
            self.stats.errors += 1

        links: list[Link] = []
        extract = outcome.extract
        if extract is not None:
            page.title = extract.title
            page.h1 = extract.h1
            page.word_count = extract.word_count
            page.canonical = extract.canonical
            page.meta_robots = extract.meta_robots
            page.text = extract.text
            page_nofollow = page.nofollow
            new_frontier: list[tuple[str, int]] = []
            for found in extract.links:
                internal = self._is_internal(found.url)
                link = Link(
                    source=final,
                    target=found.url,
                    anchor=found.anchor,
                    rel=found.rel,
                    position=found.position,
                    is_image=found.is_image,
                    internal=internal,
                    nofollow=found.nofollow or page_nofollow,
                )
                links.append(link)
                if not internal:
                    continue
                site.page(found.url)
                if self._should_enqueue(link, depth + 1):
                    self._seen.add(found.url)
                    self._frontier.append((found.url, depth + 1))
                    new_frontier.append((found.url, depth + 1))
            site.links.extend(links)
            if self._store is not None:
                self._store.mark_seen([item[0] for item in new_frontier])
                self._store.add_frontier(new_frontier, PRIORITY_LINK)
        if self._store is not None:
            self._store.save_page(page)
            self._store.save_links(final, links)
        self._emit("page", final, f"{page.status} depth={depth} links={len(links)}")

    def _should_enqueue(self, link: Link, depth: int) -> bool:
        url = link.target
        if url in self._seen:
            return False
        if self.config.max_depth is not None and depth > self.config.max_depth:
            return False
        if link.nofollow and not self.config.follow_nofollow:
            return False
        if url_extension(url) in self.config.skip_extensions:
            return False
        return self.allowed_by_filters(url)


def crawl(
    start_url: str,
    fetcher: Fetcher | None = None,
    **options: object,
) -> SiteData:
    """Convenience wrapper: ``crawl("https://example.com/", max_pages=100)``."""
    config = CrawlConfig(start_url=start_url, **options)  # type: ignore[arg-type]
    return Crawler(config, fetcher=fetcher).crawl()
