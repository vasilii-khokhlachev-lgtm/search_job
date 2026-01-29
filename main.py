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
BASE_URL = "https://www.seek.com.au"
STATE_FILE = os.path.join(BASE_DIR, "data", "seen_jobs.json")
MAX_RETRIES = 3
RETRY_DELAY = 10  # seconds

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

SEARCH_KEYWORDS = os.environ.get("SEARCH_KEYWORDS", "Python Developer").split(",")
SEARCH_LOCATION = os.environ.get("SEARCH_LOCATION", "All Australia")


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

        self.proxies = self._get_proxies()

    def _get_proxies(self) -> Optional[Dict[str, str]]:
        proxy_url = os.environ.get("PROXY_URL")
        if proxy_url:
            return {"http": proxy_url, "https": proxy_url}
        return None

    def search(self, keyword: str, location: str) -> List[Dict]:
        k_slug = re.sub(r"\s+", "-", keyword.strip())
        l_slug = re.sub(r"\s+", "-", location.strip())
        url = f"{BASE_URL}/{k_slug}-jobs/in-{l_slug}"

        logger.info(f"Requesting: {url}")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.scraper.get(url, proxies=self.proxies, timeout=30)
                if resp.status_code == 200:
                    return self._parse_response(resp.text)
                elif resp.status_code in (403, 429):
                    logger.warning(f"Received {resp.status_code}. Attempt {attempt}/{MAX_RETRIES}")
                    time.sleep(RETRY_DELAY * attempt)
                    continue
                else:
                    logger.error(f"Unexpected HTTP status: {resp.status_code}")
                    break
            except RequestException as e:
                logger.error(f"Network error: {e}. Attempt {attempt}/{MAX_RETRIES}")
                time.sleep(RETRY_DELAY * attempt)
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

        # Fallback: DOM parsing using job-card articles
        articles = soup.find_all('article', attrs={'data-automation': 'job-card'})
        logger.info(f"Falling back to DOM parsing. Cards found: {len(articles)}")
        for card in articles:
            try:
                title_tag = card.find('a', attrs={'data-automation': 'jobTitle'})
                job_id = card.get('data-job-id') or card.get('data-automation-id')
                if not title_tag or not job_id:
                    continue
                advertiser_tag = card.find('a', attrs={'data-automation': 'jobCompany'})
                location_tag = card.find('a', attrs={'data-automation': 'jobLocation'})
                jobs.append({
                    'id': str(job_id),
                    'title': title_tag.get_text(strip=True),
                    'advertiser': advertiser_tag.get_text(strip=True) if advertiser_tag else 'Unknown',
                    'location': location_tag.get_text(strip=True) if location_tag else 'Unknown',
                    'salary': 'N/A',
                    'url': f"{BASE_URL}/job/{job_id}",
                    'listingDate': 'Unknown'
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
    for job in unique_jobs:
        jid = job.get('id')
        if not jid:
            continue
        if jid not in seen_jobs:
            logger.info(f"New job: {jid} - {job.get('title')}")
            notifier.send_job(job)
            seen_jobs.add(jid)
            new_count += 1

    if new_count:
        save_state(seen_jobs)
        logger.info(f"State updated, {new_count} new jobs saved")
    else:
        logger.info("No new jobs found")


if __name__ == '__main__':
    main()
