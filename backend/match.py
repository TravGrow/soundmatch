"""Turn an analysed clip into Pixabay search queries, then rank what comes back."""
from __future__ import annotations

import re
from collections import Counter

import numpy as np

from analyze import Features

# Pixabay's own genre facet names, so these queries hit curated browse pages.
PIXABAY_GENRES = [
    "Ambient", "Beats", "Cinematic", "Classical", "Corporate", "Country",
    "Electronic", "Folk", "Funk", "Hip Hop", "House", "Jazz", "Lofi",
    "Metal", "Pop", "Rock", "Techno", "Trap", "World",
]
# Each query costs a page load (~3s), so breadth is capped.
MAX_QUERIES = 12

PIXABAY_MOODS = [
    "Angry", "Bright", "Calm", "Dark", "Dramatic", "Epic", "Funny", "Happy",
    "Inspiring", "Laid Back", "Mysterious", "Peaceful", "Playful", "Relaxing",
    "Romantic", "Sad", "Serious", "Uplifting",
]


# ---------------------------------------------------------------- descriptors

def tempo_word(t: float) -> tuple[str, str]:
    """A search-friendly word and a display phrase for a tempo."""
    if t < 70:
        return "slow", "very slow"
    if t < 90:
        return "chill", "relaxed"
    if t < 110:
        return "steady", "moderate"
    if t < 130:
        return "upbeat", "upbeat"
    if t < 150:
        return "energetic", "energetic"
    return "fast", "very fast"


def describe(f: Features) -> dict:
    """Human-readable characterisation, used both for queries and for the UI."""
    tempo_word_, pace = tempo_word(f.tempo)

    c = f.centroid
    brightness = "dark" if c < 1200 else "warm" if c < 2000 else "bright" if c < 3200 else "crisp"
    energy = "gentle" if f.rms < 0.05 else "moderate" if f.rms < 0.12 else "powerful"
    texture = "melodic" if f.harmonic_ratio > 0.6 else "rhythmic" if f.harmonic_ratio < 0.4 else "balanced"
    density = "sparse" if f.onset_rate < 1.5 else "flowing" if f.onset_rate < 3.5 else "busy"

    return {
        "tempo_word": tempo_word_, "pace": pace, "brightness": brightness,
        "energy": energy, "texture": texture, "density": density,
        "tonality": "minor" if f.is_minor else "major",
    }


def guess_moods(f: Features, d: dict) -> list[str]:
    """Map measured features onto Pixabay's mood vocabulary."""
    moods: list[str] = []
    if f.is_minor and f.centroid < 1800:
        moods += ["Dark", "Sad"]
    if not f.is_minor and f.tempo > 110 and f.centroid > 2000:
        moods += ["Happy", "Uplifting"]
    if f.tempo < 90 and f.rms < 0.1:
        moods += ["Calm", "Relaxing", "Laid Back"]
    if f.tempo > 130 and f.rms > 0.1:
        moods += ["Dramatic", "Epic"]
    if d["texture"] == "melodic" and f.tempo < 100:
        moods.append("Peaceful")
    if f.centroid > 3000 and f.tempo > 100:
        moods.append("Bright")
    if not moods:
        moods = ["Calm", "Inspiring"]
    seen, out = set(), []
    for m in moods:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out[:3]


def tempo_hypotheses(tempo: float) -> list[float]:
    """The plausible readings of a tempo, most likely first.

    Beat trackers routinely report double or half the tempo a listener would
    tap -- a 76 BPM lofi track commonly reads as 152. Because tempo drives genre
    guessing and therefore which searches we run, committing to one reading lets
    a single octave error steer the whole search away from the right music. So
    we carry the alternative too, at reduced weight.
    """
    hyps = [tempo]
    if tempo > 130:
        hyps.append(tempo / 2)
    elif tempo < 70:
        hyps.append(tempo * 2)
    return hyps


