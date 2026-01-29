# Seek Job Monitor (GitHub Actions)

Repository contains a serverless job monitor for Seek.com.au that runs on GitHub Actions.

Files added:
- `main.py` — the scraper, notifier and state manager.
- `requirements.txt` — Python dependencies.
- `.github/workflows/monitor.yml` — workflow to run every 30 minutes.
- `data/seen_jobs.json` — persisted state (initially empty).

Setup
1. Create a GitHub repository and push this project.
2. In the repository Settings -> Secrets and variables -> Actions add:
   - `TELEGRAM_TOKEN` — your bot token from @BotFather
   - `TELEGRAM_CHAT_ID` — your chat id
   - (optional) `PROXY_URL` — e.g. http://user:pass@host:port
3. (optional) Add repository variables `SEARCH_KEYWORDS` and `SEARCH_LOCATION`.
4. Ensure Actions have Read and write permissions for contents (Settings -> Actions -> General).

Notes
- The scraper uses `cloudscraper` to bypass basic Cloudflare checks. If you encounter 403/429 frequently, consider using a residential proxy via `PROXY_URL` or a paid scraping API.
- This project is for educational purposes. Respect Seek's Terms of Service and robots.txt.
