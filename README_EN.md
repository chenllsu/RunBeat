# RunBeat

**A running-music generator for runners**: turn any song into **your cadence** — every footstep lands on a beat, pitch unchanged.

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)
**English** | [**中文**](README.md)

---

## Download & Run (Windows)

**Download**: <https://github.com/chenllsu/RunBeat/releases/latest> → grab `RunBeat-win64.zip` (~130 MB)

1. **Extract first** — you must extract it into a folder before running. **Do not double-click inside the zip**, the app won't start that way
2. Double-click **`RunBeat.exe`** in the extracted folder — the app window opens (closing the window exits the app)
3. Drag a song into the window → set your target cadence → generate and download

A few things worth knowing up front, to save you some "I thought it was broken" moments:

| Situation | What's going on |
|---|---|
| **First song takes 1–2 min** | The audio engine is being compiled in the background — a one-time cost. **Every song after that takes ~1 second**; the UI tells you |
| Where are files stored | In a `data/` subfolder next to the program. **The whole folder can be copied to a USB stick or another computer and keep working** |
| Does it use the network | **Time-stretching runs entirely on your machine, nothing is uploaded.** Only "Search & Download" visits Apple Music to fetch the song you asked for |
| Something wrong | Send us `data/logs/runbeat.log` and it's usually diagnosable |
| Blocked by antivirus | Unsigned self-packaged binaries occasionally trigger false positives — choose "Run anyway"; or build from source if you prefer |

> **Don't put the folder under `C:\Program Files`** or anywhere needing admin rights — the program creates `data/` next to itself for storage.
> Desktop, Documents, D:\ — all fine. Requires **64-bit Windows 10 or newer**.

---

## What it does

**Time-stretch core**

- **Upload**: mp3 / wav / m4a / aac / flac / ogg / opus / wma, single file ≤ 20 MB, duration ≤ 10 min
- **Detect the song's BPM**: automatic detection with **automatic half-speed / double-speed correction**, plus a confidence rating; a set of candidates (±2x, 2/3x, 3/2x) is one click away, and you can always type a number yourself
- **Verify beats**: oscilloscope overlays waveform, beat lines and playhead; "Listen to beat alignment" plays the **original song + click** so you can confirm detection by ear
- **Clip a range**: drag two handles on the full-song waveform; on release they **snap to the nearest beat** (max half a beat); or just type `1:20` — no selection means the whole song
- **Set target cadence**: 120–220 spm (default 175), with 1:1 (one step per beat) and 1:2 (one step per two beats) mappings
- **Limit the stretch ratio**: set your own `min~max`; values beyond are clamped and the actual resulting cadence is shown
- **Audition**: real-time in-browser stretching with zero latency; optional metronome click; audio keeps playing seamlessly when you change cadence
- **Generate & download**: full render on the server (better quality than audition), optional metronome click
- **Batch mode**: upload up to 20 songs at once, each with independent settings and audition; "apply to all" carries the current settings over; export everything as one zip

**Online music source**

Click "⌕ Search & Download" in the top bar (or the hero button on the empty state) to search the Apple Music catalog by title / artist; a three-column grid shows 12 results per page with plain pagination, and every result can be **auditioned for 30 seconds** before downloading.
Clicking "Download" runs download → BPM detection → library import automatically, with stage-by-stage progress (the progress bar reflects actual downloaded bytes).
Downloaded songs are **fully equivalent** to manually uploaded ones — the whole stretch/export pipeline works the same.

- Requires an active **Apple Music subscription**: paste browser-exported cookies in "Source settings" on first use (three-step guide)
- Names are normalized to `Title-Artist`; credentials stay on this machine only (`data/cookies.txt`), never uploaded
- You can search and browse without cookies — credentials are only checked when you click Download

---

## Run from source (developers)

> If you just want to use the app, see "Download & Run" above — no environment needed.

