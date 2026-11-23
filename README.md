<p align="center"><a href="./README_EN.md">English</a> | <a href="./README.md">中文</a></p>

<h1 align="center">RunBeat</h1>

<p align="center"><strong>跑步音乐生成器</strong> — <em>把任何一首歌，变成你的步频。</em></p>

<p align="center"><sub>每一步踩在一拍上 · <em>音高不变</em> · 整曲时间伸缩 · 完全本机处理</sub></p>

<p align="center">
<a href="https://github.com/chenllsu/RunBeat/releases/latest"><img src="https://img.shields.io/github/v/release/chenllsu/RunBeat?style=flat-square" alt="Release"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/License-GPL--3.0-blue.svg?style=flat-square" alt="License: GPL-3.0"></a>
<a href="https://github.com/chenllsu/RunBeat"><img src="https://img.shields.io/badge/Platform-Windows%2010%2B%20x64-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Platform"></a>
</p>

<p align="center">
<a href="https://github.com/chenllsu/RunBeat/releases/latest"><b>⬇ 下载 RunBeat-win64.zip</b></a>
</p>

---

> **首开提示：** 第一首歌要等 1~2 分钟（后台编译音频引擎，一次性开销）——之后每首约 1 秒。别把程序放在 `C:\Program Files`，它要在旁边建 `data/` 存文件；放桌面、文档、D 盘都行。

## 为什么

跑步听歌最扫兴的事莫过于节奏和脚步打架：歌是好歌，可它的速度不由你。步频 170~180 spm 是多数跑者的舒适区，但市面上的歌很少正好落在这个速度上。

RunBeat 检测原曲速度，按你的目标步频把整曲时间轴拉长或压短 —— 做法是整曲时间伸缩，不重排拍点、不掺合成器、不动音高。脚步踩在鼓点上，你听到的还是那首歌。

## 核心能力

| | |
|---|---|
| 🎯 **变速跟步** | 自动检测 BPM 并**纠正半速/倍速误判**；候选值一键切换，也可手填。步频 120~220 spm，1:1 / 1:2 两种换算 |
| ✂️ **裁剪对拍** | 波形上拖选区，松手自动吸附最近鼓点（最多挪半拍）；可叠节拍声 |
| 👂 **即时试听** | 浏览器实时变速零延迟——改步频立刻听；生成用服务端精确渲染，音质更好 |
| 📦 **批量处理** | 一次 20 首，每首独立调参、独立试听，一键打包 zip |
| ⌕ **搜歌下载** | 直搜 Apple Music 曲库，**试听 30 秒**再下；下载完自动进编辑流程，与本地文件完全等价 |

## 快速上手

```bash
# 1. 下载并解压（别在压缩包里直接双击）
# 2. 双击 RunBeat.exe
# 3. 拖一首歌进去 → 调步频 → 生成下载
```

**关于联网**：变速全部在本机，不上传任何文件；只有「搜歌下载」访问 Apple Music。需要有效订阅，首次使用在「音源设置」粘贴浏览器导出的 cookies（有三步引导）；凭证只存本机。

## 从源码运行

