#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Seek.com.au Job Monitor Bot
Описание: Автоматизированный скрипт для мониторинга вакансий с обходом Cloudflare
и уведомлениями в Telegram. Предназначен для запуска в CI/CD (GitHub Actions).
"""

import os
from pathlib import Path
import json
import time
import logging
import random
import sys
import re
from typing import List, Set, Dict, Optional

import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
from requests.exceptions import RequestException

# ==========================================
# Конфигурация и Логирование
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("SeekBot")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Load .env if present
ENV_PATH = Path(BASE_DIR) / '.env'
if ENV_PATH.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=str(ENV_PATH))
        logger.info("Loaded environment from .env")
    except Exception:
        logger.info("python-dotenv not installed or failed to load .env")
# Allow overriding the Seek base URL (use NZ site by default for Auckland searches)
BASE_URL = os.environ.get("SEEK_BASE_URL", "https://www.seek.co.nz")
STATE_FILE = os.path.join(BASE_DIR, "data", "seen_jobs.json")
MAX_RETRIES = 5
RETRY_DELAY = 5  # base delay in seconds (will use exponential backoff)
LAST_RESPONSE_FILE = os.path.join(BASE_DIR, 'last_response.html')

# Environment / secrets
# DRY_RUN allows running without Telegram credentials for testing/parsing
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if DRY_RUN:
    logger.info("DRY_RUN enabled: Telegram notifications will be skipped.")
else:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.critical("Required environment variables TELEGRAM_TOKEN or TELEGRAM_CHAT_ID are not set.")
        # Exit with non-zero code so CI indicates misconfiguration
        sys.exit(1)

# Default to Manual Testing roles in Auckland. Can be overridden with env vars.
# Include common manual/QA role name variants; user can override with SEARCH_KEYWORDS env var.
SEARCH_KEYWORDS = os.environ.get(
    "SEARCH_KEYWORDS",
    "Manual Testing,Manual Tester,Manual QA,QA Tester,QA Analyst,Quality Analyst,Quality Assurance Tester,Test Analyst,Functional Tester,Regression Tester,Test Engineer (Manual),Manual QA Engineer"
).split(",")
SEARCH_LOCATION = os.environ.get("SEARCH_LOCATION", "Auckland")

# Exclude automation-related roles by keywords (can be overridden via env)
EXCLUDE_AUTOMATION_KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get(
        "EXCLUDE_AUTOMATION_KEYWORDS",
        "automation,automated,selenium,cucumber,playwright,robotframework,webdriver,pytest,protractor,qa automation,automation engineer,automation tester"
    ).split(",")
    if k.strip()
]


def looks_automated(job: Dict) -> bool:
    """Return True if job title or advertiser suggests an automation role."""
    title = (job.get('title') or '').lower()
    advertiser = (job.get('advertiser') or '').lower()
    for kw in EXCLUDE_AUTOMATION_KEYWORDS:
        if kw in title or kw in advertiser:
            return True
    return False


def matches_location(job: Dict, desired_location: str) -> bool:
    """Return True if the job location matches the desired location.

    Matching is case-insensitive and checks if the desired location token
    appears in the job's location string. If job location is Unknown or empty,
    treat as non-matching.
    """
    if not desired_location:
        return True
    loc = (job.get('location') or '').strip()
    if not loc or loc.lower() in ('unknown', 'n/a'):
        return False
    # allow multiple desired locations separated by comma
    desired_tokens = [d.strip().lower() for d in desired_location.split(',') if d.strip()]
    job_loc = loc.lower()
    for token in desired_tokens:
        if token in job_loc:
            return True
    return False


class SeekScraper:
    def __init__(self):
        # Create cloudscraper instance with desktop chrome fingerprint
        try:
            self.scraper = cloudscraper.create_scraper(
                browser={
                    'browser': 'chrome',
                    'platform': 'windows',
                    'desktop': True
                }
            )
        except Exception:
            # Fallback to default create_scraper
            self.scraper = cloudscraper.create_scraper()

        # Strengthen headers to look like a modern desktop Chrome browser.
        try:
            self.scraper.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-NZ,en;q=0.9,en-US;q=0.8',
                'Referer': BASE_URL,
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Site': 'same-origin',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-User': '?1',
                'Sec-Fetch-Dest': 'document',
                'Sec-CH-UA': '"Chromium";v="120", "Google Chrome";v="120", "Not A(Brand";v="24"',
                'Sec-CH-UA-Mobile': '?0',
                'Sec-CH-UA-Platform': '"Windows"'
            })
        except Exception:
            # If headers can't be set, proceed silently — cloudscraper will still attempt.
            pass

        self.proxies = self._get_proxies()

    def _get_proxies(self) -> Optional[Dict[str, str]]:
        proxy_url = os.environ.get("PROXY_URL")
        if proxy_url:
            return {"http": proxy_url, "https": proxy_url}
        return None

    def search(self, keyword: str, location: str) -> List[Dict]:
        # Use query-style search which is more stable across Seek domains
        # Build a query-parameter based URL to reliably target the correct site
        # and location (helps avoid redirects between AU/NZ domains).
        query = f"keywords={quote_plus(keyword)}&location={quote_plus(location)}"
        url = f"{BASE_URL}/jobs?{query}"

        logger.info(f"Requesting: {url}")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.scraper.get(url, proxies=self.proxies, timeout=30)
                # Save the latest response body to disk for debugging (truncated)
                try:
                    with open(LAST_RESPONSE_FILE, 'w', encoding='utf-8') as f:
                        f.write(resp.text[:200000])
                    logger.debug(f"Saved last response to {LAST_RESPONSE_FILE} (status {resp.status_code})")
                except Exception as e:
                    logger.debug(f"Failed to save last response: {e}")

                if resp.status_code == 200:
                    return self._parse_response(resp.text)
                elif resp.status_code in (403, 429):
                    logger.warning(f"Received {resp.status_code}. Attempt {attempt}/{MAX_RETRIES}")
                    # exponential backoff
                    time.sleep(RETRY_DELAY * (2 ** (attempt - 1)))
                    continue
                else:
                    logger.error(f"Unexpected HTTP status: {resp.status_code}")
                    break
            except RequestException as e:
                logger.error(f"Network error: {e}. Attempt {attempt}/{MAX_RETRIES}")
                time.sleep(RETRY_DELAY * (2 ** (attempt - 1)))
        return []

    def _parse_response(self, html: str) -> List[Dict]:
        soup = BeautifulSoup(html, 'lxml')
        jobs: List[Dict] = []

        # Try to extract window.SEEK_REDUX_DATA or similar embedded JSON
        scripts = soup.find_all('script')
        redux_json = None

        pattern = re.compile(r"window\.SEEK_REDUX_DATA\s*=\s*(\{.*?\})\s*;", re.S)
        for script in scripts:
            if not script.string:
                continue
            m = pattern.search(script.string)
            if m:
                candidate = m.group(1)
                # Clean trailing JS artifacts
                try:
                    redux_json = json.loads(candidate)
                    break
                except json.JSONDecodeError:
                    # Try a looser extraction by finding the first { ... } block
                    try:
                        start = candidate.find('{')
                        end = candidate.rfind('}')
                        redux_json = json.loads(candidate[start:end+1])
                        break
                    except Exception:
                        redux_json = None

        if redux_json:
            # Attempt to map to a list of jobs. Structure may vary; be defensive.
            try:
                results = redux_json
                # Common nesting paths
                for path in (['results', 'jobs'], ['search', 'results', 'jobs'], ['jobs']):
                    node = results
                    for key in path:
                        node = node.get(key, {}) if isinstance(node, dict) else {}
                    if isinstance(node, list) and node:
                        results_list = node
                        break
                else:
                    results_list = []

                for item in results_list:
                    try:
                        jid = str(item.get('id') or item.get('jobId') or item.get('job_id'))
                        jobs.append({
                            'id': jid,
                            'title': item.get('title') or item.get('occupation') or 'Unknown',
                            'advertiser': (item.get('advertiser') or {}).get('description') if isinstance(item.get('advertiser'), dict) else item.get('advertiser') or 'Unknown',
                            'location': item.get('location') or 'Unknown',
                            'salary': item.get('salary') or 'N/A',
                            'url': f"{BASE_URL}/job/{jid}",
                            'listingDate': item.get('listingDate') or item.get('postedDate') or 'Unknown'
                        })
                    except Exception:
                        continue
                if jobs:
                    logger.info(f"Found {len(jobs)} jobs via Redux extraction")
                    return jobs
            except Exception as e:
                logger.debug(f"Redux parsing fallback: {e}")

        # Fallback: DOM parsing. Seek pages may render job cards in various forms.
        # Strategy:
        #  - Find <article> job cards when present
        #  - Also consider any anchor linking to /job/<id> as a job entry
        #  - Extract id from data attributes or from the job link href
        candidates = []

        # collect article-based cards
        for a in soup.find_all('article'):
            candidates.append(a)

        # also collect any anchor that looks like a job link
        for a in soup.find_all('a', href=True):
            if re.search(r'/job/\d+', a['href']):
                # prefer the surrounding article if present, otherwise the anchor
                parent_article = a.find_parent('article')
                if parent_article is not None:
                    candidates.append(parent_article)
                else:
                    candidates.append(a)

        # de-duplicate candidate elements (by object id)
        unique_candidates = []
        seen_objs = set()
        for el in candidates:
            oid = id(el)
            if oid in seen_objs:
                continue
            seen_objs.add(oid)
            unique_candidates.append(el)

        logger.info(f"Falling back to DOM parsing. Candidate nodes: {len(unique_candidates)}")

        for node in unique_candidates:
            try:
                # Find a job link inside the node (or node itself if it's an <a>)
                link = None
                if node.name == 'a' and node.get('href'):
                    link = node
                else:
                    link = node.find('a', href=re.compile(r'/job/\d+'))

                job_id = None
                if link and link.get('href'):
                    m = re.search(r'/job/(\d+)', link['href'])
                    if m:
                        job_id = m.group(1)

                # fallback to data attributes
                if not job_id and getattr(node, 'get', None):
                    job_id = node.get('data-job-id') or node.get('data-automation-id')

                if not job_id:
                    # nothing we can do reliably
                    continue

                # Title: prefer the link text, then common heading tags, then aria-label
                title_text = None
                if link and link.get_text(strip=True):
                    title_text = link.get_text(strip=True)
                else:
                    h = node.find(['h1', 'h2', 'h3', 'h4'])
                    if h and h.get_text(strip=True):
                        title_text = h.get_text(strip=True)
                    else:
                        title_text = node.get('aria-label') or 'Unknown'

                # Advertiser/company: common selectors and attributes
                advertiser_tag = (
                    node.find(attrs={'data-automation': 'jobCompany'})
                    or node.find(class_=re.compile(r'company|employer', re.I))
                    or node.find('a', href=re.compile(r'/company|/employer'))
                )

                # Location: common selectors
                location_tag = (
                    node.find(attrs={'data-automation': 'jobLocation'})
                    or node.find(class_=re.compile(r'location', re.I))
                )

                # Salary/date: optional
                salary_tag = node.find(class_=re.compile(r'salary|package|remuneration', re.I))
                date_tag = node.find('time') or node.find(class_=re.compile(r'date|posted', re.I))

                jobs.append({
                    'id': str(job_id),
                    'title': title_text,
                    'advertiser': advertiser_tag.get_text(strip=True) if advertiser_tag else 'Unknown',
                    'location': location_tag.get_text(strip=True) if location_tag else 'Unknown',
                    'salary': salary_tag.get_text(strip=True) if salary_tag else 'N/A',
                    'url': (BASE_URL + link['href']) if link and link.get('href') and link['href'].startswith('/') else f"{BASE_URL}/job/{job_id}",
                    'listingDate': date_tag.get_text(strip=True) if date_tag else 'Unknown'
                })
            except Exception:
                continue

        return jobs


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.api_url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send_job(self, job: Dict):
        text = (
            f"🔥 <b>New Opportunity Found!</b>\n\n"
            f"💼 <b>{job.get('title')}</b>\n"
            f"🏢 {job.get('advertiser')}\n"
            f"📍 {job.get('location')}\n"
            f"💰 {job.get('salary')}\n"
            f"📅 {job.get('listingDate')}\n\n"
            f"<a href='{job.get('url')}'>🔗 View on Seek</a>"
        )

        payload = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True
        }

        if DRY_RUN:
            logger.info(f"DRY_RUN: would send message for job {job.get('id')}: {job.get('title')}")
            return

        try:
            import requests
            r = requests.post(self.api_url, data=payload, timeout=10)
            if r.status_code != 200:
                logger.error(f"Telegram API error: {r.status_code} {r.text}")
            # small throttle
            time.sleep(0.4)
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")


def load_state() -> Set[str]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(map(str, data))
        except Exception:
            return set()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    return set()


def save_state(seen_ids: Set[str]):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    limited_ids = list(seen_ids)[-2000:]
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(limited_ids, f, ensure_ascii=False, indent=2)


def main():
    logger.info("Starting Seek monitor...")
    scraper = SeekScraper()
    notifier = TelegramNotifier(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)

    seen_jobs = load_state()
    logger.info(f"Loaded {len(seen_jobs)} seen job ids")

    all_found_jobs: List[Dict] = []
    for keyword in SEARCH_KEYWORDS:
        k = keyword.strip()
        if not k:
            continue
        logger.info(f"Searching for: {k}")
        jobs = scraper.search(k, SEARCH_LOCATION)
        if jobs:
            all_found_jobs.extend(jobs)
        time.sleep(random.uniform(2, 5))

    # Deduplicate
    unique_jobs = list({j['id']: j for j in all_found_jobs if j.get('id')}.values())

    new_count = 0
    skipped_not_location = 0
    skipped_automation = 0
    for job in unique_jobs:
        jid = job.get('id')
        if not jid:
            continue
        if jid not in seen_jobs:
            # location filter
            if not matches_location(job, SEARCH_LOCATION):
                skipped_not_location += 1
                continue
            # automation exclusion
            if looks_automated(job):
                skipped_automation += 1
                continue

            logger.info(f"New job: {jid} - {job.get('title')}")
            notifier.send_job(job)
            seen_jobs.add(jid)
            new_count += 1

    if new_count:
        save_state(seen_jobs)
        logger.info(f"State updated, {new_count} new jobs saved")
    else:
        logger.info("No new jobs found")
    if skipped_not_location:
        logger.info(f"Skipped {skipped_not_location} jobs due to location filter (not {SEARCH_LOCATION})")
    if skipped_automation:
        logger.info(f"Skipped {skipped_automation} jobs due to automation exclusion")


if __name__ == '__main__':
    main()
