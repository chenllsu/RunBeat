# RunBeat

**把任何一首歌，变成你的跑步步频。**

每一步踩在一拍上 · 音高不变 · 完全本机处理

[![Release](https://img.shields.io/github/v/release/chenllsu/RunBeat)](https://github.com/chenllsu/RunBeat/releases/latest)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)
[**English**](README_EN.md) | **中文**

---

## 30 秒上手

1. [下载](https://github.com/chenllsu/RunBeat/releases/latest) `RunBeat-win64.zip` 并**解压**（别在压缩包里直接双击）
2. 双击 `RunBeat.exe`（需要 64 位 Windows 10+）
3. 拖一首歌进去 → 调步频 → 生成下载

> 第一首歌会等 1~2 分钟（后台编译音频引擎，一次性），之后每首约 1 秒。
> 别放在 `C:\Program Files`，程序要在旁边建 `data/` 存文件。

## 它做什么

| | |
|---|---|
| 🎯 **变速跟步** | 检测原曲 BPM（自动纠正半速/倍速误判），按你的步频（120~220 spm）整曲伸缩，踩点不跑偏 |
| ✂️ **裁剪对拍** | 波形上拖选区，松手自动吸附鼓点；可叠节拍声 |
| 👂 **试听** | 浏览器实时变速零延迟，改步频立刻听 |
| 📦 **批量** | 一次 20 首，逐首独立调参，一键打包 zip |
| ⌕ **搜歌下载** | 直搜 Apple Music，试听 30 秒再下，下载完直接进编辑流程 |

**关于联网**：变速全部在本机，不上传任何文件；只有「搜歌下载」会访问 Apple Music（需要有效订阅，凭证只存本机）。

## 从源码运行

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。开发用 `run-dev.bat`（自动重载）。ffmpeg 已自带，无需安装。

<details>
<summary><b>检测精度与已知局限</b>（点开）</summary>

| 项目 | 结果 |
|---|---|
| BPM 检测（合成样本 100~184.6） | 误差全部 **0**（含半速误判自动修回） |
| 真实歌曲（鼓点密集） | 真值 99.4 → 读出 **99.0** |
| 变速时长 | 误差 < 0.1% |
| 导出成品 click 落点 | 中位误差 **0 ms** |

- **量化误差**（良性 ±1.5%）：自相关只能落在网格点，128 BPM 的歌会读成 126 或 129，对跑步影响 ±3 spm，可接受
- **半速/倍速误判**（恶性）：检测器自动裁决，流行/电子/摇滚基本不出错；民谣、爵士等弱鼓点曲风仍可能失手，用「听节拍对齐」核对
- 古典/现场版/纯人声本来就没有唯一 BPM
- **别用 1:1 / 1:2 开关修检测错误**——检测错了直接改数字

完整精度表见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

</details>

<details>
<summary><b>目录结构与 data 目录</b>（点开）</summary>

```
runbeats/
├── app/           FastAPI 后端（audio 变速 / apple_music 在线音源 / store 缓存清理）
├── static/        前端（原生 JS，无框架）
├── scripts/       selftest.py 核心自检 · check_frontend.py 前端校验 · e2e_test.py 端到端测试
├── docs/          实现笔记
└── data/          运行时生成：上传件 / 成品 / 凭证 / 日志
```

`data/` 全是运行时产物，删掉不影响程序运行；每 30 分钟自动清理超过 6 小时的临时文件。
cookies 与下载内容只留本机。完整目录说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

</details>

<details>
<summary><b>HTTP 接口一览</b>（点开）</summary>

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload` · `/api/upload-many` | 上传（单/批量），返回 BPM 与波形 |
| GET | `/api/waveform/{id}` · `/api/beats/{id}?bpm=` | 波形峰值 / 拍点时间 |
| POST | `/api/export` · `/api/package` | 渲染成品 / 打包 zip |
| POST | `/api/apple/search` · `/api/apple/import` | 搜索 / 提交下载任务（轮询 `import/{task_id}`） |
| GET/POST/DELETE | `/api/apple/cookies` · `/api/apple/wvd` · `/api/apple/settings` | 凭证 / CDM / 地区设置 |

</details>

## 许可

**GPL-3.0**（[LICENSE](LICENSE)）——因依赖 pywidevine（GPL-3.0），整体分发必须同许可。

在线音源基于 [gamdl](https://github.com/glomatico/gamdl) / [amdl](https://github.com/wenfeng110402/AppleMusic-Downloader) / [yt-dlp](https://github.com/yt-dlp/yt-dlp)，BPM 检测用 [librosa](https://github.com/librosa/librosa)，变速用 [ffmpeg](https://ffmpeg.org/)，感谢这些开源项目。

> **免责**：本项目不存储任何受版权保护内容；Apple Music 凭证由用户自行提供。仅供个人学习研究，变速结果请勿分发或商用。DRM 规避在部分司法辖区受法律限制，使用前请了解当地规定。