Requires **64-bit Python 3.10+** (librosa needs 64-bit; it won't install on 32-bit Python).

```bash
# 1. Create a virtual environment
python -m venv .venv

# 2. Install dependencies (swap the mirror if the default is slow)
.venv/Scripts/python.exe -m pip install --isolated \
  -i https://mirrors.aliyun.com/pypi/simple/ \
  -r requirements.txt
```

On Windows double-click `run.bat`, or start manually:

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open <http://127.0.0.1:8000>. For development use `run-dev.bat` (auto-reload on code changes).

**About ffmpeg**: the project ships its own ffmpeg binary via `imageio-ffmpeg` — **no manual installation needed**.
To use your own build, point the `RUNBEAT_FFMPEG` environment variable at the executable.

> If pip complains `Could not find a version that satisfies the requirement ... (from versions: none)`,
> it's usually a proxy/mirror issue: `unset http_proxy https_proxy` first, then switch `-i` to the Aliyun mirror
> or the official PyPI (the Tsinghua mirror occasionally returns empty version lists).

---

## UI

On first launch you see an **empty-state hero**: a prominent "⌕ Search & Download" primary button next to "＋ Add local songs",
putting the online-search path right in front of new users. Once you have any song, the hero steps aside for the editor.

The editor is a **two-column layout**: the left column manages files and tools, the right column walks through the workflow.

| Area | What it does |
|---|---|
| Empty-state hero (no songs yet) | Big title + "⌕ Search & Download" primary button + "＋ Add local songs" secondary button; you can also drag files anywhere on the page to upload |
| Top bar "⌕ Search & Download" | The right column switches to the search view (search box + three-column results + pager); "Source settings" holds cookies / Widevine CDM / region config. "← Back to editor" switches back any time — library and settings are preserved |
| Left: drop zone / ＋Add songs | Drag in or pick audio files; multiple selection supported |
| Left: library | One row per song: the checkbox controls batch export, clicking a row edits it on the right; "Apply to all" copies current settings to the other songs |
| Left: cache & data | See how much `data/` occupies on disk, clear by category (with confirmation) |
| Bottom floating play bar | Always visible, doesn't scroll with content: play/pause + stop + draggable seek bar |

**Right column — the single-song editor**, four cards top to bottom plus one batch card:

| # | Name | What it does |
|---|---|---|
| 1 | Original tempo | Track info, detected BPM with confidence, one-click candidates, manual override, oscilloscope (waveform + beat lines + playhead) |
| 2 | Clip | Drag handles on the full-song thumbnail to select a range, optional fade-in; "Preview clip" auditions the seams at original speed (**no cadence needed**) |
| 3 | Target cadence | Drag cadence, pick 1:1 / 1:2 mapping, set the stretch range; a status strip rates how natural the result will sound; "Audition stretch" plays it on the spot |
| 4 | Export | Render the full result and download, optional metronome click (affects the file only, not audition) |
| — | Batch export | Render every checked song in the library, bundled into one zip |

When the target cadence falls outside your stretch range, card 3 doesn't silently hand you a different result — it says exactly where it was clamped and what cadence you'll actually get.

### The auditions, and how they differ

There are several levels of audition, **each closer to the final result**, answering a different question:

| Entry | Where | What it plays | What question it answers |
|---|---|---|---|
| Beat alignment | Card 2 | Original song + click on detected beats | Is detection accurate? |
| Clip preview | Card 2 | Clip + fade-in only, **original speed, no click** | Does this section start/end cleanly? |
| Stretch audition | Card 3 | Clip + stretch, optional click | Does this cadence feel right? |
| Result audition | Bottom play bar | Clip + stretch + click | The final effect (in-browser real-time stretch) |

The two mid-level auditions **stop automatically when you change the selection** — they mean "listen to that section", and a changed selection makes them stale;
the bottom play bar instead jumps to the new position and keeps playing, uninterrupted.

> All auditions are **real-time in-browser stretches**: zero delay, and you hear cadence changes instantly — at the cost of slight timbre differences from the final render.
> **For the true result use "Generate full file"** — that's the server-side ffmpeg-accurate render, with better quality.

### Batch mode (playlist)

Select multiple files at once and the library appears on the left: click any song to tune it — **cadence, ratio, clip and click settings are all per-song**, switching songs never mixes them.
"Apply to all" deliberately excludes the detected BPM and clip range (every song has its own tempo, and you may not want to clip everything). Song parameters are auto-saved before switching.

Batch export renders each song then **bundles everything into one zip** — browsers throttle consecutive automatic downloads (in testing, 3 downloads → 1 arrives), so zip is the only reliable path.

---

## Known accuracy

| Item | Result |
|---|---|
| BPM detection (synthetic 120 BPM) | Reads **120.2**, error 0.17% |
| BPM detection (synthetic 100 / 120 / 140 / 160 / 175 / 184.6) | Error **0** across the board (175 / 184.6 were initially read at half speed, corrected by adjudication) |
| BPM detection (real song, dense drums) | Ground truth 99.4 → **99.0** (previously misread as 198.8, fixed) |
| BPM detection (alternating strong/weak hits, the easiest structure to double) | 99.4 / 120 / 150 all detected correctly, never doubled |
| Stretch duration | Error < 0.1% (24.00s at ×1.0815 → 22.19s, theoretical 22.19s) |
| Click placement in exported file | Median error **0 ms**, no drift accumulation |
| Click interval error in audition | Median **0.26 ms**, max 0.61 ms late in the song (previously 3.7 ms / 9.6 ms, worsening over time) |
| Click placement error in audition | p90 ≈ 10 ms, max 11 ms (previously 108 ms) |
| Clip audition anchor | Matches theory: changing start lands at the new start, changing end lands at "end − 5 s", without interrupting playback |
| Dropped sounds under main-thread stalls (8 × 350 ms) | **0** (previously 2) |

Two kinds of detection error, worth telling apart:

- **Quantization error** (benign, ±1.2–1.6%): autocorrelation can only compare whole frames, so BPM lands on grid points.
  At hop=256 near 120 BPM the grid is `123.05 / 120.19 / 117.45`, spaced ~2.9 BPM.
  A standard 128 BPM song will read as 129.2 or 126.1 — **never exactly 128**. For running that's ±3 spm, acceptable.
- **Double / half-speed misjudgment** (malignant, 100% error): autocorrelation peaks at T, 2T, 4T alike; picking the wrong one doubles the result.
  Sixty times larger than quantization error — **almost all reliability problems come from here**. The detector now adjudicates automatically;
  regular pop / electronic / rock rarely fails; folk and jazz with weak drums can still slip —
  verify with card 2's "Beat alignment", then switch candidates or type the number.

Reliability by genre: electronic / pop / rock trustworthy; folk / slow songs / jazz prone to half-double mistakes;
classical / live recordings / a cappella / ambient **have no single BPM to begin with** (tempo keeps changing).

> **Don't use the 1:1 / 1:2 switch to fix detection errors.** With a half-speed misjudgment, 1:2 happens to cancel out (looks fixed),
> but with a double-speed misjudgment 1:2 doubles the error. The switch is not a repair tool — if detection is wrong, change the number.

---

## Project layout

```
runbeats/
├── app/
│   ├── config.py       Configuration & limits (all tunable constants)
│   ├── audio.py        ffmpeg wrapper, BPM detection, ratio math, time-stretch, click generation
│   ├── apple_music.py  Online source: Apple Music search, cookie normalization, download tasks & progress
│   ├── store.py        File registry, preview cache, expiry cleanup
│   ├── update.py       Version check (once at startup, silent on failure)
│   ├── banner.py       Startup banner (why run.bat must stay pure ASCII — see its comments)
│   ├── _vendor_stubs/  pyplayready stub shim (see "License & credits")
│   └── main.py         FastAPI endpoints
├── static/
│   ├── index.html      Page structure (two columns: library / editor; right column includes search view)
│   ├── style.css
│   └── app.js          All frontend logic: waveform, beats, clipping, audition scheduling, export, search & download
├── scripts/
│   ├── selftest.py        Core algorithm self-check
│   ├── check_frontend.py  Frontend static validation
│   └── e2e_test.py        End-to-end HTTP tests
├── .github/workflows/
│   └── release.yml   Tag → auto-package → attach zip to Release
├── docs/             [implementation notes](docs/ARCHITECTURE.md)
├── data/             Generated at runtime, see next section
├── runbeat_desktop.py  Desktop launcher (also the PyInstaller entry)
├── RunBeat.spec        PyInstaller packaging config
├── requirements.txt
├── run.bat
└── run-dev.bat
```

---

## What the data directory is for

**Everything under `data/` is runtime-generated; deleting it does not affect the program** — the service rebuilds what it needs,
only uploaded files and generated results are lost. Cleanup is handled by `store.purge_expired()`, every 30 minutes, 6-hour retention
(it also sweeps orphaned download directories left in `imports/` by interrupted runs).

| Subdirectory | Contents | When created | Auto-cleaned? |
|---|---|---|---|
| `uploads/` | Your **original audio** (online downloads land here too) | Every upload / online import | ✅ after 6 h (registered files kept) |
| `analysis/` | **Mono 22 kHz wav** converted from the original, only for BPM detection & waveform drawing | Every upload | ✅ after 6 h |
| `previews/` | **Server-rendered audition clips** (hashed by `file ID + position + ratio + click volume`) | On clip requests | ✅ after 6 h |
| `outputs/` | **Full rendered results**, for in-page audition and download | Every export | ✅ after 6 h |
| `tmp/` | Intermediate files for mixing | When overlaying clicks | ✅ deleted right after use |
| `imports/` | Online download **staging area**: the source lands here, moved on import | On "Download" | ✅ cleared on import |
| `settings.json` | Source preferences (Apple Music region / metadata language) | Changed in "Source settings" | ❌ kept |
| `cookies.txt` | **Apple Music credentials** (Netscape format, local only, never uploaded) | On saving cookies | ❌ manual (clear button) |
| `widevine.wvd` | User-supplied Widevine CDM (optional, built-in by default) | In "Source settings" | ❌ manual |
| `logs/` | Runtime log (`runbeat.log`). Send it in when reporting issues | Desktop launches | ❌ manual |
| `numba_cache/` | numba compilation cache — decides whether "the second song onward is instant" | During first warm-up | ❌ manual |
| `webview/` | Desktop window browsing data (desktop build only) | Desktop launches | ❌ manual |

> The last three only appear in the **desktop (exe) build**; running the web mode via uvicorn doesn't create them.

Implementation details of the cleanup mechanism and one fixed historical pitfall: [implementation notes](docs/ARCHITECTURE.md#data-目录与清理机制) (Chinese).

---

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Self-check: ffmpeg path, output format, registry count and per-directory file counts |
| POST | `/api/upload` | Upload audio; returns duration, detected BPM, candidate BPMs, waveform peaks |
| POST | `/api/upload-many` | Batch upload (≤ 20), each processed independently; one failure doesn't affect the rest |
| GET | `/api/waveform/{file_id}` | Full-song waveform peaks (precomputed at upload, instant) |
| GET | `/api/beats/{file_id}?bpm=` | Beat times for a given BPM (original timeline, seconds) |
| POST | `/api/metronome` | "Beat alignment": original clip + click mixed |
| POST | `/api/preview` | Render one stretched audition clip (cached). **No longer used by the frontend**, kept for automated tests |
| POST | `/api/export` | Render the result; `start` / `length` select a clip range |
| POST | `/api/package` | Bundle several rendered results into one zip (batch export) |
| GET | `/api/download/{name}` | Download a result (audio or zip) |
| POST | `/api/apple/search` | Apple Music search (public catalog, no cookies needed; `offset` pagination) |
| POST | `/api/apple/import` | Submit a download task, returns `task_id` immediately (background thread runs it; optional `expected_bytes` for progress estimation) |
| GET | `/api/apple/import/{task_id}` | Poll download progress: stage, percent, logs |
| GET | `/api/apple/record/{task_id}` | Full import record after success |
| GET/POST/DELETE | `/api/apple/cookies` | Credential management (paste text or local file, stored locally only) |
| GET/POST/DELETE | `/api/apple/wvd` | Widevine CDM management (built-in by default, can use your own .wvd) |
| GET/POST | `/api/apple/settings` | Source settings (Apple Music region / metadata language) |
| GET | `/api/apple/status` | Credential status probe (cookies present / subscription valid, 30 s cache) |

Static files: `/media/source`, `/media/preview`, `/media/output` map to three data directories; the root maps to `static/`.

In the `/api/export` response, `clipped` says whether a clip range applied; the filename gets a `_0m20s-0m26s`-style range tag,
so different clips of the same song don't overwrite each other.

---

## Self-checks & tests

Three scripts, no external test framework:

```bash
# Core self-check: ffmpeg capabilities, BPM detection accuracy, half/double-speed adjudication,
# ratio math, post-stretch duration & BPM re-check, clip extraction, click alignment
.venv\Scripts\python.exe scripts\selftest.py

# Frontend static validation: JS-referenced ids exist, tags balanced, CSS braces paired
.venv\Scripts\python.exe scripts\check_frontend.py

# End-to-end HTTP tests (start the service first): upload → waveform → beats → preview cache → clipped export → download → bad inputs
.venv\Scripts\python.exe scripts\e2e_test.py
```

`selftest.py` synthesizes fixed-tempo percussion tracks itself — no external audio assets needed.
Section 4 builds two kinds of traps: **even-strength drum tracks whose ground truth falls in the "easily read at half speed" range** (e.g. 175 / 184.6),
and **alternating strong/weak hits** (strong at 1200 Hz, weak half-beat at 600 Hz, mimicking snare vs kick spectra) —
the structure autocorrelation most easily doubles, kept as a regression gate.
`e2e_test.py` includes a **frequency-tagged audio** test (main frequency changes every 8 seconds): it cuts three clips, exports them,
then measures each result's dominant frequency — proving "it clipped the right section", not just "it produced a correctly-sized file".

The online source pipeline (search / download / import / progress polling) is not in automated tests — it depends on the real
Apple Music API and valid subscription credentials, unsuitable for CI. Manual verification: search a song you haven't downloaded →
watch the progress finish → confirm the library shows a `Title-Artist` entry; also verify that clicking Download without cookies is clearly rejected.

---

## Notes

- All stretching runs locally; nothing is uploaded to third parties. Only "Search & Download" contacts Apple Music,
  to fetch the song you asked for; credentials and downloads stay on this machine.
- The bigger the stretch, the worse it sounds. Rule of thumb: **within ±15% mostly unnoticeable, ±25% awkward, beyond 30% generally unlistenable**.
  The status strip colors accordingly (green / yellow / red).
- Card 2's "Beat alignment" and "Preview clip" play the **original at original speed** — no stretching;
  "Stretch audition" and "Result audition" are real-time in-browser stretches, with slightly different timbre from the final render —
  for the true result use "Generate full file".
- Only process audio you have the rights to. If you operate this publicly, copyright compliance is on you.

---

## Implementation notes

How the four-layer BPM adjudication is designed, why the click track takes two different paths for audition vs export,
why audition scheduling fits a line, how motion tokens are chosen — implementation trade-offs and
"don't casually change this back" constants are documented in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** (Chinese).

---

## License & credits

This project is released under **GPL-3.0** (see [LICENSE](LICENSE)). GPL-3.0 is a hard constraint:
the online source feature depends on pywidevine (GPL-3.0), so per its terms any distribution of this work as a whole must be GPL-3.0 licensed too.

### Credits chain

The online source feature stands on these open-source projects:

| Project | License | Role in this project |
|---|---|---|
| [gamdl](https://github.com/glomatico/gamdl) | MIT | The core of Apple Music parsing / decryption / download |
| [AppleMusic-Downloader](https://github.com/wenfeng110402/AppleMusic-Downloader) | Unlicense | Download pipeline wrapper (amdl — the package this project calls directly) |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense | Audio track stream download |
| [pywidevine](https://github.com/devine-dl/pywidevine) | GPL-3.0 | Widevine decryption (the source of GPL copyleft) |
| [librosa](https://github.com/librosa/librosa) | ISC | BPM detection |
| [ffmpeg](https://ffmpeg.org/) (via imageio-ffmpeg) | LGPL/GPL | Time-stretching, format conversion |

Sincere thanks to all contributors of the projects above.

### About pyplayready (CC BY-NC-ND)

gamdl automatically pulls in pyplayready (CC BY-NC-ND 4.0: no commercial use, no derivatives) as a transitive dependency,
which conflicts with this project's licensing model ("commercial modification allowed, GPL copyleft").

Handling: **app/_vendor_stubs/pyplayready** is a same-name empty shim that masks the real package at startup
(see `app/__init__.py`). The effect:

- This project **only supports the Widevine decryption path** (all downloads take this path in practice; PlayReady is never used);
- Any code attempting the PlayReady backend fails immediately with a clear error instead of silently misbehaving;
- Functionality is unaffected — search, download, decryption, stretching all work as usual.

If you need the PlayReady backend, install the real pyplayready and remove the shim yourself —
but that makes the whole project fail CC BY-NC-ND distribution conditions. Assess that yourself.

### Disclaimer

1. This project does not host or provide any copyrighted content; the online source feature requires users
   to **supply their own** legitimate Apple Music subscription credentials (cookies).
2. For personal study and research only; do not use for anything illegal in your jurisdiction.
   DRM circumvention is legally restricted in some jurisdictions (e.g. DMCA 1201) — know your local rules before use.
3. Source audio is provided and downloaded by the user; results are for personal use only.
   Do not distribute, publicly broadcast, or commercialize them.
4. By using this project you accept full responsibility for compliance.
