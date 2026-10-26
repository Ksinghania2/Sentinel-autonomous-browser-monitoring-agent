# Sentinel: autonomous browser monitoring agent

Describe a check in plain English. A [browser-use](https://github.com/browser-use/browser-use) agent navigates a real Chromium browser, streams its progress to the dashboard, and archives a report with screenshots to **AWS S3** or local storage.

## Features
- **Live browser session**: follow the current page and agent activity as the check runs.
- **Run history and reports**: review saved summaries, screenshots, and report links.
- **Change detection**: compare screenshots and summaries with the previous run of the same goal.
- **Scheduled monitors**: re-run a goal every 5 minutes, 15 minutes, hourly, or daily. Changes can post to `ALERT_WEBHOOK_URL` (Slack/Discord-compatible).
- **Container health**: Docker installs Chromium, runs the app as a non-root user, and provides a `/healthz` health check.

## Run it (VS Code)

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                                  # Windows; add your key to .env
# macOS/Linux: cp .env.example .env
uvicorn main:app --reload
```
Open http://localhost:8000.

Run the test suite with `pip install -r requirements-dev.txt` followed by `python -m pytest -q`.

Install Google Chrome or Chromium before starting Sentinel. Browser-use controls the installed browser through Chrome DevTools Protocol; a separate Playwright install is not required. Set `HEADLESS=true` for a machine without a desktop.

Choose one model provider in `.env`:
- `openai`: set `OPENAI_API_KEY`
- `gemini`: set `GOOGLE_API_KEY`
- `anthropic`: set `ANTHROPIC_API_KEY`
- `bedrock`: set `BEDROCK_MODEL`, `AWS_REGION`, and configure AWS credentials with Bedrock model access

Keep `.env` private; it is excluded from Git. The app reports a clear configuration error if the selected provider's required credentials are missing.
The dashboard checks `/api/status` before enabling runs or scheduled monitors. Add the selected provider's credential to `.env` and restart the app; a configured key is not considered verified until a real run succeeds.

## AWS setup
1. Create a private S3 bucket and set `S3_BUCKET` in `.env`.
2. Credentials: `aws configure` (needs `s3:PutObject` and `s3:GetObject` on the bucket).
3. Using Bedrock? Set `LLM_PROVIDER=bedrock` and enable model access in the Bedrock console.

No bucket set? Reports are saved to `./reports` and served locally.

## Run with Docker

Create `.env` from `.env.example` and add a key for the model provider you selected. Then:

```bash
docker build -t sentinel .
docker run --rm --name sentinel -p 8000:8000 --env-file .env -v sentinel-data:/app/data -v sentinel-reports:/app/reports sentinel
```

The image runs Chromium headlessly. The named volumes preserve scheduled monitors and locally stored reports across container restarts. Browse to http://localhost:8000; check container readiness with `docker inspect --format='{{.State.Health.Status}}' sentinel`.

## Notes
Local runs use a visible Chrome window by default. Docker sets `HEADLESS=true`. For cloud hosting, configure model credentials and persistent storage. Sentinel can navigate to URLs based on its task instructions; do not expose it publicly without adding appropriate authentication and network-access controls.
