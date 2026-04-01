from __future__ import annotations

import asyncio
import random
import re
from typing import Iterable
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from jobspy.model import JobPost, Location
from jobspy.util import create_logger

SITE_SEARCH_DOMAINS = {
    "linkedin": "linkedin.com/jobs/view",
    "indeed": "indeed.com/viewjob",
    "naukri": "naukri.com",
    "glassdoor": "glassdoor.co.in",
}

SITE_SELECTORS = {
    "linkedin": {
        "title": [
            "h1.top-card-layout__title",
            "h1.t-24",
            "h1",
        ],
        "company": [
            "a.topcard__org-name-link",
            ".topcard__flavor-row a",
            ".topcard__flavor",
        ],
        "location": [
            ".topcard__flavor--bullet",
            ".topcard__flavor-row .topcard__flavor",
        ],
        "description": [
            ".show-more-less-html__markup",
            ".description__text",
            "main",
        ],
    },
    "indeed": {
        "title": [
            "[data-testid='jobsearch-JobInfoHeader-title']",
            "h1",
        ],
        "company": [
            "[data-testid='inlineHeader-companyName']",
            "[data-company-name='true']",
        ],
        "location": [
            "[data-testid='job-location']",
            ".jobsearch-JobInfoHeader-subtitle div",
        ],
        "description": [
            "#jobDescriptionText",
            "[data-testid='jobsearch-JobComponent-description']",
            "main",
        ],
    },
    "naukri": {
        "title": [
            ".styles_jd-header-title__rZwM1",
            "h1",
        ],
        "company": [
            ".styles_jd-header-comp-name__MvqAI a",
            ".styles_jd-header-comp-name__MvqAI",
        ],
        "location": [
            ".styles_jhc__location__W_pVs",
            ".styles_jhc__loc___Du2H",
        ],
        "description": [
            ".styles_JDC__dang-inner-html__h0K4t",
            ".dang-inner-html",
            "main",
        ],
    },
    "glassdoor": {
        "title": [
            "h1[data-test='job-title']",
            "h1.heading_Heading__BqX5J",
            "h1",
        ],
        "company": [
            "[data-test='employer-name']",
            ".EmployerProfile_employerName__d1f8x",
        ],
        "location": [
            "[data-test='location']",
            ".JobDetails_location__mSg5h",
        ],
        "description": [
            "[data-test='jobDescriptionContent']",
            ".JobDetails_jobDescription__uW_fK",
            "main",
        ],
    },
}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)

log = create_logger("PlaywrightFallback")


def get_jobs_with_playwright(
    search_term: str,
    location: str,
    site_name: str = "linkedin",
    results_wanted: int = 15,
    headless: bool = True,
    pause_on_login: bool = False,
    pause_on_captcha: bool = False,
) -> list[JobPost]:
    try:
        return asyncio.run(
            _get_jobs_with_playwright(
                search_term=search_term,
                location=location,
                site_name=site_name,
                results_wanted=results_wanted,
                headless=headless,
                pause_on_login=pause_on_login,
                pause_on_captcha=pause_on_captcha,
            )
        )
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _get_jobs_with_playwright(
                    search_term=search_term,
                    location=location,
                    site_name=site_name,
                    results_wanted=results_wanted,
                    headless=headless,
                    pause_on_login=pause_on_login,
                    pause_on_captcha=pause_on_captcha,
                )
            )
        finally:
            loop.close()


