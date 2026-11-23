# RunBeat

**Turn any song into your running cadence.**

Every step lands on a beat · Pitch unchanged · Runs entirely on your machine

[![Release](https://img.shields.io/github/v/release/chenllsu/RunBeat)](https://github.com/chenllsu/RunBeat/releases/latest)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)
**English** | [**中文**](README.md)

---

## Up and running in 30 seconds

1. [Download](https://github.com/chenllsu/RunBeat/releases/latest) `RunBeat-win64.zip` and **extract it** (don't run from inside the zip)
2. Double-click `RunBeat.exe` (64-bit Windows 10+ required)
3. Drag a song in → set your cadence → generate & download

> The first song takes 1–2 minutes (one-time audio engine compilation); every song after that takes ~1 second.
> Don't put the folder under `C:\Program Files` — the app creates `data/` next to itself for storage.

## What it does

| | |
|---|---|
| 🎯 **Stretch to your steps** | Detects the song's BPM (auto-corrects half/double-speed mistakes) and time-stretches the whole track to your cadence (120–220 spm) without changing pitch |
| ✂️ **Beat-snapped clipping** | Drag a range on the waveform; handles snap to the nearest drum hit on release; optional metronome click |
| 👂 **Instant audition** | Real-time in-browser stretching with zero latency — hear cadence changes immediately |
| 📦 **Batch mode** | Up to 20 songs, per-song settings, one-click zip export |
| ⌕ **Search & download** | Search Apple Music directly, audition 30 seconds before downloading, then the song flows straight into the editor |

**About the network**: all stretching runs locally, nothing is uploaded; only "Search & Download" contacts Apple Music (requires an active subscription; credentials stay on this machine).

## Run from source

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. For development use `run-dev.bat` (auto-reload). ffmpeg is bundled — no installation needed.

<details>
<summary><b>Detection accuracy & known limits</b> (click to expand)</summary>

| Item | Result |
|---|---|
| BPM detection (synthetic 100–184.6) | Error **0** across the board (incl. automatic half-speed correction) |
| Real song (dense drums) | Ground truth 99.4 → reads **99.0** |
| Stretch duration | Error < 0.1% |
| Click placement in exported file | Median error **0 ms** |

- **Quantization error** (benign, ±1.5%): autocorrelation lands on grid points — a 128 BPM song reads as 126 or 129; ±3 spm for running, acceptable
- **Half/double-speed misjudgment** (malignant): auto-adjudicated; pop/electronic/rock rarely fail; folk and jazz with weak drums can still slip — verify with "Beat alignment"
- Classical / live / a cappella music has no single BPM to begin with
- **Don't use the 1:1 / 1:2 switch to fix detection errors** — change the number instead

Full accuracy table: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

</details>

<details>
<summary><b>Project layout & the data directory</b> (click to expand)</summary>

```
runbeats/
├── app/           FastAPI backend (audio stretch / apple_music online source / store cache & cleanup)
├── static/        Frontend (vanilla JS, no framework)
├── scripts/       selftest.py core checks · check_frontend.py frontend validation · e2e_test.py end-to-end tests
├── docs/          Implementation notes
└── data/          Runtime-generated: uploads / outputs / credentials / logs
```

Everything under `data/` is runtime-generated — deleting it doesn't affect the program; temporary files older than 6 hours are cleaned every 30 minutes.
Cookies and downloads stay on this machine. Full layout details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

</details>

<details>
<summary><b>HTTP API overview</b> (click to expand)</summary>

| Method | Path | Description |
|---|---|---|
| POST | `/api/upload` · `/api/upload-many` | Upload (single/batch), returns BPM & waveform |
| GET | `/api/waveform/{id}` · `/api/beats/{id}?bpm=` | Waveform peaks / beat times |
| POST | `/api/export` · `/api/package` | Render result / bundle zip |
| POST | `/api/apple/search` · `/api/apple/import` | Search / submit download task (poll `import/{task_id}`) |
| GET/POST/DELETE | `/api/apple/cookies` · `/api/apple/wvd` · `/api/apple/settings` | Credentials / CDM / region settings |

</details>

## License

**GPL-3.0** ([LICENSE](LICENSE)) — required by the pywidevine (GPL-3.0) dependency; the whole work must be distributed under the same license.

The online source builds on [gamdl](https://github.com/glomatico/gamdl) / [amdl](https://github.com/wenfeng110402/AppleMusic-Downloader) / [yt-dlp](https://github.com/yt-dlp/yt-dlp); BPM detection uses [librosa](https://github.com/librosa/librosa); stretching uses [ffmpeg](https://ffmpeg.org/). Thanks to all these open-source projects.

> **Disclaimer**: this project stores no copyrighted content; Apple Music credentials are provided by the user. For personal study and research only — don't distribute or commercialize the results. DRM circumvention is legally restricted in some jurisdictions; know your local rules before use.
