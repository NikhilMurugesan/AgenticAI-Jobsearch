from __future__ import annotations

import math
import random
import time
from datetime import datetime, date, timedelta
from typing import Optional

import regex as re
import requests
from DrissionPage import ChromiumPage

from jobspy.exception import NaukriException
from jobspy.naukri.constant import headers as naukri_headers
from jobspy.naukri.util import (
    is_job_remote,
    parse_job_type,
    parse_company_industry,
)
from jobspy.model import (
    JobPost,
    Location,
    JobResponse,
    Country,
    Compensation,
    DescriptionFormat,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.util import (
    extract_emails_from_text,
    currency_parser,
    markdown_converter,
    create_session,
    create_logger,
)
import sys
from pathlib import Path

# Add the directory containing RecaptchaSolver to the Python path
sys.path.append(str(Path(__file__).parent.parent.parent / "GoogleRecaptchaBypass-main"))

from RecaptchaSolver import RecaptchaSolver


log = create_logger("Naukri")

class Naukri(Scraper):
    base_url = "https://www.naukri.com/jobapi/v3/search"
    delay = 3
    band_delay = 4
    jobs_per_page = 20

    def __init__(
        self, proxies: list[str] | str | None = None, ca_cert: str | None = None, user_agent: str | None = None
    ):
        """
        Initializes NaukriScraper with the Naukri API URL
        """
        super().__init__(Site.NAUKRI, proxies=proxies, ca_cert=ca_cert)
        self.driver = ChromiumPage()
        self.recaptcha_solver = RecaptchaSolver(self.driver)

        self.scraper_input = None
        self.country = "India"  #naukri is india-focused by default
        log.info("Naukri scraper initialized")
        log.info("Please ensure that DrissionPage, pydub, SpeechRecognition, and ffmpeg are installed for reCAPTCHA support.")

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes Naukri for jobs by navigating to the search page, solving CAPTCHA if necessary,
        and then using the session to fetch job listings from the API.
        """
        self.scraper_input = scraper_input
        self.driver.get(f"https://www.naukri.com/{scraper_input.search_term.lower().replace(' ', '-')}-jobs")
        
        try:
            self.recaptcha_solver.solveCaptcha()
            log.info("CAPTCHA solved successfully.")
        except Exception as e:
            log.error(f"CAPTCHA challenge failed: {e}")

        cookies = {cookie['name']: cookie['value'] for cookie in self.driver.get_cookies()}
        self.session = create_session(
            proxies=self.proxies,
            ca_cert=self.ca_cert,
            has_retry=True,
            delay=5,
            clear_cookies=True,
        )
        self.session.cookies.update(cookies)
        self.session.headers.update(naukri_headers)

        return self._fetch_job_posts(scraper_input)

    def _fetch_job_posts(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Fetches job posts from Naukri's API after the browser session is authenticated.
        """
        job_list: list[JobPost] = []
        seen_ids = set()
        start = scraper_input.offset or 0
        page = (start // self.jobs_per_page) + 1
        request_count = 0
        seconds_old = scraper_input.hours_old * 3600 if scraper_input.hours_old else None
        
        continue_search = (
            lambda: len(job_list) < scraper_input.results_wanted and page <= 50
        )

        while continue_search():
            request_count += 1
            log.info(f"Scraping page {request_count} for search term: {scraper_input.search_term}")
            
            params = self._build_api_params(scraper_input, page, seconds_old)
            
            try:
                response = self.session.get(self.base_url, params=params, timeout=10)
                if response.status_code != 200:
                    log.error(f"Naukri API request failed with status {response.status_code}: {response.text}")
                    break
                
                data = response.json()
                job_details = data.get("jobDetails", [])
                if not job_details:
                    log.info("No more job details found.")
                    break

                for job in job_details:
                    job_id = job.get("jobId")
                    if job_id and job_id not in seen_ids:
                        seen_ids.add(job_id)
                        job_post = self._process_job(job, job_id, scraper_input.linkedin_fetch_description)
                        if job_post:
                            job_list.append(job_post)
                            if len(job_list) >= scraper_input.results_wanted:
                                break
                
                if len(job_list) >= scraper_input.results_wanted:
                    break

            except requests.exceptions.RequestException as e:
                log.error(f"An error occurred during API request: {e}")
                break

            time.sleep(random.uniform(self.delay, self.delay + self.band_delay))
            page += 1

        return JobResponse(jobs=job_list[:scraper_input.results_wanted])

    def _build_api_params(self, scraper_input: ScraperInput, page: int, seconds_old: Optional[int]) -> dict:
        """Builds the parameter dictionary for the Naukri API request."""
        params = {
            "noOfResults": self.jobs_per_page,
            "urlType": "search_by_keyword",
            "searchType": "adv",
            "keyword": scraper_input.search_term,
            "pageNo": page,
            "k": scraper_input.search_term,
            "seoKey": f"{scraper_input.search_term.lower().replace(' ', '-')}-jobs",
            "src": "jobsearchDesk",
            "latLong": "",
            "location": scraper_input.location,
            "remote": "true" if scraper_input.is_remote else None,
        }
        if seconds_old:
            params["freshness"] = seconds_old // 86400

        return {k: v for k, v in params.items() if v is not None}

    def _process_job(
        self, job: dict, job_id: str, full_descr: bool
    ) -> Optional[JobPost]:
        """
        Processes a single job from API response into a JobPost object
        """
        title = job.get("title", "N/A")
        company = job.get("companyName", "N/A")
        company_url = f"https://www.naukri.com/{job.get('staticUrl', '')}" if job.get("staticUrl") else None

        location = self._get_location(job.get("placeholders", []))
        compensation = self._get_compensation(job.get("placeholders", []))
        date_posted = self._parse_date(job.get("footerPlaceholderLabel"), job.get("createdDate"))

        job_url = f"https://www.naukri.com{job.get('jdURL', f'/job/{job_id}')}"
        raw_description = job.get("jobDescription") if full_descr else None

        job_type = parse_job_type(raw_description) if raw_description else None
        company_industry = parse_company_industry(raw_description) if raw_description else None

        description = raw_description
        if description and self.scraper_input.description_format == DescriptionFormat.MARKDOWN:
            description = markdown_converter(description)

        is_remote = is_job_remote(title, description or "", location)
        company_logo = job.get("logoPathV3") or job.get("logoPath")

        # Naukri-specific fields
        skills = job.get("tagsAndSkills", "").split(",") if job.get("tagsAndSkills") else None
        experience_range = job.get("experienceText")
        ambition_box = job.get("ambitionBoxData", {})
        company_rating = float(ambition_box.get("AggregateRating")) if ambition_box.get("AggregateRating") else None
        company_reviews_count = ambition_box.get("ReviewsCount")
        vacancy_count = job.get("vacancy")
        work_from_home_type = self._infer_work_from_home_type(job.get("placeholders", []), title, description or "")

        job_post = JobPost(
            id=f"nk-{job_id}",
            title=title,
            company_name=company,
            company_url=company_url,
            location=location,
            is_remote=is_remote,
            date_posted=date_posted,
            job_url=job_url,
            compensation=compensation,
            job_type=job_type,
            company_industry=company_industry,
            description=description,
            emails=extract_emails_from_text(description or ""),
            company_logo=company_logo,
            skills=skills,
            experience_range=experience_range,
            company_rating=company_rating,
            company_reviews_count=company_reviews_count,
            vacancy_count=vacancy_count,
            work_from_home_type=work_from_home_type,
        )
        log.debug(f"Processed job: {title} at {company}")
        return job_post

    def _get_location(self, placeholders: list[dict]) -> Location:
        """
        Extracts location data from placeholders
        """
        location = Location(country=Country.INDIA)
        for placeholder in placeholders:
            if placeholder.get("type") == "location":
                location_str = placeholder.get("label", "")
                parts = location_str.split(", ")
                city = parts[0] if parts else None
                state = parts[1] if len(parts) > 1 else None
                location = Location(city=city, state=state, country=Country.INDIA)
                log.debug(f"Parsed location: {location.display_location()}")
                break
        return location

    def _get_compensation(self, placeholders: list[dict]) -> Optional[Compensation]:
        """
        Extracts compensation data from placeholders, handling Indian salary formats (Lakhs, Crores)
        """
        for placeholder in placeholders:
            if placeholder.get("type") == "salary":
                salary_text = placeholder.get("label", "").strip()
                if salary_text == "Not disclosed":
                    log.debug("Salary not disclosed")
                    return None

                # Handle Indian salary formats (e.g., "12-16 Lacs P.A.", "1-5 Cr")
                salary_match = re.match(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*(Lacs|Lakh|Cr)\s*(P\.A\.)?", salary_text, re.IGNORECASE)
                if salary_match:
                    min_salary, max_salary, unit = salary_match.groups()[:3]
                    min_salary, max_salary = float(min_salary), float(max_salary)
                    currency = "INR"

                    # Convert to base units (INR)
                    if unit.lower() in ("lacs", "lakh"):
                        min_salary *= 100000  # 1 Lakh = 100,000 INR
                        max_salary *= 100000
                    elif unit.lower() == "cr":
                        min_salary *= 10000000  # 1 Crore = 10,000,000 INR
                        max_salary *= 10000000

                    log.debug(f"Parsed salary: {min_salary} - {max_salary} INR")
                    return Compensation(
                        min_amount=int(min_salary),
                        max_amount=int(max_salary),
                        currency=currency,
                    )
                else:
                    log.debug(f"Could not parse salary: {salary_text}")
                    return None
        return None

    def _parse_date(self, label: str, created_date: int) -> Optional[date]:
        """
        Parses date from footerPlaceholderLabel or createdDate, returning a date object
        """
        today = datetime.now()
        if not label:
            if created_date:
                return datetime.fromtimestamp(created_date / 1000).date()  # Convert to date
            return None
        label = label.lower()
        if "today" in label or "just now" in label or "few hours" in label:
            log.debug("Date parsed as today")
            return today.date()
        elif "ago" in label:
            match = re.search(r"(\d+)\s*day", label)
            if match:
                days = int(match.group(1))
                parsed_date = (today - timedelta(days = days)).date()
                log.debug(f"Date parsed: {days} days ago -> {parsed_date}")
                return parsed_date
        elif created_date:
            parsed_date = datetime.fromtimestamp(created_date / 1000).date()
            log.debug(f"Date parsed from timestamp: {parsed_date}")
            return parsed_date
        log.debug("No date parsed")
        return None

    def _infer_work_from_home_type(self, placeholders: list[dict], title: str, description: str) -> Optional[str]:
        """
        Infers work-from-home type from job data (e.g., 'Hybrid', 'Remote', 'Work from office')
        """
        location_str = next((p["label"] for p in placeholders if p["type"] == "location"), "").lower()
        if "hybrid" in location_str or "hybrid" in title.lower() or "hybrid" in description.lower():
            return "Hybrid"
        elif "remote" in location_str or "remote" in title.lower() or "remote" in description.lower():
            return "Remote"
        elif "work from office" in description.lower() or not ("remote" in description.lower() or "hybrid" in description.lower()):
            return "Work from office"
        return None