"""Sentinel: goal -> browser agent -> screenshots -> S3 report, with history, diffs and monitors."""
import logging
import asyncio, base64, difflib, html, io, json, os, time, urllib.request, uuid
from contextlib import asynccontextmanager
from pathlib import Path

import boto3
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageChops, ImageFilter
from pydantic import BaseModel, Field, field_validator
from browser_use import Agent, Browser

load_dotenv()
logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
REPORTS, DATA = ROOT / "reports", ROOT / "data"
REPORTS.mkdir(exist_ok=True); DATA.mkdir(exist_ok=True)
MON_FILE = DATA / "monitors.json"
BUCKET = os.getenv("S3_BUCKET")
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
s3 = boto3.client("s3") if BUCKET else None


# ---------- storage: S3 when configured, local disk otherwise ----------
def put(key, body, ctype):
    if s3:
        s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType=ctype)
    else:
        p = REPORTS / key; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(body)

def get(key):
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() if s3 else (REPORTS / key).read_bytes()

def link(key):
    if s3:
        return s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=86400)
    return f"/reports/{key}"

def list_runs(limit=30):
    if s3:
        keys = [o["Key"] for pg in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET)
                for o in pg.get("Contents", []) if o["Key"].endswith("/meta.json")]
    else:
        keys = [str(p.relative_to(REPORTS)) for p in REPORTS.glob("*/meta.json")]
    return [json.loads(get(k)) for k in sorted(keys, reverse=True)[:limit]]

def public(m):
    m = dict(m)
    m["report_url"] = link(f"{m['id']}/report.html")
    if m.get("has_diff"):
        m["diff_url"] = link(f"{m['id']}/diff.png")
    return m


def public_monitor(m):
    m = dict(m)
    if m.get("last_run"):
        m["report_url"] = link(f"{m['last_run']}/report.html")
    return m


# ---------- screenshot diff ----------
def make_diff(prev: bytes, cur: bytes):
    a = Image.open(io.BytesIO(prev)).convert("RGB")
    b = Image.open(io.BytesIO(cur)).convert("RGB").resize(a.size)
    mask = ImageChops.difference(a, b).convert("L").point(lambda x: 255 if x > 30 else 0)
    pct = round(100 * mask.histogram()[255] / (a.width * a.height), 2)
    mask = mask.filter(ImageFilter.MaxFilter(9))  # thicken so small changes are visible
    out = Image.composite(Image.new("RGB", a.size, (255, 70, 70)), Image.blend(b, Image.new("RGB", b.size), .35), mask)
    buf = io.BytesIO(); out.save(buf, "PNG")
    return pct, buf.getvalue()


# ---------- agent ----------
def make_llm():
    p = os.getenv("LLM_PROVIDER", "openai").lower()
    if p == "bedrock":
        from browser_use.llm import ChatAWSBedrock
        return ChatAWSBedrock(model=os.getenv("BEDROCK_MODEL", "anthropic.claude-3-5-sonnet-20241022-v2:0"),
                              aws_region=os.getenv("AWS_REGION", "us-west-2"))
    if p == "gemini":
        if not os.getenv("GOOGLE_API_KEY"):
            raise RuntimeError("GOOGLE_API_KEY is required when LLM_PROVIDER=gemini.")
        from browser_use import ChatGoogle
        return ChatGoogle(model="gemini-2.5-flash")
    if p == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic.")
        from browser_use import ChatAnthropic
        return ChatAnthropic(model="claude-sonnet-4-5")
    if p == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required when LLM_PROVIDER=openai.")
        from browser_use import ChatOpenAI
        return ChatOpenAI(model="gpt-4.1")
    raise RuntimeError(f"Unsupported LLM_PROVIDER: {p}. Choose openai, gemini, anthropic, or bedrock.")


def provider_status():
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    requirements = {
        "openai": ("OPENAI_API_KEY",),
        "gemini": ("GOOGLE_API_KEY",),
        "anthropic": ("ANTHROPIC_API_KEY",),
        "bedrock": (),
    }
    if provider not in requirements:
        return {
            "ready": False,
            "provider": provider,
            "missing": [],
            "message": f"Unsupported model provider '{provider}'. Choose openai, gemini, anthropic, or bedrock.",
        }
    missing = [name for name in requirements[provider] if not os.getenv(name)]
    if missing:
        return {
            "ready": False,
            "provider": provider,
            "missing": missing,
            "message": f"Configure {', '.join(missing)} in your environment before starting a browser check.",
        }
    return {
        "ready": True,
        "provider": provider,
        "missing": [],
        "message": f"{provider.title()} is configured. Credentials are checked when a run starts.",
    }


def normalize_goal(goal):
    return " ".join(goal.split()).casefold()


