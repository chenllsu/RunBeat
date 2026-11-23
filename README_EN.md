<p align="center"><a href="./README_EN.md">English</a> | <a href="./README.md">中文</a></p>

<h1 align="center">RunBeat</h1>

<p align="center"><strong>Running-music generator</strong> — <em>turn any song into your cadence.</em></p>

<p align="center"><sub>Every step lands on a beat · <em>pitch unchanged</em> · whole-track time-stretch · runs entirely on your machine</sub></p>

<p align="center">
<a href="https://github.com/chenllsu/RunBeat/releases/latest"><img src="https://img.shields.io/github/v/release/chenllsu/RunBeat?style=flat-square" alt="Release"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/License-GPL--3.0-blue.svg?style=flat-square" alt="License: GPL-3.0"></a>
<a href="https://github.com/chenllsu/RunBeat"><img src="https://img.shields.io/badge/Platform-Windows%2010%2B%20x64-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Platform"></a>
</p>

<p align="center">
<a href="https://github.com/chenllsu/RunBeat/releases/latest"><b>⬇ Download RunBeat-win64.zip</b></a>
</p>

---

> **First-launch note:** the first song takes 1–2 minutes (one-time audio engine compilation) — every song after that takes ~1 second. Don't put the app under `C:\Program Files`; it creates `data/` next to itself for storage. Desktop, Documents, D:\ — all fine.

## Why

The most disappointing thing about running with music is when the beat fights your footsteps: the song is great, but its tempo isn't yours. Cadence of 170–180 spm (steps per minute) is the comfort zone for most runners, yet few songs land exactly there.

RunBeat detects the song's tempo and stretches or compresses the whole track to your target cadence — pure whole-track time-stretch: no beat re-arrangement, no synthesizers, no pitch change. Your steps land on the drums, and it's still the same song.

## Core features

| | |
|---|---|
| 🎯 **Stretch to your steps** | Auto-detects BPM and **corrects half/double-speed mistakes**; candidate values one click away, or type your own. Cadence 120–220 spm with 1:1 / 1:2 mappings |
| ✂️ **Beat-snapped clipping** | Drag a range on the waveform; handles snap to the nearest drum hit on release (max half a beat); optional metronome click |
| 👂 **Instant audition** | Real-time in-browser stretching with zero latency — hear cadence changes immediately; final renders use precise server-side processing for better quality |
| 📦 **Batch mode** | Up to 20 songs, per-song settings and audition, one-click zip export |
| ⌕ **Search & download** | Search the Apple Music catalog directly, **audition 30 seconds** before downloading; imported songs flow straight into the editor, fully equivalent to local files |

**About the network**: all stretching runs locally, nothing is uploaded; only "Search & Download" contacts Apple Music. An active subscription is required — paste browser-exported cookies in "Source settings" on first use (three-step guide); credentials stay on this machine.

## Run from source

```bash
git clone https://github.com/chenllsu/RunBeat.git
cd RunBeat
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. For development use `run-dev.bat` (auto-reload). ffmpeg is bundled via imageio-ffmpeg — no installation needed.

<details>
<summary><b>📊 Detection accuracy & known limits</b></summary>

| Item | Result |
|---|---|
| BPM detection (synthetic 100–184.6) | Error **0** across the board (incl. automatic half-speed correction) |
| Real song (dense drums) | Ground truth 99.4 → reads **99.0** |
| Stretch duration | Error < 0.1% |
| Click placement in exported file | Median error **0 ms**, no drift |

- **Quantization error** (benign, ±1.5%): autocorrelation lands on grid points — a 128 BPM song reads as 126 or 129; ±3 spm for running, acceptable
- **Half/double-speed misjudgment** (malignant, 100% error): four-layer adjudication corrects automatically; pop/electronic/rock rarely fail, folk and jazz with weak drums can still slip — verify with "Beat alignment"
- Classical / live / a cappella music has no single BPM to begin with
- **Don't use the 1:1 / 1:2 switch to fix detection errors** — it cancels out half-speed mistakes but doubles the error on double-speed ones; change the number instead

Full accuracy table: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
</details>

<details>
<summary><b>📁 Project structure</b></summary>

```
runbeats/
├── app/           FastAPI backend
│   ├── audio.py        ffmpeg wrapper · BPM detection · time-stretch · click generation
│   ├── apple_music.py  Apple Music search · download tasks · progress
│   ├── store.py        File registry · cache cleanup
│   └── main.py         API endpoints
├── static/        Frontend (vanilla JS, no framework): waveform / beats / clipping / audition scheduling / search & download
├── scripts/       selftest.py core checks · check_frontend.py frontend validation · e2e_test.py end-to-end tests
├── docs/          Implementation notes (ARCHITECTURE.md)
└── data/          Runtime-generated: uploads / outputs / credentials / logs
```

</details>

<details>
<summary><b>🗄️ The data directory</b></summary>

**Everything under `data/` is runtime-generated — deleting it doesn't affect the program**; temporary files older than 6 hours are cleaned every 30 minutes (orphaned download directories from interrupted runs are swept too).

| Subdirectory | Contents |
|---|---|
| `uploads/` | Original audio (online downloads land here too) |
| `analysis/` | Mono 22 kHz wav, only for BPM detection |
| `previews/` · `outputs/` | Audition clip cache · full rendered results |
| `tmp/` · `imports/` | Mixing intermediates · download staging area |
| `settings.json` · `cookies.txt` · `widevine.wvd` | Source preferences · **Apple Music credentials (local only)** · user-supplied CDM (optional) |
| `logs/` | Runtime log — send this when reporting issues |

</details>

<details>
<summary><b>🔌 HTTP API overview</b></summary>

| Method | Path | Description |
|---|---|---|
| POST | `/api/upload` · `/api/upload-many` | Upload (single/batch), returns BPM & waveform |
| GET | `/api/waveform/{id}` · `/api/beats/{id}?bpm=` | Waveform peaks / beat times |
| POST | `/api/metronome` | Beat alignment: original clip + click mixed |
| POST | `/api/export` · `/api/package` | Render result / bundle zip |
| POST | `/api/apple/search` | Apple Music search (no cookies needed) |
| POST | `/api/apple/import` | Submit download task, poll `import/{task_id}` for progress |
| GET/POST/DELETE | `/api/apple/cookies` · `/api/apple/wvd` · `/api/apple/settings` | Credentials / CDM / region settings |
| GET | `/api/health` | Self-check |

</details>

## Status

**v1.1.0** — the stretch core and online source are both usable. Algorithm self-checks (selftest.py) all pass; end-to-end HTTP tests cover the upload-to-download pipeline. The online source depends on real credentials and is not in CI — verify manually.

## License

**GPL-3.0** ([LICENSE](LICENSE)) — required by the pywidevine (GPL-3.0) dependency; the whole work must be distributed under the same license.

The online source builds on [gamdl](https://github.com/glomatico/gamdl) / [amdl](https://github.com/wenfeng110402/AppleMusic-Downloader) / [yt-dlp](https://github.com/yt-dlp/yt-dlp); BPM detection uses [librosa](https://github.com/librosa/librosa); stretching uses [ffmpeg](https://ffmpeg.org/) — thanks to all these open-source projects.

> **Disclaimer**: this project stores no copyrighted content; Apple Music credentials are provided by the user. For personal study and research only — don't distribute or commercialize the results. DRM circumvention is legally restricted in some jurisdictions; know your local rules before use.