def guess_genres(f: Features, d: dict, hints: list[str]) -> list[str]:
    """Heuristic genre guess, biased by any text hints from the source post."""
    scores = Counter()
    blob = " ".join(hints).lower()
    for g in PIXABAY_GENRES:
        if g.lower() in blob:
            scores[g] += 10

    for i, tempo in enumerate(tempo_hypotheses(f.tempo)):
        # The alternative octave is a fallback, so it votes at half strength.
        w = 1.0 if i == 0 else 0.5
        if tempo < 95 and f.harmonic_ratio > 0.45 and f.centroid < 2200:
            scores["Lofi"] += 4 * w
            scores["Beats"] += 2 * w
        if 115 <= tempo <= 135 and f.onset_rate > 2:
            scores["House"] += 3 * w
            scores["Electronic"] += 2 * w
        if tempo > 135 and f.rms > 0.1:
            scores["Techno"] += 3 * w
            scores["Electronic"] += 2 * w
        if 60 <= tempo <= 80 and f.harmonic_ratio < 0.5:
            scores["Trap"] += 3 * w
            scores["Hip Hop"] += 2 * w
        if f.harmonic_ratio > 0.7 and f.onset_rate < 2 and f.dynamic_range > 0.03:
            scores["Cinematic"] += 3 * w
            scores["Ambient"] += 2 * w
        if f.harmonic_ratio > 0.65 and tempo < 85 and f.centroid < 1600:
            scores["Ambient"] += 3 * w
        if 90 <= tempo <= 130 and f.centroid > 2200 and f.harmonic_ratio > 0.5:
            scores["Pop"] += 2 * w
            scores["Corporate"] += 1 * w
    if not scores:
        scores["Electronic"] += 1
    return [g for g, _ in scores.most_common(3)]


def build_queries(f: Features, meta: dict) -> tuple[list[tuple[str, str]], dict]:
    """Produce a prioritised list of (query, kind) searches.

    Known track titles come first -- a lot of social audio *is* Pixabay music, so
    a title search is the most direct route to an exact match.
    """
    d = describe(f)
    hints = [h for h in (meta.get("track"), meta.get("artist"), meta.get("genre"),
                         meta.get("post_title"), meta.get("album")) if h]
    genres = guess_genres(f, d, hints)
    moods = guess_moods(f, d)

    queries: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(q: str, kind: str = "q"):
        q = re.sub(r"\s+", " ", q).strip()
        if q and (q.lower(), kind) not in seen:
            seen.add((q.lower(), kind))
            queries.append((q, kind))

    # 1. Direct lookups from platform metadata.
    if meta.get("track"):
        add(meta["track"])
        if meta.get("artist"):
            add(f"{meta['track']} {meta['artist']}")
    elif meta.get("post_title"):
        # Post titles are noisy; keep the informative words only.
        words = re.findall(r"[A-Za-z][A-Za-z&'-]{2,}", meta["post_title"])
        stop = {"the", "and", "for", "with", "you", "your", "this", "that", "from",
                "video", "shorts", "reel", "tiktok", "official", "music"}
        keep = [w for w in words if w.lower() not in stop][:4]
        if len(keep) >= 2:
            add(" ".join(keep))

    # 2. Genre facets, then each genre crossed with every plausible tempo feel.
    # Crossing against both tempo readings matters: when a 76 BPM track is
    # measured at 152, only the alternative reading produces the "Lofi chill"
    # style query that actually surfaces it.
    for g in genres:
        add(g, "genre")
    words = list(dict.fromkeys(tempo_word(t)[0] for t in tempo_hypotheses(f.tempo)))
    for g in genres[:3]:
        for w in words:
            add(f"{g} {w}")
    # 3. Mood facets.
    for m in moods[:2]:
        add(m, "mood")
    # 4. Pure descriptor searches as a safety net.
    add(f"{d['tempo_word']} {d['brightness']} {genres[0]}")
    add(f"{d['energy']} {d['texture']} background music")

    # Each query is a separate page load, so cap the breadth.
    return queries[:MAX_QUERIES], {"descriptors": d, "genres": genres, "moods": moods}


# ------------------------------------------------------------------- scoring

def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if not na or not nb:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _tempo_score(t1: float, t2: float) -> float:
    """Compare tempos, treating half/double time as near-equivalent."""
    if t1 <= 0 or t2 <= 0:
        return 0.0
    best = 0.0
    for mult in (0.5, 1.0, 2.0):
        ratio = min(t1, t2 * mult) / max(t1, t2 * mult)
        # A 10% tempo gap should still score respectably.
        best = max(best, max(0.0, 1 - (1 - ratio) * 4))
    return best


def _chroma_score(c1: list, c2: list) -> tuple[float, int]:
    """Best pitch-class correlation over all 12 transpositions.

    Uses Pearson correlation rather than plain cosine: chroma vectors are all
    strictly positive and roughly uniform once averaged over time, so cosine
    saturates near 1.0 for *every* pair and carries no information. Centring
    each vector first is what makes the comparison discriminative.
    """
    a, b = np.array(c1, dtype=float), np.array(c2, dtype=float)
    a = a / (a.sum() or 1)
    b = b / (b.sum() or 1)
    a = a - a.mean()
    best, shift = -1.0, 0
    for k in range(12):
        rb = np.roll(b, k)
        s = _cos(a, rb - rb.mean())
        if s > best:
            best, shift = s, k
    # Correlation runs [-1, 1]; fold to [0, 1] where 0 means "unrelated".
    return max(0.0, best), shift


