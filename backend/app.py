"""SoundMatch API.

Pipeline: social link -> audio -> acoustic analysis -> Pixabay search ->
download candidates -> score every candidate against the clip -> ranked results.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import certs
import extract
import identify
import match
from analyze import Features
from featstore import analyze_cached, warm
from pixabay import PixabayMusic, download_preview

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
JOBS_DIR = CACHE / "jobs"
FEATS_DIR = CACHE / "features"
WEB = ROOT / "web"
for d in (CACHE, JOBS_DIR, FEATS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# How many Pixabay tracks we download and fully analyse per request. Each one
# costs a download plus a few seconds of DSP, so this is the main speed dial.
MAX_CANDIDATES = 80
ANALYSIS_WORKERS = 8

app = FastAPI(title="SoundMatch")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Audio analysis is CPU-bound and largely holds the GIL, so threads serialise
# it. Real processes are what make examining ~80 candidates practical.
_io_pool = ThreadPoolExecutor(max_workers=4)
_cpu_pool: ProcessPoolExecutor | None = None


def cpu_pool() -> ProcessPoolExecutor:
    global _cpu_pool
    if _cpu_pool is None:
        _cpu_pool = ProcessPoolExecutor(max_workers=ANALYSIS_WORKERS,
                                        initializer=warm)
    return _cpu_pool
_browser: PixabayMusic | None = None
_browser_lock = asyncio.Lock()


# ------------------------------------------------------------------ job state

@dataclass
class Job:
    id: str
    url: str
    status: str = "queued"
    events: list = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    def emit(self, stage: str, message: str, **extra):
        ev = {"stage": stage, "message": message, "t": round(time.time(), 3), **extra}
        self.events.append(ev)
        self.queue.put_nowait(ev)


JOBS: dict[str, Job] = {}


class AnalyzeRequest(BaseModel):
    url: str
    max_candidates: int | None = None


# ------------------------------------------------------------------- helpers

async def get_browser() -> PixabayMusic:
    """One shared headless browser, started on first use."""
    global _browser
    async with _browser_lock:
        if _browser is None:
            px = PixabayMusic()
            await px.__aenter__()
            _browser = px
        return _browser


async def run_pipeline(job: Job, max_candidates: int):
    work_dir = JOBS_DIR / job.id
    work_dir.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()

    # 1. Audio out of the social post -------------------------------------
    job.status = "running"
    job.emit("extract", "Fetching audio from the link…")
    meta = await loop.run_in_executor(
        _io_pool, lambda: extract.extract_audio(job.url, work_dir,
                                             progress=lambda m, tr=False: job.emit("extract", m, transient=tr)))
    label = meta.get("track") or meta.get("post_title") or "audio"
    job.emit("extract", f"Got audio from {meta.get('extractor') or 'the post'}: {label}")

    # 1b. Name the recording ------------------------------------------------
    # Platforms often expose no music metadata at all (Instagram Reels commonly
    # expose none), so ask Shazam. A confirmed title is both the answer to
    # "what is this song?" and the best possible Pixabay search term.
    ident = None
    if identify.available():
        job.emit("identify", "Identifying the track…")
        ident = await identify.identify(meta["audio_path"])
        if ident:
            meta.setdefault("track", None)
            meta["track"] = ident.get("title") or meta.get("track")
            meta["artist"] = ident.get("artist") or meta.get("artist")
            meta["album"] = ident.get("album") or meta.get("album")
            meta["genre"] = ident.get("genre") or meta.get("genre")
            job.emit("identify",
                     f"Identified: {ident['title']} — {ident['artist']}", identified=ident)
        else:
            job.emit("identify", "No commercial release matched — matching on sound alone")

    # 2. What does it sound like? -----------------------------------------
    job.emit("analyze", "Analysing tempo, key, timbre and energy…")
    qf: Features = await loop.run_in_executor(cpu_pool(), analyze_cached, meta["audio_path"])
    desc = match.describe(qf)
    job.emit("analyze",
             f"{qf.tempo:.0f} BPM · key {qf.key} · {desc['brightness']}, {desc['energy']}",
             features=qf.to_dict(), descriptors=desc)

    # 3. Decide what to search for ----------------------------------------
    queries, plan = match.build_queries(qf, meta)
    job.emit("search", "Searching Pixabay: " + ", ".join(q for q, _ in queries[:5]),
             queries=[{"q": q, "kind": k} for q, k in queries], plan=plan)

    # 4. Scrape Pixabay ----------------------------------------------------
    px = await get_browser()
    tracks = await px.search_many(queries, pages=1,
                                  progress=lambda m: job.emit("search", m))
    if not tracks:
        raise RuntimeError("Pixabay returned no tracks for any query.")
    job.emit("search", f"Found {len(tracks)} candidate tracks")

    # When trimming to the analysis budget, prefer tracks several queries agreed
    # on, then Pixabay's own relevance order. Popularity is deliberately not used
    # here: a track's like count says nothing about whether it sounds like the
    # clip, and ranking by it buries the real match under famous unrelated songs.
    tracks.sort(key=lambda t: (-len(t.get("also_matched", [])), t.get("rank", 999)))
    tracks = tracks[:max_candidates]

    # 5. Download previews -------------------------------------------------
    job.emit("download", f"Downloading {len(tracks)} previews…")
    sem = asyncio.Semaphore(6)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                 verify=certs.ssl_context()) as client:
        async def grab(t):
            async with sem:
                return t, await download_preview(t, CACHE, client)
        pairs = await asyncio.gather(*(grab(t) for t in tracks))
    pairs = [(t, p) for t, p in pairs if p]
    job.emit("download", f"Downloaded {len(pairs)} previews")

    # 6. Analyse each candidate -------------------------------------------
    job.emit("compare", f"Comparing {len(pairs)} tracks against your clip…")
    done = 0

    async def analyse_one(t, p):
        nonlocal done
        try:
            f = await loop.run_in_executor(cpu_pool(), analyze_cached, p)
        except Exception:
            return None
        done += 1
        if done % 5 == 0:
            job.emit("compare", f"Analysed {done}/{len(pairs)}")
        return t, f, p

    analysed = [r for r in await asyncio.gather(*(analyse_one(t, p) for t, p in pairs)) if r]

    # 7. Score and rank ----------------------------------------------------
    job.emit("compare", "Scoring similarity…")
    normalize = match.timbre_normalizer([f for _, f, _ in analysed])
    query_terms = {w.lower() for q, _ in queries for w in q.split()}

    results = []
    for t, f, p in analysed:
        sc = match.score_candidate(qf, f, query_terms=query_terms,
                                   tags=t.get("tags") or [], normalize=normalize)
        peak, share = match.fingerprint_match(qf.fingerprint, f.fingerprint)
        label, why = match.classify(sc["score"], peak, share)
        results.append({
            "id": t["id"],
            "name": t["name"],
            "url": f"https://pixabay.com{t['href']}" if t.get("href") else None,
            "audio": t["src"],
            "download": f"https://pixabay.com{t['downloadUrl']}" if t.get("downloadUrl") else t["src"],
            "thumbnail": t.get("thumbnail"),
            "duration": t.get("duration"),
            "tags": t.get("tags") or [],
            "likes": t.get("likes"),
            "ai_generated": t.get("isAiGenerated"),
            "attribution": t.get("attribution"),
            "matched_query": t.get("query"),
            "score": sc["score"],
            "parts": sc["parts"],
            "tempo": f.tempo,
            "key": f.key,
            "same_key": sc["same_key"],
            "fingerprint_peak": peak,
            "fingerprint_share": round(share, 4),
            "match_type": label,
            "match_reason": why,
        })

    # Confirmed same-recording hits outrank everything else.
    results.sort(key=lambda r: (r["fingerprint_peak"] >= 12, r["score"]), reverse=True)

    exact = [r for r in results if r["match_type"] in ("exact", "likely_exact")]
    job.result = {
        "url": job.url,
        "source": {k: meta.get(k) for k in
                   ("post_title", "uploader", "track", "artist", "album", "genre",
                    "extractor", "webpage_url", "thumbnail")},
        "identified": ident,
        "query_audio": f"/api/audio/{job.id}",
        "features": qf.to_dict(),
        "descriptors": desc,
        "plan": plan,
        "queries": [{"q": q, "kind": k} for q, k in queries],
        "exact_match": exact[0] if exact else None,
        "results": results,
        "counts": {"found": len(tracks), "analysed": len(analysed)},
    }
    job.status = "done"
    job.emit("done", ("Found an exact match!" if exact
                      else f"Ranked {len(results)} close alternatives"))


async def run_job(job: Job, max_candidates: int):
    try:
        await run_pipeline(job, max_candidates)
    except extract.ExtractionError as e:
        job.status, job.error = "error", str(e)
        job.emit("error", str(e))
    except Exception as e:  # noqa: BLE001 - surface anything else verbatim
        job.status, job.error = "error", f"{type(e).__name__}: {e}"
        job.emit("error", job.error)
    finally:
        job.queue.put_nowait(None)


# --------------------------------------------------------------------- routes

@app.post("/api/analyze")
async def start_analyze(req: AnalyzeRequest):
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Please paste a full http(s) link.")
    job = Job(id=uuid.uuid4().hex[:12], url=url)
    JOBS[job.id] = job
    asyncio.create_task(run_job(job, req.max_candidates or MAX_CANDIDATES))
    return {"job_id": job.id, "supported_guess": extract.looks_supported(url)}


@app.get("/api/stream/{job_id}")
async def stream(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")

    async def gen():
        for ev in list(job.events):          # replay anything already emitted
            yield f"data: {json.dumps(ev)}\n\n"
        if job.status in ("done", "error"):
            yield f"data: {json.dumps({'stage': '_end'})}\n\n"
            return
        while True:
            ev = await job.queue.get()
            if ev is None:
                yield f"data: {json.dumps({'stage': '_end'})}\n\n"
                return
            yield f"data: {json.dumps(ev)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/result/{job_id}")
async def result(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return {"status": job.status, "error": job.error, "result": job.result}


@app.get("/api/audio/{job_id}")
async def job_audio(job_id: str):
    """Serve the extracted clip so the UI can play it beside the matches."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    for p in sorted((JOBS_DIR / job_id).glob("source.*")):
        if p.suffix.lower() in (".wav", ".m4a", ".mp3", ".opus", ".webm"):
            return FileResponse(p)
    raise HTTPException(404, "No audio for this job")


@app.delete("/api/job/{job_id}")
async def drop_job(job_id: str):
    JOBS.pop(job_id, None)
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"ok": True, "jobs": len(JOBS), "extra_ca": certs.extra_ca_paths()}


@app.on_event("shutdown")
async def _shutdown():
    if _browser:
        await _browser.__aexit__()
    if _cpu_pool:
        _cpu_pool.shutdown(wait=False, cancel_futures=True)


app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")
