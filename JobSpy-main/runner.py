import csv
import random
import re
import time
from pathlib import Path

import pandas as pd

from jobspy import scrape_jobs

SEARCH_TERMS = [
    "machine learning engineer",
    "ai engineer",
    "ml engineer",
    "data scientist",
    "llm engineer",
    "nlp engineer",
    "computer vision engineer",
    "deep learning engineer",
]

TARGET_SITES = [
    "indeed",
    "linkedin",
    "google",
    "glassdoor",
    "naukri",
]

AI_ML_PATTERN = re.compile(
    r"\b(ai|ml|llm|nlp)\b|"
    r"artificial intelligence|"
    r"machine learning|"
    r"generative ai|"
    r"genai|"
    r"large language model|"
    r"computer vision|"
    r"deep learning|"
    r"data science",
    re.IGNORECASE,
)

USE_PROXIES = False
PROXY_FILE = Path(__file__).resolve().parent.parent / "proxies.txt"
PLAYWRIGHT_HEADLESS = False
PLAYWRIGHT_PAUSE_ON_LOGIN = True
PLAYWRIGHT_PAUSE_ON_CAPTCHA = True
QUERY_DELAY_RANGE_SECONDS = (6, 12)


def load_proxies() -> list[str] | None:
    if not USE_PROXIES:
        return None
    if not PROXY_FILE.exists():
        return None

    proxies = [
        line.strip()
        for line in PROXY_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return proxies or None


def build_google_query(search_term: str, location: str) -> str:
    return f"{search_term} jobs near {location} in the last 3 days"


def matches_ai_ml_role(row: pd.Series) -> bool:
    text = " ".join(
        str(row.get(column, "") or "")
        for column in ["title", "description", "job_function", "skills"]
    )
    return AI_ML_PATTERN.search(text) is not None


def collect_jobs() -> pd.DataFrame:
    all_jobs: list[pd.DataFrame] = []
    proxies = load_proxies()
    for search_term in SEARCH_TERMS:
        print(f"Searching for: {search_term}")
        jobs = scrape_jobs(
            site_name=TARGET_SITES,
            search_term=search_term,
            google_search_term=build_google_query(
                search_term=search_term,
                location="India",
            ),
            location="India",
            results_wanted=30,
            hours_old=72,
            country_indeed="India",
            linkedin_fetch_description=True,
            proxies=proxies,
            verbose=1,
            use_playwright_fallback=True,
            playwright_headless=PLAYWRIGHT_HEADLESS,
            playwright_pause_on_login=PLAYWRIGHT_PAUSE_ON_LOGIN,
            playwright_pause_on_captcha=PLAYWRIGHT_PAUSE_ON_CAPTCHA,
        )
        if not jobs.empty:
            jobs["search_term_used"] = search_term
            all_jobs.append(jobs)
        time.sleep(random.uniform(*QUERY_DELAY_RANGE_SECONDS))

    if not all_jobs:
        return pd.DataFrame()

    combined = pd.concat(all_jobs, ignore_index=True)

    combined = combined[
        combined.apply(matches_ai_ml_role, axis=1)
    ].copy()

    combined["dedupe_key"] = combined.apply(
        lambda row: row["job_url"]
        or row["job_url_direct"]
        or "|".join(
            [
                str(row.get("site", "") or "").strip().lower(),
                str(row.get("title", "") or "").strip().lower(),
                str(row.get("company", "") or "").strip().lower(),
            ]
        ),
        axis=1,
    )

    combined = combined.drop_duplicates(subset=["dedupe_key"]).drop(
        columns=["dedupe_key"]
    )

    if "date_posted" in combined.columns:
        combined = combined.sort_values(
            by=["date_posted", "site", "title"],
            ascending=[False, True, True],
            na_position="last",
        )

    return combined.reset_index(drop=True)


jobs = collect_jobs()
print(f"Found {len(jobs)} AI/ML jobs")
print(jobs.head(20))
jobs.to_csv(
    "ai_ml_jobs_india.csv",
    quoting=csv.QUOTE_NONNUMERIC,
    escapechar="\\",
    index=False,
)
