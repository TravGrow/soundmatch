"""Name the actual song, when it is a commercially released one.

The Pixabay side of this app answers "what royalty-free music sounds like this?".
It cannot answer "what *is* this?", because it only ever compares against
Pixabay's catalogue. Shazam's fingerprint database covers commercial releases,
so a lookup here fills that gap -- and a confirmed title and artist is also the
single most useful thing we can feed into the Pixabay search.

Optional: if shazamio isn't installed or the lookup fails, everything downstream
still works, just without a name.
"""
from __future__ import annotations


def available() -> bool:
    try:
        import shazamio  # noqa: F401
        return True
    except Exception:
        return False


async def identify(audio_path: str) -> dict | None:
    """Look up a recording. Returns None when nothing is recognised."""
    try:
        from shazamio import Shazam
    except Exception:
        return None

    try:
        out = await Shazam().recognize(audio_path)
    except Exception:
        # Network failure or an API change shouldn't take the whole run down.
        return None

    track = (out or {}).get("track")
    if not track:
        return None

    # Album / label / year live in a loosely-typed "sections" blob.
    meta: dict[str, str] = {}
    for section in track.get("sections") or []:
        for item in section.get("metadata") or []:
            title, text = item.get("title"), item.get("text")
            if title and text:
                meta[title.lower()] = text

    return {
        "title": track.get("title"),
        "artist": track.get("subtitle"),
        "album": meta.get("album"),
        "label": meta.get("label"),
        "released": meta.get("released"),
        "genre": (track.get("genres") or {}).get("primary"),
        "url": track.get("url"),
        "cover": (track.get("images") or {}).get("coverart"),
    }