```bash
git clone https://github.com/chenllsu/RunBeat.git
cd RunBeat
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。开发用 `run-dev.bat`（自动重载）。ffmpeg 经 imageio-ffmpeg 自带，无需安装。

<details>
<summary><b>📊 检测精度与已知局限</b></summary>

| 项目 | 结果 |
|---|---|
| BPM 检测（合成样本 100~184.6） | 误差全部 **0**（含半速误判自动修回） |
| 真实歌曲（鼓点密集） | 真值 99.4 → 读出 **99.0** |
| 变速时长 | 误差 < 0.1% |
| 导出成品 click 落点 | 中位误差 **0 ms**，不累积漂移 |

- **量化误差**（良性，±1.5%）：自相关只能落在网格点——128 BPM 的歌会读成 126 或 129，对跑步影响 ±3 spm，可接受
- **半速/倍速误判**（恶性，误差 100%）：四层裁决自动纠正；流行/电子/摇滚基本不出错，民谣/爵士等弱鼓点曲风仍可能失手——用「听节拍对齐」核对
- 古典/现场版/纯人声本来就没有唯一 BPM
- **别用 1:1 / 1:2 开关修检测错误**——半速误判时它恰好抵消，倍速误判时错更远；检测错了直接改数字

完整精度表：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
</details>

<details>
<summary><b>📁 项目结构</b></summary>

```
runbeats/
├── app/           FastAPI 后端
│   ├── audio.py        ffmpeg 封装 · BPM 检测 · 时间伸缩 · click 生成
│   ├── apple_music.py  Apple Music 搜索 · 下载任务 · 进度
│   ├── store.py        文件登记 · 缓存清理
│   └── main.py         API 接口
├── static/        前端（原生 JS，无框架）：波形 / 拍点 / 裁剪 / 试听排程 / 搜歌下载
├── scripts/       selftest.py 核心自检 · check_frontend.py 前端校验 · e2e_test.py 端到端测试
├── docs/          实现笔记（ARCHITECTURE.md）
└── data/          运行时生成：上传件 / 成品 / 凭证 / 日志
```

</details>

<details>
<summary><b>🗄️ data 目录说明</b></summary>

**全部是运行时产物，删掉不影响程序运行**；每 30 分钟自动清理超过 6 小时的临时文件（顺带清扫下载中断留下的孤儿目录）。

| 子目录 | 装什么 |
|---|---|
| `uploads/` | 原始音频（在线下载的歌也在这里） |
| `analysis/` | 单声道 22 kHz wav，仅供 BPM 检测 |
| `previews/` · `outputs/` | 试听片段缓存 · 完整成品 |
| `tmp/` · `imports/` | 混音中间文件 · 下载暂存区 |
| `settings.json` · `cookies.txt` · `widevine.wvd` | 音源偏好 · **Apple Music 凭证（仅本机）** · 自备 CDM（可选） |
| `logs/` | 运行日志，反馈问题时发这个 |

</details>

<details>
<summary><b>🔌 HTTP 接口一览</b></summary>

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload` · `/api/upload-many` | 上传（单/批量），返回 BPM 与波形 |
| GET | `/api/waveform/{id}` · `/api/beats/{id}?bpm=` | 波形峰值 / 拍点时间 |
| POST | `/api/metronome` | 听节拍对齐：原曲 + click 混音 |
| POST | `/api/export` · `/api/package` | 渲染成品 / 打包 zip |
| POST | `/api/apple/search` | Apple Music 搜索（无需 cookies） |
| POST | `/api/apple/import` | 提交下载任务，轮询 `import/{task_id}` 取进度 |
| GET/POST/DELETE | `/api/apple/cookies` · `/api/apple/wvd` · `/api/apple/settings` | 凭证 / CDM / 地区设置 |
| GET | `/api/health` | 自检 |

</details>

## 状态

**v1.1.0** —— 变速核心与在线音源均已可用。算法自检（selftest.py）全绿，端到端 HTTP 测试覆盖上传到下载全链路；在线音源依赖真实凭证，未进 CI，需手工验证。

## 许可证

**GPL-3.0**（[LICENSE](LICENSE)）—— 因依赖 pywidevine（GPL-3.0），整体分发必须同许可。

在线音源基于 [gamdl](https://github.com/glomatico/gamdl) / [amdl](https://github.com/wenfeng110402/AppleMusic-Downloader) / [yt-dlp](https://github.com/yt-dlp/yt-dlp)，BPM 检测用 [librosa](https://github.com/librosa/librosa)，变速用 [ffmpeg](https://ffmpeg.org/)——感谢这些开源项目。

> **免责**：本项目不存储任何受版权保护内容；Apple Music 凭证由用户自行提供。仅供个人学习研究，变速结果请勿分发或商用。DRM 规避在部分司法辖区受法律限制，使用前请了解当地规定。
