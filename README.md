# SoundMatch

Paste a social media link. SoundMatch pulls the audio, works out what it sounds
like, searches Pixabay's royalty-free music library, and ranks what it finds —
telling you either *this is the same recording* or *these are the closest
royalty-free stand-ins*.

```bash
run.bat
```

Then open <http://127.0.0.1:8420>.

## Getting started

You need **Python 3.11+** and **ffmpeg**. ffmpeg is a system program, not a
Python package, so `pip` will not install it for you — this is the step people
most often miss.

<details>
<summary>Installing ffmpeg</summary>

- **Windows:** `winget install Gyan.FFmpeg --source winget`
- **macOS:** `brew install ffmpeg`
- **Linux:** `sudo apt install ffmpeg`

Check it worked with `ffmpeg -version`.
</details>

Then:

```bash
git clone <your-repo-url>
cd soundmatch
python -m venv .venv
```

Activate the environment.

Windows:

```bash
.venv\Scripts\activate
```

macOS / Linux:

```bash
source .venv/bin/activate
```

Then install everything:

```bash
pip install -r requirements.txt
playwright install chromium
```

That last command downloads a headless browser (~115 MB). It is required: the
app reads Pixabay's music results through a real browser, for the reasons in
[Limits worth knowing](#limits-worth-knowing).

### Running it

```bash
run.bat
```

(`./run.sh` on macOS/Linux.) Then open <http://127.0.0.1:8420> and paste a link.

The first search is slower than later ones — it builds a cache of analysed
tracks, and Pixabay results overlap a lot between searches.

### Troubleshooting

| Symptom | Cause |
|---|---|
| `ffmpeg not found` | ffmpeg isn't installed or isn't on your PATH |
| `Executable doesn't exist` on search | `playwright install chromium` wasn't run |
| `CERTIFICATE_VERIFY_FAILED` | Antivirus/corporate TLS inspection — see [Notes on TLS](#notes-on-tls) |
| Instagram/YouTube link fails | The post is private, or the platform is rate-limiting you |

## What it does

1. **Extracts** the audio with `yt-dlp` (TikTok, Instagram, YouTube, Facebook, X,
   Reddit, Vimeo, SoundCloud). Any music title/artist the platform exposes is
   kept — TikTok in particular often names the track outright.
2. **Identifies** the recording via Shazam. Platforms frequently expose no music
   metadata at all (Instagram Reels typically expose none), and a confirmed
   title is both the answer to "what *is* this song?" and the strongest possible
   Pixabay search term.
3. **Analyses** it: tempo, musical key, timbre (MFCCs), harmony (chroma),
   brightness, energy, dynamics and note density.
4. **Plans searches** from those measurements — genre and mood guesses crossed
   with the tempo feel, plus a direct title lookup when one is known.
5. **Scrapes Pixabay** for candidates, then **downloads every candidate** and
   analyses it the same way. Ranking is done on the actual audio, not on tags.
6. **Ranks** by weighted similarity, and separately checks a Shazam-style
   acoustic fingerprint to detect the *same recording*. The top 10 are shown by
   default, with the full ranked list one click away.

The wide candidate net is deliberate. Pixabay's metadata says nothing about
whether a track *sounds* like your clip, so the only way to rank honestly is to
fetch and analyse each one — and the correct answer is often far down Pixabay's
own relevance order. In testing, a 20-candidate budget missed an exact match
sitting at rank 31; at 80 candidates it was found with 21,047 aligned
fingerprint points. `MAX_CANDIDATES` in `backend/app.py` is the dial: lower it
for speed and bandwidth, raise it for recall.

## Naming vs. matching

Two different questions, answered by two different systems:

- *"What is this song?"* — Shazam lookup, covering commercial releases.
- *"What royalty-free music sounds like it?"* — the Pixabay pipeline below.

A track can be identified and still have no Pixabay match: naming a song does
not make it royalty-free. Identification is optional; if `shazamio` is missing
or the lookup fails, everything else still runs.

## Exact match vs. similar

These are two different mechanisms, which is why the app can tell them apart.

**Similarity** is a weighted blend: timbre 34%, tempo 20%, harmony 18%,
energy 14%, rhythm 9%, tag overlap 5%. Every dimension is shown per result so
you can see *why* something ranked where it did, and re-sort by any single one.

**Exact match** is a constellation fingerprint: spectral peaks are paired and
hashed, and two files are the same recording if many hashes align at one
consistent time offset. This survives re-encoding, volume changes and trimming,
so a 15-second clip lifted from the middle of a track still matches it.

The separation is stark in practice — a true match scores tens of thousands of
aligned points where an unrelated track scores single digits — so
"exact" is reported with confidence rather than as a high similarity score.

## Measured accuracy

Known-item retrieval over 260 Pixabay tracks: a 15-second excerpt is cut from
each track (quieter, mono, re-encoded) and matched against the whole pool.

| Metric | Result |
|---|---|
| Correct track ranked #1 | 237 / 260 (91%) |
| Correct track in top 3 | 254 / 260 (98%) |

Most remaining misses are tracks Pixabay hosts in several lengths (a 35s, 44s
and 61s cut of one recording), where the "wrong" answer is the same music.

A full run takes roughly 30–60 seconds: ~0.65s of analysis per candidate across
8 worker processes, plus scraping and downloads.

## Limits worth knowing

- **Pixabay has no public music API.** Their documented API covers images and
  video only, and music pages sit behind Cloudflare, so the app drives a
  headless browser and reads the track data out of the page. If Pixabay changes
  its front end, `backend/pixabay.py` is the file that breaks.
- **An exact match is only found if the search surfaces it.** The fingerprint
  can only confirm tracks that were scraped. If the clip's music isn't on
  Pixabay at all — most commercial music isn't — you correctly get similar
  alternatives instead of a match.
- **Shazam access is unofficial.** `shazamio` is a reverse-engineered client, not
  a supported API; treat it as best-effort and expect it to break eventually.
- **Tempo octave ambiguity is real.** Beat trackers report 152 BPM for a 76 BPM
  lofi track fairly often. Searches deliberately cover both readings, because a
  single wrong reading otherwise steers the entire search into the wrong genre.
- **Private or login-walled posts can't be downloaded.**
- **Licences are yours to check.** Pixabay's terms and any attribution
  requirement are shown per track; verify before publishing.

## Layout

```
backend/
  app.py        FastAPI server, pipeline orchestration, SSE progress
  extract.py    yt-dlp audio extraction + platform metadata
  identify.py   Shazam lookup (optional; names commercial releases)
  analyze.py    feature extraction + constellation fingerprinting
  match.py      query planning, similarity scoring, match classification
  pixabay.py    headless-browser scraper + CDN preview downloads
  featstore.py  disk-cached analysis (safe to import in worker processes)
  certs.py      TLS trust, incl. antivirus/proxy interception CAs
web/            single-page UI (no build step, no dependencies)
cache/          downloaded previews and cached features
```

## Notes on TLS

Some antivirus products (Norton, Kaspersky) and corporate proxies inspect HTTPS
by re-signing it with their own certificate authority. Python doesn't read the
OS certificate store, so this shows up as `CERTIFICATE_VERIFY_FAILED` on what
looks like a working internet connection.

`backend/certs.py` detects such a CA automatically and folds it into the normal
trust bundle, keeping verification enabled. **If you aren't behind TLS
inspection, it does nothing and you can ignore this section.**

`pip` needs the certificate passed explicitly, since it runs before any of this
code does:

```bash
pip install -r requirements.txt --cert "C:/ProgramData/Norton/Antivirus/wscert.pem"
```

## Licence

MIT — see [LICENSE](LICENSE). Note this covers *this code only*. The music it
finds is licensed by Pixabay under their own terms, and the app relies on
Pixabay and Shazam services that it does not control.
