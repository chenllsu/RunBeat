"""全局配置与路径常量。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_frozen() -> bool:
    """是否跑在 PyInstaller 打出来的包里。"""
    return bool(getattr(sys, "frozen", False))


def _resource_dir() -> Path:
    """只读资源（``static/``）所在目录。

    打包后 PyInstaller 会把 datas 解到 ``sys._MEIPASS``（onedir 形态下就是
    ``_internal``），开发时就是源码目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else Path(__file__).resolve().parent.parent


def _writable(path: Path) -> bool:
    """新建目录并判断能否写入。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK)


def _data_dir() -> Path:
    """运行时数据目录（上传件 / 分析缓存 / 预览 / 成品）。

    打包后**绝不能**落在 ``sys._MEIPASS`` 里：那是每次启动临时解压出来的目录，
    写进去的东西用户下次就找不到了（onefile 形态更是退出即删）。
    优先用 exe 同级的 ``data/``（绿色便携，用户一眼能看到自己的文件）；
    装在 Program Files 这类只读位置时退到 ``%LOCALAPPDATA%\\RunBeat``。
    """
    if not _is_frozen():
        return Path(__file__).resolve().parent.parent / "data"

    exe_dir = Path(sys.executable).resolve().parent
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "RunBeat" / "data"
    for candidate in (exe_dir / "data", local):
        if _writable(candidate):
            return candidate
    return exe_dir / "data"  # 都不行就先用 exe 同级，让后面的报错说话


BASE_DIR = _resource_dir()
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = _data_dir()

UPLOAD_DIR = DATA_DIR / "uploads"      # 用户上传的原始文件
ANALYSIS_DIR = DATA_DIR / "analysis"   # 转成单声道 wav，仅供 BPM 检测
PREVIEW_DIR = DATA_DIR / "previews"    # 预览片段（服务端生成，带缓存）
OUTPUT_DIR = DATA_DIR / "outputs"      # 完整成品，供下载
TMP_DIR = DATA_DIR / "tmp"             # 混音用的中间文件，用完即删

for _d in (UPLOAD_DIR, ANALYSIS_DIR, PREVIEW_DIR, OUTPUT_DIR, TMP_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 上传限制
MAX_UPLOAD_BYTES = 20 * 1024 * 1024     # 单文件 20 MB
MAX_DURATION_SEC = 10 * 60.0            # 最长 10 分钟
MIN_DURATION_SEC = 5.0                  # 太短没法可靠检测节拍
ALLOWED_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
MAX_BATCH_FILES = 20                    # 批量上限：逐首检测约几秒/首，再多等太久

# ---------------------------------------------------------------- 裁剪区间
MIN_CLIP_SEC = 5.0                      # 太短变速后听不出节奏对不对
DEFAULT_FADE_SEC = 1.0                  # 开头渐进切入的默认时长（秒）
MAX_FADE_SEC = 3.0                      # 淡入时长上限；再长整段听着像还没进来

# ---------------------------------------------------------------- 变速参数
DEFAULT_MIN_RATIO = 0.5                 # 默认最多放慢到 0.5x
DEFAULT_MAX_RATIO = 2.0                 # 默认最多加速到 2.0x
HARD_MIN_RATIO = 0.25                   # 硬性下限，防止设出无意义的值
HARD_MAX_RATIO = 4.0

# 自然度分档：倍率偏离 1 的比例
NATURAL_GREEN = 0.15                    # ±15% 内：舒适
NATURAL_YELLOW = 0.25                   # ±25% 内：勉强

# ---------------------------------------------------------------- 预览与步频
PREVIEW_LENGTH_SEC = 20.0               # 精确试听片段长度
SPM_MIN = 120                           # 目标步频可调范围
SPM_MAX = 220
SPM_DEFAULT = 170

# ---------------------------------------------------------------- 其他
ANALYSIS_SR = 22050                     # BPM 检测用的采样率
# 帧移 256 不能改回 librosa 默认 512：帧率不够会把 120 BPM 读成 117.45
ANALYSIS_HOP = 256
RETENTION_HOURS = 6.0                   # 临时文件保留时长
MP3_BITRATE = "192k"

# ---------------------------------------------------------------- 节拍检测
# 「多频带投票 + 能量终审」（借鉴 run_beats 的做法）。
# 注意：不用「数起音事件」的 F1 打分 —— 一拍常有多个起音（鼓/镲各算一个），
# 召回率被天然压低，会把慢歌误判成倍速。
BPM_LO = 60.0                           # 投票与候选的 BPM 下限（照抄竞品）
BPM_HI = 200.0                          # 放宽到 200：常传 175~190 的歌，卡 180 会漏掉真值
BAND_N_FFT = 2048                       # 多频带用的 STFT 窗长（竞品同款）
BAND_EDGES_HZ = (0, 30, 60, 100, 150, 250, 400, 600, 1000, 1600, 2500, 3500, 5000, 8000, 11025)
# 静带过滤必须在 z-score 之前：合成音/极简编曲的噪声带会被归一化放大成等权票
BAND_MIN_STD_RATIO = 0.05
# 聚类容差必须 2% 不能 1%：lag 量化误差 ±1.5%，1% 会把真值的票拆散
VOTE_TOL = 0.02
JUDGE_MARGIN = 1.03                     # 挑战者赢过 base 这么多倍才改判；宁可漏改，不要改坏
# 倍频关系（2 倍/半速）门槛更高：半拍切分律动能凑出 ~1.1 倍的假优势
JUDGE_OCTAVE_MARGIN = 1.15
# 非倍频改判门槛：终审打分结构性偏爱快网格（点数多 √2 倍），错误候选 +9~14% 就能碰线；
# 而锚点贴合度在这种误判里高出改判值 4 倍以上，正确改判两边接近 —— 1.10 空隙足够
JUDGE_Q_MARGIN = 1.10
# 终审打分 = 「网格点包络 sum/√点数」×「精确率因子」。
# 精确率 = 网格点踩中离散起音的比例，专治倍频误读与杂乱切分律动；
# 软底 0.25 防止起音检测整体失灵时分数全零。
JUDGE_PREC_TOL_RATIO = 0.20             # 「踩中了」的判定半径，取周期的比例
JUDGE_PREC_TOL_SEC = 0.05               # 判定半径绝对上限（秒）
JUDGE_PREC_FLOOR = 0.25                 # 精确率因子的软底
JUDGE_WIN = 2                           # 打分时网格点左右各看几帧取最大（对齐误差容忍）
FOLD_CENTER = 120.0                     # 折叠目标：2^k 倍里取离它最近的

# 均匀网格：拍点走严格等间隔网格。不做逐点吸附（会破坏间隔稳定性，跑步要的是稳）；
# 周期必须用 DP 拍点拟合，不能直接用 beat_track 的量化 tempo（长曲会累积漂移错开）
GRID_PHASES = 720                       # 相位搜索步数，精度 = 周期/720
GRID_FIT_SIGMA = 2.0                    # 拟合时剔除残差超过这个标准差的拍点
GRID_FIT_ROUNDS = 3                     # 迭代剔除轮数（应对漏拍/跳拍把斜率带偏）

# ---------------------------------------------------------------- 节拍可视化
WAVEFORM_BUCKETS = 4000                 # 整首压成固定峰值，前端滚动零请求
WAVE_WINDOW_SEC = 20.0                  # 波形窗口宽度（原曲时间轴秒数）
BEAT_CACHE_MAX = 6                      # 每个文件最多缓存几套拍点（按 BPM）

# ---------------------------------------------------------------- 听节拍对齐
METRONOME_LENGTH_SEC = 15.0             # 混音片段长度
METRONOME_GAIN = 0.50                   # click 相对原曲的音量（也是滑块 50% 对应值）
METRONOME_BED_GAIN = 0.82               # 对齐预览里原曲压低一点，给 click 留头空间
METRONOME_LIMIT = 0.97                  # 限幅兜底
# 导出成品不压原曲：跑步听的主体是音乐，不因多一层 click 就整体变小声
METRONOME_EXPORT_BED_GAIN = 1.0
METRONOME_FREQ = 1400.0                 # click 频率（Hz），短促但能穿透音乐
METRONOME_SR = 44100                    # 混音统一采样率，避免两路不一致
