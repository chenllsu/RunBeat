"""全局配置与路径常量。"""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"

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
MIN_DURATION_SEC = 5.0                  # 太短的片段没法可靠检测节拍
ALLOWED_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}

# ---------------------------------------------------------------- 裁剪区间
# 选区最短长度。再短的话，变速之后根本听不出节奏对不对，还不如不裁。
MIN_CLIP_SEC = 5.0

# ---------------------------------------------------------------- 变速参数
DEFAULT_MIN_RATIO = 0.85                # 默认最多放慢到 0.85x
DEFAULT_MAX_RATIO = 1.15                # 默认最多加速到 1.15x
HARD_MIN_RATIO = 0.25                   # 硬性下限，防止用户设出无意义的值
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
# 帧移。librosa 默认 512，对应的帧率只有 43 Hz，BPM 分辨率不足以分辨
# 「120」和「117.45」这种差别（实测 120 BPM 的音频会被读成 117.45）。
# 降到 256 后帧率翻倍，同一条音频可准确读到 120.19，误差 0.16%。
ANALYSIS_HOP = 256
RETENTION_HOURS = 6.0                   # 临时文件保留时长
MP3_BITRATE = "192k"

# ---------------------------------------------------------------- 节拍可视化
# 波形峰值点数。整首压成固定数量的峰值一次给前端，之后滚动只是 canvas 平移，
# 播放过程中不会向后端发任何请求。
WAVEFORM_BUCKETS = 4000
WAVE_WINDOW_SEC = 20.0                  # 波形窗口宽度（原曲时间轴秒数）
BEAT_CACHE_MAX = 6                      # 每个文件最多缓存几套拍点（按 BPM）

# ---------------------------------------------------------------- 听节拍对齐
METRONOME_LENGTH_SEC = 15.0             # 混音片段长度
METRONOME_GAIN = 0.50                   # click 相对原曲的音量（默认值，也是音量滑块 50% 对应值）
METRONOME_BED_GAIN = 0.82               # 原曲压低一点，给 click 留头空间，避免相加削波
METRONOME_LIMIT = 0.97                  # 限幅兜底
# 「听节拍对齐」只是校对工具，压一点原曲无所谓；但导出成品时不压 ——
# 跑步听的主体还是音乐，不该因为多了一层 click 就整体变小声。
METRONOME_EXPORT_BED_GAIN = 1.0
METRONOME_FREQ = 1400.0                 # click 频率（Hz），短促但能穿透音乐
METRONOME_SR = 44100                    # 混音统一用这个采样率，避免两路采样率不一致