async def _get_jobs_with_playwright(
    search_term: str,
    location: str,
    site_name: str,
    results_wanted: int,
    headless: bool,
    pause_on_login: bool,
    pause_on_captcha: bool,
) -> list[JobPost]:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run `pip install playwright` "
            "and `playwright install chromium`."
        ) from exc

    query = _build_search_query(search_term=search_term, location=location, site_name=site_name)
    jobs: list[JobPost] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless, slow_mo=75)
        context = await browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        await page.goto("https://www.google.com/ncr", wait_until="domcontentloaded")
        await _human_pause()
        await _accept_google_consent(page)
        await _handle_google_captcha(
            page=page,
            pause_on_captcha=pause_on_captcha,
            headless=headless,
        )

        search_box = page.locator("textarea[name='q'], input[name='q']").first
        await search_box.wait_for(timeout=15000)
        await search_box.click()
        await _human_pause(0.3, 0.8)
        await search_box.fill(query)
        await _human_pause(0.4, 0.9)
        await search_box.press("Enter")
        await page.wait_for_load_state("domcontentloaded")
        await _human_pause(1.2, 2.6)
        await _handle_google_captcha(
            page=page,
            pause_on_captcha=pause_on_captcha,
            headless=headless,
        )

        for _ in range(4):
            await page.mouse.wheel(0, random.randint(1200, 2400))
            await _human_pause(0.9, 1.8)

        links = await _extract_search_results(
            page=page,
            site_name=site_name,
            results_limit=max(results_wanted * 2, 10),
        )

        for title_hint, job_url in links[:results_wanted]:
            detail_page = await context.new_page()
            try:
                await detail_page.goto(
                    job_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await _human_pause(1.2, 2.3)

                if pause_on_login and site_name == "linkedin" and "login" in detail_page.url.lower():
                    await detail_page.pause()

                await _soft_scroll(detail_page)
                html = await detail_page.content()
                jobs.append(
                    _build_job_post(
                        html=html,
                        url=detail_page.url,
                        site_name=site_name,
                        location_hint=location,
                        title_hint=title_hint,
                    )
                )
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
            finally:
                await detail_page.close()

        await context.close()
        await browser.close()

    deduped: list[JobPost] = []
    seen_urls = set()
    for job in jobs:
        if not job.job_url or job.job_url in seen_urls:
            continue
        seen_urls.add(job.job_url)
        deduped.append(job)
    return deduped[:results_wanted]


def _build_search_query(search_term: str, location: str, site_name: str) -> str:
    domain = SITE_SEARCH_DOMAINS.get(site_name, site_name)
    query_parts = [
        f"site:{domain}",
        f'"{search_term}"',
    ]
    if location:
        query_parts.append(f'"{location}"')
    query_parts.append("jobs")
    return " ".join(query_parts)


async def _accept_google_consent(page) -> None:
    button_patterns = [
        re.compile(r"accept all", re.IGNORECASE),
        re.compile(r"i agree", re.IGNORECASE),
        re.compile(r"accept", re.IGNORECASE),
    ]
    for pattern in button_patterns:
        button = page.get_by_role("button", name=pattern).first
        try:
            if await button.count():
                await button.click(timeout=3000)
                await _human_pause(0.5, 1.0)
                return
        except Exception:
            continue


async def _handle_google_captcha(page, pause_on_captcha: bool, headless: bool) -> None:
    if not await _is_google_captcha_page(page):
        return

    log.warning("Google served a captcha or block page")
    if pause_on_captcha and not headless:
        log.warning("Pausing browser for manual captcha completion")
        await page.pause()
        await page.wait_for_load_state("domcontentloaded")
        if await _is_google_captcha_page(page):
            raise RuntimeError("Captcha page is still active after manual pause")
        return

    raise RuntimeError(
        "Google captcha detected. Re-run with `playwright_headless=False` "
        "and `playwright_pause_on_captcha=True` for manual solving."
    )


async def _extract_search_results(page, site_name: str, results_limit: int) -> list[tuple[str, str]]:
    anchors = page.locator("a:has(h3)")
    anchor_count = min(await anchors.count(), results_limit * 2)
    results: list[tuple[str, str]] = []
    seen = set()
    site_domain = SITE_SEARCH_DOMAINS.get(site_name, "")

    for index in range(anchor_count):
        anchor = anchors.nth(index)
        href = await anchor.get_attribute("href")
        title = await anchor.locator("h3").first.text_content()
        normalized_url = _normalize_google_result_url(href)
        if not normalized_url or not title:
            continue
        if site_domain and site_domain not in normalized_url:
            continue
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        results.append((title.strip(), normalized_url))
        if len(results) >= results_limit:
            break

    return results


async def _is_google_captcha_page(page) -> bool:
    url = page.url.lower()
    title = (await page.title()).lower()
    content = (await page.content()).lower()
    markers = [
        "sorry/index",
        "our systems have detected unusual traffic",
        "recaptcha",
        "/sorry/",
        "detected unusual traffic",
        "not a robot",
    ]
    haystacks = [url, title, content]
    return any(marker in haystack for marker in markers for haystack in haystacks)


def _normalize_google_result_url(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("/url?"):
        parsed = urlparse(href)
        actual_url = parse_qs(parsed.query).get("q", [None])[0]
        return actual_url
    if href.startswith("http"):
        return href
    return None


async def _soft_scroll(page) -> None:
    for _ in range(2):
        await page.mouse.wheel(0, random.randint(600, 1200))
        await _human_pause(0.4, 0.9)


async def _human_pause(minimum: float = 0.8, maximum: float = 1.8) -> None:
    await asyncio.sleep(random.uniform(minimum, maximum))


def _build_job_post(
    html: str,
    url: str,
    site_name: str,
    location_hint: str,
    title_hint: str,
) -> JobPost:
    soup = BeautifulSoup(html, "html.parser")
    selectors = SITE_SELECTORS.get(site_name, {})

    title = _first_text(soup, selectors.get("title", [])) or title_hint
    company_name = _first_text(soup, selectors.get("company", []))
    location_text = _first_text(soup, selectors.get("location", [])) or location_hint
    description = _first_text(soup, selectors.get("description", []), separator="\n")
    description = description[:8000] if description else None

    location = None
    if location_text:
        location = Location(country=location_hint or None, city=location_text)

    return JobPost(
        id=f"pw-{site_name}-{abs(hash(url))}",
        title=title,
        company_name=company_name,
        location=location,
        job_url=url,
        description=description,
        is_remote=_is_remote(title, description, location_text),
    )


def _first_text(
    soup: BeautifulSoup,
    selectors: Iterable[str],
    *,
    separator: str = " ",
) -> str | None:
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            text = element.get_text(separator=separator, strip=True)
            if text:
                return text
    return None


def _is_remote(title: str | None, description: str | None, location_text: str | None) -> bool:
    haystack = " ".join(
        value for value in [title or "", description or "", location_text or ""] if value
    ).lower()
    return "remote" in haystack or "work from home" in haystack or "hybrid" in haystack
