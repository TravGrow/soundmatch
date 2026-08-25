"""Pull the audio track (and any music metadata) out of a social media link."""
from __future__ import annotations

import re
from pathlib import Path

import yt_dlp

import certs

# Platforms yt-dlp handles that people actually paste.
SUPPORTED_HINTS = (
    "tiktok.com", "instagram.com", "youtube.com", "youtu.be", "facebook.com",
    "twitter.com", "x.com", "reddit.com", "vimeo.com", "soundcloud.com",
    "twitch.tv", "dailymotion.com", "snapchat.com", "pinterest.com",
)


class ExtractionError(RuntimeError):
    pass


def looks_supported(url: str) -> bool:
    return any(h in url.lower() for h in SUPPORTED_HINTS)


def _clean(value) -> str | None:
    """yt-dlp sometimes returns placeholder junk for missing music metadata."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if not v or v.lower() in {"none", "null", "original sound", "original audio", "sound"}:
        return None
    # TikTok often formats as "original sound - username"
    if re.match(r"^original sound\s*-", v, re.I):
        return None
    return v


def extract_audio(url: str, out_dir: str | Path, progress=None) -> dict:
    """Download the best audio stream and transcode to WAV for analysis.

    Returns a dict with the local ``audio_path`` plus whatever the platform told
    us about the music (title/artist), which is a strong hint for the search.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / "source"
    certs.install()

    def hook(d):
        if progress and d.get("status") == "downloading":
            pct = d.get("_percent_str", "").strip()
            # Transient: shown as live status, never appended to the step log.
            progress(f"Downloading audio {pct}", True)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(stem) + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "overwrites": True,
        "progress_hooks": [hook],
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
        }],
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise ExtractionError(_friendly_error(str(e), url)) from e

    audio_path = stem.with_suffix(".wav")
    if not audio_path.exists():
        # Fall back to whatever extension survived post-processing.
        candidates = sorted(out_dir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
        if not candidates:
            raise ExtractionError("Audio was downloaded but no file was produced.")
        audio_path = candidates[0]

    return {
        "audio_path": str(audio_path),
        "post_title": _clean(info.get("title")),
        "uploader": _clean(info.get("uploader")),
        "track": _clean(info.get("track")),
        "artist": _clean(info.get("artist")) or _clean(info.get("creator")),
        "album": _clean(info.get("album")),
        "genre": _clean(info.get("genre")),
        "description": (info.get("description") or "")[:500],
        "duration": info.get("duration"),
        "extractor": info.get("extractor_key"),
        "webpage_url": info.get("webpage_url") or url,
        "thumbnail": info.get("thumbnail"),
    }


def _friendly_error(raw: str, url: str) -> str:
    low = raw.lower()
    if "private" in low or "login" in low or "cookies" in low:
        return ("This post looks private or login-walled, so its audio can't be "
                "downloaded. Try a public post.")
    if "unsupported url" in low:
        return f"Nothing downloadable was found at {url}."
    if "video unavailable" in low or "404" in low:
        return "That post is unavailable — it may have been deleted."
    return f"Could not extract audio: {raw.splitlines()[-1][:200]}"