def render_report(goal, summary, shots, urls):
    evidence = []
    for index in range(len(shots)):
        url = urls[index] if index < len(urls) else ""
        evidence.append(
            f'<figure><img loading="lazy" src="step-{index + 1}.png" '
            f'alt="Browser evidence, step {index + 1}"><figcaption>'
            f'Step {index + 1} · {html.escape(url or "Page URL unavailable")}</figcaption></figure>'
        )
    figures = "".join(evidence) or '<p class="empty">No screenshots were captured for this run.</p>'
    timestamp = time.strftime("%b %d, %Y at %H:%M UTC", time.gmtime())
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>Sentinel run report</title>
<style>
:root{{--ink:#152238;--muted:#617087;--line:#dce4ed;--surface:#fff;--canvas:#f4f7fa;--accent:#087f73}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--canvas);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:900px;margin:0 auto;padding:48px 24px 80px}}.eyebrow{{color:var(--accent);font-weight:700;letter-spacing:.1em;text-transform:uppercase;font-size:.76rem}}
h1{{font-size:clamp(2rem,5vw,3rem);line-height:1.1;letter-spacing:-.04em;margin:8px 0}}
.timestamp{{color:var(--muted);margin:0 0 28px}}section{{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:24px;margin:18px 0}}
h2{{font-size:1.1rem;margin:0 0 12px}}.goal{{font-weight:600}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:0}}
.evidence{{display:grid;gap:18px}}figure{{margin:0}}img{{display:block;width:100%;height:auto;border:1px solid var(--line);border-radius:10px}}
figcaption{{color:var(--muted);font-size:.85rem;margin-top:8px;overflow-wrap:anywhere}}.empty{{color:var(--muted)}}
@media(max-width:600px){{main{{padding:28px 16px 48px}}section{{padding:18px}}}}
</style></head><body><main>
<p class="eyebrow">Sentinel · Run report</p><h1>Browser monitoring report</h1>
<p class="timestamp">Generated {timestamp}</p>
<section><h2>Monitoring goal</h2><p class="goal">{html.escape(goal)}</p></section>
<section><h2>Agent summary</h2><pre>{html.escape(summary)}</pre></section>
<section><h2>Evidence</h2><div class="evidence">{figures}</div></section>
</main></body></html>"""

def archive(meta, shots, urls):
    """Write report, screenshots, diff vs the previous run of the same goal, and meta.json."""
    rid = meta["id"]
    put(f"{rid}/report.html", render_report(meta["goal"], meta["summary"], shots, urls).encode(), "text/html")
    for i, s in enumerate(shots):
        put(f"{rid}/step-{i+1}.png", base64.b64decode(s), "image/png")
    if shots:
        final = base64.b64decode(shots[-1])
        prev = next(
            (
                m
                for m in list_runs()
                if normalize_goal(m["goal"]) == normalize_goal(meta["goal"]) and m.get("has_final")
            ),
            None,
        )
        put(f"{rid}/final.png", final, "image/png")
        meta["has_final"] = True
        if prev:
            pct, img = make_diff(get(f"{prev['id']}/final.png"), final)
            put(f"{rid}/diff.png", img, "image/png")
            sim = difflib.SequenceMatcher(None, prev["summary"], meta["summary"]).ratio()
            meta.update(has_diff=True, prev_id=prev["id"], diff_pct=pct, text_sim=round(sim, 2),
                        changed=pct > 2 or sim < 0.85)
    put(f"{rid}/meta.json", json.dumps(meta).encode(), "application/json")

async def execute(goal, max_steps=15, on_step=None, headless=HEADLESS):
    task = f"{goal}\n\nWhen finished, give a concise factual summary including exact status wording from the page."
    agent = Agent(task=task, llm=make_llm(),
                  browser=Browser(headless=headless, enable_default_extensions=False))
    result = await agent.run(max_steps=max_steps, on_step_end=on_step)
    shots = [s for s in result.screenshots() if s]
    urls = list(result.urls())[-len(shots):] if shots else []
    meta = dict(id=f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}", goal=goal, ts=time.time(),
                summary=result.final_result() or "Agent finished without a summary.", changed=False)
    await asyncio.to_thread(archive, meta, shots, urls)
    return meta


# ---------- monitors ----------
class MonitorIn(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    minutes: int = Field(default=60, ge=1, le=10080)

    @field_validator("goal")
    @classmethod
    def goal_must_not_be_blank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Goal must contain non-whitespace characters.")
        return value

def load_mon():
    if not MON_FILE.exists():
        return []
    monitors = json.loads(MON_FILE.read_text(encoding="utf-8"))
    if not isinstance(monitors, list):
        raise ValueError(f"Monitor data in {MON_FILE} must be a JSON array.")
    return monitors

def save_mon(m):
    MON_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = MON_FILE.with_name(f"{MON_FILE.name}.tmp")
    temp_file.write_text(json.dumps(m), encoding="utf-8")
    temp_file.replace(MON_FILE)

def notify(text):
    url = os.getenv("ALERT_WEBHOOK_URL")
    if url:
        req = urllib.request.Request(url, json.dumps({"text": text, "content": text}).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass


async def run_monitor(monitor):
    meta = await execute(monitor["goal"], headless=True)
    update = dict(
        last_run=meta["id"],
        last_ts=meta["ts"],
        last_changed=meta["changed"],
        error=None,
        alert_error=None,
    )
    if meta["changed"]:
        try:
            await asyncio.to_thread(
                notify,
                f"Sentinel: change detected for '{monitor['goal']}'\n"
                f"{link(meta['id'] + '/report.html')}",
            )
        except Exception as e:
            logger.exception("Monitor alert delivery failed")
            update["alert_error"] = str(e)[:200]
    return update


async def monitor_loop():
    while True:
        try:
            if not provider_status()["ready"]:
                await asyncio.sleep(30)
                continue
            mons = load_mon()
            due = [m for m in mons if time.time() >= m["next"]]
            for m in due:
                m["next"] = time.time() + m["minutes"] * 60
            save_mon(mons)
            for m in due:
                try:
                    update = await run_monitor(m)
                    update["next"] = time.time() + m["minutes"] * 60
                except Exception as e:
                    logger.exception("Monitor run failed")
                    update = dict(
                        error=str(e)[:200],
                        alert_error=None,
                        next=time.time() + m["minutes"] * 60,
                    )
                mons = load_mon()
                for x in mons:
                    if x["id"] == m["id"]:
                        x.update(update)
                save_mon(mons)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Monitor scheduler cycle failed")
        await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(monitor_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="Sentinel", lifespan=lifespan)

@app.get("/api/status")
def status():
    configuration = provider_status()
    return {
        **configuration,
        "storage": "s3" if BUCKET else "local",
        "headless": HEADLESS,
    }


@app.get("/api/monitors")
def monitors():
    return [public_monitor(m) for m in load_mon()]

@app.post("/api/monitors")
def add_monitor(m: MonitorIn):
    configuration = provider_status()
    if not configuration["ready"]:
        raise HTTPException(status_code=503, detail=configuration["message"])
    mons = load_mon()
    mons.append(dict(id=uuid.uuid4().hex[:8], goal=m.goal, minutes=max(m.minutes, 5), next=time.time()))
    save_mon(mons)
    return [public_monitor(item) for item in mons]

@app.delete("/api/monitors/{mid}")
def del_monitor(mid: str):
    existing = load_mon()
    mons = [m for m in existing if m["id"] != mid]
    if len(mons) == len(existing):
        raise HTTPException(status_code=404, detail="Monitor was not found.")
    save_mon(mons)
    return [public_monitor(m) for m in mons]

@app.get("/api/history")
def history():
    return [public(m) for m in list_runs()]


# ---------- live run (SSE) ----------
def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"

@app.get("/api/run")
async def run(goal: str = Query(min_length=1, max_length=2000),
              max_steps: int = Query(default=15, ge=1, le=30)):
    goal = goal.strip()
    if not goal:
        return StreamingResponse(iter([sse("error", {"msg": "Goal must not be blank."})]),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    queue: asyncio.Queue = asyncio.Queue()

    async def on_step(agent):
        h = agent.history
        try:
            shots = [s for s in h.screenshots() if s]
            th = h.model_thoughts()
            await queue.put(sse("step", {"n": len(h.history), "url": (h.urls() or [""])[-1],
                                         "goal": getattr(th[-1], "next_goal", "") if th else "",
                                         "shot": shots[-1] if shots else None}))
        except Exception:
            logger.exception("Unable to stream browser step")

    async def work():
        try:
            meta = await execute(goal, max_steps, on_step)
            await queue.put(sse("done", public(meta) | {"storage": "s3" if BUCKET else "local"}))
        except Exception as e:
            await queue.put(sse("error", {"msg": str(e)}))
        finally:
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(work())
        try:
            while (msg := await queue.get()) is not None:
                yield msg
            await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/")
def index():
    candidates = [ROOT / "static" / "index.html", ROOT / "index.html"]
    for path in candidates:
        if path.exists():
            return FileResponse(path)
    raise FileNotFoundError("No frontend entry file found in the project root")


@app.get("/healthz")
def health():
    return {"status": "ok"}


app.mount("/reports", StaticFiles(directory=REPORTS), name="reports")
