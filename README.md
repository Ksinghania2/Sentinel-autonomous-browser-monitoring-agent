# Sentinel: autonomous browser monitoring agent

Type a goal in plain English. A [browser-use](https://github.com/browser-use/browser-use) agent opens a real Chromium window,
works through the page with a vision-language model, and Sentinel archives a report with screenshots to **AWS S3**
(returned as a presigned link). Watch every step live in the dashboard.

## Features
- **Live run view**: watch each agent step and browser frame stream in.
- **History**: every run is archived (S3 or local) and listed in the History tab.
- **Change detection**: each run is compared with the previous run of the same goal. Changed pixels are painted red and the summaries are compared too.
- **Monitors**: re-run a goal every 5 min to daily, headless. Changes can post to `ALERT_WEBHOOK_URL` (Slack/Discord compatible).
- **Docker + CI**: `docker build -t sentinel . && docker run -p 8000:8000 --env-file .env sentinel`. GitHub Actions lints on every push and publishes the image to GHCR from `main`.

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

Install Google Chrome or Chromium before starting Sentinel. Browser-use controls the installed browser through Chrome DevTools Protocol; a separate Playwright install is not required. Set `HEADLESS=true` for a machine without a desktop.

Choose one model provider in `.env`:
- `openai`: set `OPENAI_API_KEY`
- `gemini`: set `GOOGLE_API_KEY`
- `anthropic`: set `ANTHROPIC_API_KEY`
- `bedrock`: set `BEDROCK_MODEL`, `AWS_REGION`, and configure AWS credentials with Bedrock model access

Keep `.env` private; it is excluded from Git. The app reports a clear configuration error if the selected provider's required credentials are missing.

## AWS setup
1. Create a private S3 bucket and set `S3_BUCKET` in `.env`.
2. Credentials: `aws configure` (needs `s3:PutObject` and `s3:GetObject` on the bucket).
3. Using Bedrock? Set `LLM_PROVIDER=bedrock` and enable model access in the Bedrock console.

No bucket set? Reports are saved to `./reports` and served locally.

## Run with Docker

```bash
docker build -t sentinel .
docker run --rm -p 8000:8000 --env-file .env sentinel
```

The image installs Chromium and runs it headlessly. Add `-v sentinel-data:/app/data -v sentinel-reports:/app/reports` to retain monitors and local reports between container restarts.

## Notes
Local runs use a visible Chrome window by default. Docker sets `HEADLESS=true`; for cloud hosting, run on EC2 or ECS and configure model credentials and persistent storage.