def fingerprint_match(fp_query: dict, fp_cand: dict) -> tuple[int, float]:
    """Count hashes that align at a consistent time offset.

    A large, sharply-peaked alignment histogram means the two files contain the
    same recording. Returns (peak alignment count, share of query hashes matched).
    """
    if not fp_query or not fp_cand:
        return 0, 0.0
    offsets = Counter()
    shared = 0
    for h, q_times in fp_query.items():
        c_times = fp_cand.get(h)
        if not c_times:
            continue
        shared += 1
        for qt in q_times[:6]:
            for ct in c_times[:6]:
                offsets[ct - qt] += 1
    if not offsets:
        return 0, 0.0
    peak = offsets.most_common(1)[0][1]
    return peak, shared / max(1, len(fp_query))


# How much each musical dimension contributes to the similarity score.
WEIGHTS = {
    "timbre": 0.34,
    "harmony": 0.18,
    "tempo": 0.20,
    "energy": 0.14,
    "rhythm": 0.09,
    "tags": 0.05,
}


def _timbre_vector(f: Features) -> np.ndarray:
    """MFCC means (instrument colour) and stds (how much it varies).

    Coefficient 0 is dropped because it only tracks overall loudness, which we
    already handle separately as energy.
    """
    return np.array(f.mfcc_mean[1:] + f.mfcc_std[1:], dtype=float)


def timbre_normalizer(pool: list[Features]) -> callable:
    """Build a z-scoring function from the spread of a candidate pool.

    MFCC coefficients differ in scale by two orders of magnitude, so a raw
    cosine between them is dominated by the first few dimensions and rates every
    pair of tracks as ~0.95 similar. Standardising each dimension against the
    pool puts them on equal footing, which is what makes timbre informative.
    """
    if not pool:
        return lambda f: _timbre_vector(f)
    mat = np.vstack([_timbre_vector(f) for f in pool])
    mean = mat.mean(axis=0)
    std = mat.std(axis=0)
    std[std < 1e-6] = 1.0  # a dimension with no spread carries no information
    return lambda f: (_timbre_vector(f) - mean) / std


def score_candidate(q: Features, c: Features, *, query_terms: set[str],
                    tags: list[str], normalize=None) -> dict:
    """Score one Pixabay track against the query clip across every dimension."""
    normalize = normalize or (lambda f: _timbre_vector(f))
    qv, cv = normalize(q), normalize(c)
    # Distance per dimension, so the scale is independent of vector length.
    dist = float(np.linalg.norm(qv - cv)) / np.sqrt(len(qv))
    # Decay chosen so ~1 pooled standard deviation of difference scores ~0.5.
    timbre = float(np.exp(-0.7 * dist))

    harmony, shift = _chroma_score(q.chroma, c.chroma)
    tempo = _tempo_score(q.tempo, c.tempo)

    # Energy: loudness and brightness proximity.
    rms_gap = abs(q.rms - c.rms) / max(q.rms, c.rms, 1e-6)
    cen_gap = abs(q.centroid - c.centroid) / max(q.centroid, c.centroid, 1e-6)
    energy = max(0.0, 1 - (rms_gap * 0.5 + cen_gap * 0.9))

    # Rhythm: note density and tonal/percussive balance.
    onset_gap = abs(q.onset_rate - c.onset_rate) / max(q.onset_rate, c.onset_rate, 1e-6)
    hr_gap = abs(q.harmonic_ratio - c.harmonic_ratio)
    rhythm = max(0.0, 1 - (onset_gap * 0.6 + hr_gap * 1.2))

    tag_words = {w.lower() for t in tags for w in re.findall(r"[a-z]+", t.lower())}
    tag_hit = len(tag_words & query_terms) / max(1, len(query_terms)) if query_terms else 0.0

    parts = {
        "timbre": timbre, "harmony": harmony, "tempo": tempo,
        "energy": energy, "rhythm": rhythm, "tags": min(1.0, tag_hit),
    }
    overall = sum(parts[k] * w for k, w in WEIGHTS.items())

    return {
        "score": round(overall * 100, 1),
        "parts": {k: round(v * 100, 1) for k, v in parts.items()},
        "key_shift": shift,
        "same_key": q.key == c.key,
    }


def classify(score: float, fp_peak: int, fp_share: float) -> tuple[str, str]:
    """Label a result: exact recording, near-identical, or degrees of similarity."""
    if fp_peak >= 25 and fp_share >= 0.08:
        return "exact", "Same recording - fingerprint aligned"
    if fp_peak >= 12:
        return "likely_exact", "Very likely the same recording"
    if score >= 88:
        return "very_close", "Extremely close match"
    if score >= 78:
        return "close", "Close match"
    if score >= 68:
        return "similar", "Similar feel"
    return "loose", "Loosely related"
