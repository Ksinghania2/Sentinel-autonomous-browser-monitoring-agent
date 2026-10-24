"""Sentinel: goal -> browser agent -> screenshots -> S3 report, with history, diffs and monitors."""
import logging
import asyncio, base64, difflib, html, io, json, os, time, urllib.request, uuid
from contextlib import asynccontextmanager
from pathlib import Path

import boto3
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageChops, ImageFilter
from pydantic import BaseModel
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

def render_report(goal, summary, shots, urls):
    figs = "".join(f'<figure><img src="data:image/png;base64,{s}"><figcaption>{html.escape(u or "")}</figcaption></figure>'
                   for s, u in zip(shots, urls))
    return f"""<!doctype html><meta charset=utf-8><title>Sentinel report</title>
<style>body{{font:16px/1.6 system-ui;max-width:860px;margin:3rem auto;padding:0 1rem;color:#16202c}}
img{{width:100%;border:1px solid #ccd;border-radius:8px}}figcaption{{font-size:13px;color:#667}}
pre{{white-space:pre-wrap;background:#f3f6f9;padding:1rem;border-radius:8px}}</style>
<h1>Sentinel report</h1><p><b>Goal:</b> {html.escape(goal)}</p><h2>Summary</h2>
<pre>{html.escape(summary)}</pre><h2>Evidence</h2>{figs}"""

def archive(meta, shots, urls):
    """Write report, screenshots, diff vs the previous run of the same goal, and meta.json."""
    rid = meta["id"]
    put(f"{rid}/report.html", render_report(meta["goal"], meta["summary"], shots, urls).encode(), "text/html")
    for i, s in enumerate(shots):
        put(f"{rid}/step-{i+1}.png", base64.b64decode(s), "image/png")
    if shots:
        final = base64.b64decode(shots[-1])
        prev = next((m for m in list_runs() if m["goal"] == meta["goal"] and m.get("has_final")), None)
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
    goal: str
    minutes: int = 60

def load_mon():
    return json.loads(MON_FILE.read_text()) if MON_FILE.exists() else []

def save_mon(m):
    MON_FILE.write_text(json.dumps(m))

def notify(text):
    url = os.getenv("ALERT_WEBHOOK_URL")
    if url:
        req = urllib.request.Request(url, json.dumps({"text": text, "content": text}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)

async def monitor_loop():
    while True:
        mons = load_mon()
        due = [m for m in mons if time.time() >= m["next"]]
        for m in due:
            m["next"] = time.time() + m["minutes"] * 60
        save_mon(mons)
        for m in due:
            try:
                meta = await execute(m["goal"], headless=True)
                update = dict(last_run=meta["id"], last_ts=meta["ts"], last_changed=meta["changed"], error=None)
                if meta["changed"]:
                    await asyncio.to_thread(notify, f"Sentinel: change detected for '{m['goal']}'\n{link(meta['id'] + '/report.html')}")
            except Exception as e:
                update = dict(error=str(e)[:200])
            mons = load_mon()
            for x in mons:
                if x["id"] == m["id"]:
                    x.update(update)
            save_mon(mons)
        await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(monitor_loop())
    yield
    task.cancel()

app = FastAPI(title="Sentinel", lifespan=lifespan)

@app.get("/api/monitors")
def monitors():
    return load_mon()

@app.post("/api/monitors")
def add_monitor(m: MonitorIn):
    mons = load_mon()
    mons.append(dict(id=uuid.uuid4().hex[:8], goal=m.goal, minutes=max(m.minutes, 5), next=time.time()))
    save_mon(mons)
    return mons

@app.delete("/api/monitors/{mid}")
def del_monitor(mid: str):
    save_mon([m for m in load_mon() if m["id"] != mid])
    return load_mon()

@app.get("/api/history")
def history():
    return [public(m) for m in list_runs()]


# ---------- live run (SSE) ----------
def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"

@app.get("/api/run")
async def run(goal: str, max_steps: int = 15):
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

    return StreamingResponse(stream(), media_type="text/event-stream")

@app.get("/")
def index():
    candidates = [ROOT / "static" / "index.html", ROOT / "index.html"]
    for path in candidates:
        if path.exists():
            return FileResponse(path)
    raise FileNotFoundError("No frontend entry file found in the project root")

app.mount("/reports", StaticFiles(directory=REPORTS), name="reports")
