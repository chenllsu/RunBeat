"""音频处理核心：定位 ffmpeg、探测时长、检测 BPM、时间伸缩（变速不变调）。

设计要点
--------
* 预览与正式生成走的是**同一个** :func:`render` 函数，差别只在 ``start`` /
  ``length`` 两个参数（预览=截一段，导出=整首）。
* 「变速不变调」由 ffmpeg 的 ``atempo`` 滤镜实现 —— 改速度，音高不动。
* ``atempo`` 单级只支持 0.5~2.0 倍，超出范围时自动拆成多级串联。
* mp3 编码器不一定存在，启动时探测一次，没有就退回 aac/m4a。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import config, store

# --------------------------------------------------------------------------
# ffmpeg 定位
# --------------------------------------------------------------------------

_FFMPEG: str | None = None


def _locate_ffmpeg() -> str:
    """按优先级查找 ffmpeg：环境变量 → imageio-ffmpeg 自带 → 系统 PATH。"""
    env = os.environ.get("RUNBEAT_FFMPEG") or os.environ.get("FFMPEG_BINARY")
    if env and Path(env).exists():
        return env

    try:
        import imageio_ffmpeg  # noqa: PLC0415

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:  # pragma: no cover - 依赖缺失时走下面
        pass

    found = shutil.which("ffmpeg")
    if found:
        return found

    raise RuntimeError(
        "找不到 ffmpeg。请先执行 pip install imageio-ffmpeg，"
        "或用环境变量 RUNBEAT_FFMPEG 指定 ffmpeg 可执行文件的路径。"
    )


def ffmpeg() -> str:
    """返回可用的 ffmpeg 路径（首次调用时解析并缓存）。"""
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = _locate_ffmpeg()
    return _FFMPEG


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [ffmpeg(), "-hide_banner", "-nostdin", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _run_checked(args: list[str]) -> None:
    proc = _run(args)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-1200:]
        raise RuntimeError(f"ffmpeg 执行失败（退出码 {proc.returncode}）：\n{tail}")


# --------------------------------------------------------------------------
# 媒体信息探测
# --------------------------------------------------------------------------

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_AUDIO_STREAM_RE = re.compile(
    r"Stream #\d+:\d+.*?:\s*Audio:\s*([\w\-]+)[^,]*,\s*(\d+)\s*Hz"
)


@dataclass
class MediaInfo:
    duration: float
    sample_rate: int
    codec: str


def probe(path: Path) -> MediaInfo:
    """只解析文件头，不解码 —— 比 librosa 读一遍快得多。"""
    proc = _run(["-i", str(path)])
    text = proc.stderr or ""

    match = _DURATION_RE.search(text)
    if not match:
        raise ValueError(f"读不出音频信息，可能不是有效的音频文件：{Path(path).name}")
    duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))

    sample_rate = 44100
    codec = "unknown"
    sm = _AUDIO_STREAM_RE.search(text)
    if sm:
        codec = sm.group(1).strip()
        sample_rate = int(sm.group(2))

    return MediaInfo(duration=duration, sample_rate=sample_rate, codec=codec)


# --------------------------------------------------------------------------
# 输出编码
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _mp3_available() -> bool:
    proc = _run(["-encoders"])
    return "libmp3lame" in (proc.stdout or "")


def output_settings() -> tuple[list[str], str]:
    """返回 (编码参数, 扩展名)。优先 mp3，没有 lame 就退回 aac。"""
    if _mp3_available():
        return ["-c:a", "libmp3lame", "-b:a", config.MP3_BITRATE], ".mp3"
    return ["-c:a", "aac", "-b:a", config.MP3_BITRATE], ".m4a"


# --------------------------------------------------------------------------
# BPM 检测
# --------------------------------------------------------------------------


def to_analysis_wav(src: Path, dst: Path) -> Path:
    """转成单声道低采样率 wav，专供 BPM 检测（快、省内存）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run_checked(
        [
            "-y",
            "-i", str(src),
            "-vn",
            "-ac", "1",
            "-ar", str(config.ANALYSIS_SR),
            "-c:a", "pcm_s16le",
            str(dst),
        ]
    )
    return dst


def _bpm_candidates(bpm: float) -> list[float]:
    """列出常见的半速 / 倍速误判候选，供用户一键纠正。"""
    pool = [bpm, bpm / 2.0, bpm * 2.0, bpm * 2.0 / 3.0, bpm * 3.0 / 2.0]
    result: list[float] = []
    for value in pool:
        value = round(value, 1)
        if 50.0 <= value <= 260.0 and value not in result:
            result.append(value)
    return result


def _fold_bpm(bpm: float) -> float:
    """把候选 BPM 折叠到 :data:`config.FOLD_CENTER` 附近（2^k 倍里取最近的）。

    自相关对 T、2T、4T 都会有峰，直接拿峰对应的 BPM 投票，半速 / 倍速会各拉一票。
    折叠之后它们落回同一个值，票数才能聚到一起。范围 20~400，跟竞品一致。
    """
    if bpm <= 0 or not np.isfinite(bpm):
        return 0.0
    center = config.FOLD_CENTER
    best = bpm
    best_d = abs(bpm - center)
    v = bpm
    for _ in range(10):
        v /= 2.0
        if v < 20.0:
            break
        d = abs(v - center)
        if d < best_d:
            best, best_d = v, d
    v = bpm
    for _ in range(10):
        v *= 2.0
        if v > 400.0:
            break
        d = abs(v - center)
        if d < best_d:
            best, best_d = v, d
    return best


def _cluster_vote(items: list[tuple[float, float]]) -> list[list[float]]:
    """聚类投票。``items`` 是 ``(BPM, 权重)``，返回 ``[代表BPM, 权重和]`` 按权重降序。

    权重只决定簇的代表（先进簇的是权重最高的）；票值按竞品做法聚类（相差
    ``config.VOTE_TOL`` 以内合并），但**累加的是权重而不是个数** —— 峰的强弱
    本身就是证据，纯计票会把「一个强峰 + 一个弱峰」和「两个噪声峰」当成
    同样的两票，实测 150 BPM 样本的真值峰（权重 1600+）就是这样被
    伪峰（权重 680）以票数挤掉的。平票时代表与 120 更近的排前面。
    """
    clusters: list[list[float]] = []
    for bpm, w in sorted(items, key=lambda t: -t[1]):
        if bpm <= 0:
            continue
        for c in clusters:
            ratio = max(bpm, c[0]) / min(bpm, c[0])
            if abs(ratio - 1.0) < config.VOTE_TOL:
                c[1] += max(w, 0.0)
                break
        else:
            clusters.append([bpm, max(w, 0.0)])
    clusters.sort(key=lambda c: (-c[1], abs(c[0] - config.FOLD_CENTER)))
    return clusters


def _autocorr_pearson(x: np.ndarray) -> np.ndarray:
    """归一化自相关（Pearson 修正），FFT 实现。

    竞品的写法是分母除以 ``sqrt(var_前段 × var_后段) × (N-lag)``，等价于把每个
    lag 的重合段当成两条独立序列算相关系数 —— 修掉了「短 lag 重合样本少、
    乘积天然偏大」的统计偏置。这里用累积平方和把 O(N²) 压到 O(N log N)。
    """
    n = x.size
    out = np.zeros(n, dtype=np.float64)
    if n < 16:
        return out
    x = x - x.mean()
    size = 1 << (2 * n - 1).bit_length()
    spec = np.fft.rfft(x, size)
    ac = np.fft.irfft(spec * np.conj(spec), size)[:n]
    cs = np.concatenate(([0.0], np.cumsum(x * x)))
    total = float(cs[n])
    idx = np.arange(n, dtype=np.float64)
    # sum1[i] = x[0 .. n-i-1] 的平方和；sum2[i] = x[i .. n-1] 的平方和
    sum1 = cs[n - np.arange(n)]
    sum2 = total - cs[:n]
    denom = np.sqrt(np.maximum(sum1 * sum2, 0.0))
    ok = denom > 0
    out[ok] = ac[ok] * (n - idx[ok]) / denom[ok]
    return out


def _band_energy(y: np.ndarray, sr: int, hop: int) -> np.ndarray:
    """STFT → 14 个频带的帧能量序列，形状 ``(带数, 帧数)``。

    **逐带** z-score 归一化（每条序列除以自己的标准差）：让 hi-hat 带和
    底鼓带一人一票，同时保留帧与帧之间的强弱差 —— 那是「哪边才是真拍」的
    关键证据。竞品用的是逐帧跨带 L2 归一化，实测会把强弱拍的整体响度差
    抹平（强弱击频谱相近时），自相关便分不清 T 和 T/2 哪个是拍。
    """
    import librosa  # noqa: PLC0415

    spec = np.abs(librosa.stft(y, n_fft=config.BAND_N_FFT, hop_length=hop)) ** 2
    freqs = librosa.fft_frequencies(sr=sr, n_fft=config.BAND_N_FFT)
    edges = [int(np.searchsorted(freqs, f)) for f in config.BAND_EDGES_HZ]
    edges[-1] = spec.shape[0]

    rows: list[np.ndarray] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        hi = min(hi, spec.shape[0])
        if hi - lo < 1:
            continue
        rows.append(spec[lo:hi].sum(axis=0))
    if not rows:
        return np.zeros((0, 0), dtype=np.float64)

    bands = np.asarray(rows, dtype=np.float64)
    # 静带过滤：z-score 会把纯数值噪声的 std 放大到 1（和其他带等权），
    # 必须在归一化**之前**用原始 std 掐掉（阈值见 config.BAND_MIN_STD_RATIO）。
    raw_std = bands.std(axis=1)
    keep = raw_std > max(float(raw_std.max()), 1e-12) * config.BAND_MIN_STD_RATIO
    bands = bands[keep]
    if bands.shape[0] == 0:
        return np.zeros((0, 0), dtype=np.float64)
    std = bands.std(axis=1, keepdims=True)
    return (bands - bands.mean(axis=1, keepdims=True)) / np.maximum(std, 1e-12)


def _band_bpm(series: np.ndarray, fps: float) -> list[tuple[float, float]]:
    """单频带检测：自相关 → 峰 → 权重候选，返回 ``(BPM, 权重)`` 列表。

    关键的三道保险（全部照抄竞品）：
    * lag 限制在 [BPM_LO, BPM_HI] 对应的范围内 —— 搜索空间先收窄；
    * 峰的权重乘 ``sqrt((N-lag)/N)`` —— 短 lag（高倍速）的重合样本少，天然被压低；
    * 只取最强的 top 20% 峰 —— 弱峰多数是噪声。
    """
    ac = _autocorr_pearson(series)
    n = ac.size
    if n < 16:
        return []
    lo_lag = max(2, int(np.floor(fps * 60.0 / config.BPM_HI)))
    hi_lag = min(n - 2, int(np.ceil(fps * 60.0 / config.BPM_LO)))
    if hi_lag - lo_lag < 4:
        return []
    seg = ac[lo_lag : hi_lag + 1]

    mean, std = float(seg.mean()), float(seg.std())
    peaks = [i for i in range(1, seg.size - 1) if seg[i] > seg[i - 1] and seg[i] > seg[i + 1]]
    strong = [i for i in peaks if seg[i] > mean + 0.1 * std] or peaks
    if not strong:
        return []
    strong.sort(key=lambda i: float(seg[i]), reverse=True)
    top = strong[: max(1, int(np.ceil(seg.size * 0.2)))]

    out: list[tuple[float, float]] = []
    for i in top:
        lag = lo_lag + i
        bpm = 60.0 * fps / lag
        weight = float(seg[i]) * np.sqrt((n - lag) / float(n))
        out.append((bpm, weight))
    return out


def _vote_candidates(
    y: np.ndarray, sr: int, hop: int, dp_bpm: float
) -> tuple[list[float], float, float]:
    """多频带投票，返回 ``(终审候选池, 投票top1, top2)``。

    每带内部先折叠投票出前 2 名，再跨带聚类数票。候选池 = DP 值 / 投票
    top1 / top2 及各自的 2 倍 / 半速，全部框在 [BPM_LO, BPM_HI]。

    ``dp_bpm`` 是 librosa beat_track 的结果（调用前先折叠）。它**不参与投票
    计权** —— 实测一首 99.4 BPM 的歌，低频带（底鼓+贝斯）会齐刷刷投 132.5
    （切分律动，权重近 5 万，是 99.4 的两倍），票选 top1 被带跑；但 DP 的全局
    时序连贯性直接命中 99.4。所以分工是：**DP 当终审 base（锚点），投票负责
    候选池** —— DP 犯半速错时（175 被读成 87.6），候选池里的 2 倍变体由
    终审切回，两道证据互补。
    """
    bands = _band_energy(y, sr, hop)
    fps = sr / float(hop)
    if bands.size == 0:
        return [], 0.0, 0.0

    votes: list[tuple[float, float]] = []
    for bi in range(bands.shape[0]):
        cands = _band_bpm(bands[bi], fps)
        folded = [(_fold_bpm(b), w) for b, w in cands]
        for rep, w in _cluster_vote(folded)[:2]:
            votes.append((rep, w))

    clusters = _cluster_vote(votes)
    tops = [c[0] for c in clusters[:2]]
    while len(tops) < 2:
        tops.append(0.0)

    # 候选池：DP 变体排最前（base，终审平分时优先），然后投票 top1 / top2 的变体
    pool: list[float] = []
    for base in (dp_bpm, tops[0], tops[1]):
        if base <= 0:
            continue
        for v in (base, base / 2.0, base * 2.0):
            if (
                config.BPM_LO - 1e-6 <= v <= config.BPM_HI + 1e-6
                and all(abs(v - p) > 0.15 for p in pool)
            ):
                pool.append(v)
    return pool, tops[0], tops[1]


def _onset_times(env: np.ndarray, hop: int, sr: int) -> np.ndarray:
    """把起音包络的峰挑成**离散的起音时刻**（秒）。终审的精确率用它。"""
    import librosa  # noqa: PLC0415

    if env.size == 0:
        return np.zeros(0, dtype=np.float64)

    frames = librosa.onset.onset_detect(
        onset_envelope=env,
        sr=sr,
        hop_length=hop,
        units="frames",
        backtrack=False,
    )
    return np.asarray(frames, dtype=np.float64) * (hop / float(sr))


def _match_rate(a: np.ndarray, b: np.ndarray, tol: float) -> float:
    """``a`` 里的点有多少能在 ``b`` 里找到相距 ``tol`` 以内的同伴。"""
    if a.size == 0 or b.size == 0:
        return 0.0
    pos = np.searchsorted(b, a)
    hit = np.zeros(a.size, dtype=bool)
    for shift in (-1, 0):
        j = np.clip(pos + shift, 0, b.size - 1)
        hit |= np.abs(a - b[j]) <= tol
    return float(hit.mean())


def _is_octave(a: float, b: float) -> bool:
    """两个 BPM 是否互为 2 倍 / 半速关系（±5% 容差）。"""
    if a <= 0 or b <= 0:
        return False
    ratio = max(a, b) / min(a, b)
    return 1.9 <= ratio <= 2.1 or 0.475 <= ratio <= 0.525


def _grid_quality(
    y: np.ndarray,
    onset_env: np.ndarray,
    hop: int,
    sr: int,
    bpm: float,
) -> float:
    """给定 BPM 下的**精细贴合度**（倍频复核用）。

    用该速度引导 beat_track 找 DP 拍点 → 最小二乘拟合出精确周期 →
    ``_uniform_grid`` 720 档精细相位搜贴合度。粗打分（``_energy_judge``）
    的相位只有 5ms 精度、周期未拟合，快侧网格的周期误差会随曲长累积漂移，
    把贴合度毁掉（实测一首歌 198.8 侧粗评 1.23x、精确周期下其实 2.95x）
    —— 所以复核必须用拟合周期加精细相位。
    """
    if bpm <= 0:
        return 0.0
    import librosa  # noqa: PLC0415

    try:
        _tempo, frames = librosa.beat.beat_track(
            y=y, sr=sr, hop_length=hop, start_bpm=bpm, bpm=bpm, trim=False
        )
        times = np.atleast_1d(
            librosa.frames_to_time(frames, sr=sr, hop_length=hop)
        )
        period, _phase, _resid = _fit_period(times)
        if not np.isfinite(period) or period <= 0.01:
            period = 60.0 / bpm
        duration = y.size / float(sr)
        _grid, hit, _p = _uniform_grid(onset_env, hop, sr, period, duration)
        return float(hit)
    except Exception:
        return 0.0


def _energy_judge(
    env: np.ndarray,
    hop: int,
    sr: int,
    candidates: list[float],
    base_bpm: float,
) -> tuple[float, float, str]:
    """能量终审：每个候选铺网格、搜相位，比综合分。

    打分 = ``sum/√点数 × (软底 + 精确率因子)``（参数与理由见 config 注释块）：

    * ``sum/√点数`` —— 总和与均值的折中。等强拍下真值与半速读法的**均值**相等
      （都全踩峰，分不出），而总和恰好差 √2 倍，折中分让点数多的真值胜出。
    * 精确率因子 —— 网格点踩中**离散起音**的比例。专治两类冒牌候选：
      倍频误读（一半网格点落在起音空档，实测真值 99.4 的歌精确率 0.80、
      198.8 只有 0.12）和杂乱切分律动（网格与起音对不上）。刻意不用召回率
      —— 它的分母是全部起音数，「一拍多个起音」的歌里真值召回率被天然压低，
      旧裁决（F1）就是这么把 99.4 推成 198.8 的。

    相位粗搜步长约 5ms（竞品同款），打分窗口 ±``config.JUDGE_WIN`` 帧取最大，
    容忍 ±23ms 的对齐误差。挑战者要赢过 base（DP 锚点）``config.JUDGE_MARGIN``
    倍才改判；**倍频关系**（差 2 倍 / 半速）用更高的 ``config.JUDGE_OCTAVE_MARGIN``。
    分数持平时候选顺序优先（base 变体排最前，竞品同款）—— 平分说明音频本身
    分不出，听 DP 锚点的。

    返回 ``(最终 BPM, 得分, 结论)``，``结论``：``switch`` / ``keep`` / ``keep-margin``。
    """
    n = env.size
    valid = [c for c in candidates if c > 0 and np.isfinite(c)]
    if n < 8 or not valid:
        return base_bpm, 0.0, "keep"

    frame_dt = hop / float(sr)
    dur = n * frame_dt
    onsets = _onset_times(env, hop, sr)

    def profile(bpm: float) -> float:
        period = 60.0 / bpm
        steps = max(8, min(120, int(period / 0.005)))
        tol = min(period * config.JUDGE_PREC_TOL_RATIO, config.JUDGE_PREC_TOL_SEC)
        best = 0.0
        for s in range(steps):
            phase = period * s / float(steps)
            idx = np.rint(np.arange(phase, dur, period) / frame_dt).astype(np.int64)
            idx = idx[(idx >= 0) & (idx < n)]
            if idx.size == 0:
                continue
            vals = env[idx].astype(np.float64)
            for off in range(1, config.JUDGE_WIN + 1):
                vals = np.maximum(vals, env[np.clip(idx + off, 0, n - 1)])
                vals = np.maximum(vals, env[np.clip(idx - off, 0, n - 1)])
            sc = float(vals.sum()) / np.sqrt(idx.size)
            prec = _match_rate(idx * frame_dt, onsets, tol)
            combined = sc * (
                config.JUDGE_PREC_FLOOR
                + (1.0 - config.JUDGE_PREC_FLOOR) * prec
            )
            best = max(best, combined)
        return best

    scored = {b: profile(float(b)) for b in valid}
    best_bpm, best_score = max(scored.items(), key=lambda kv: kv[1])
    base_key = next((b for b in scored if abs(b - base_bpm) <= 0.15), None)
    base_score = scored[base_key] if base_key is not None else 0.0

    if base_score <= 0 or best_score <= 0 or base_key is None:
        return base_bpm, base_score, "keep"
    if abs(best_bpm - base_bpm) <= 0.15:
        return base_bpm, base_score, "keep"

    # 倍频关系（差 2 倍 / 半速）用更高的门槛 —— 那正是「99.4 被推成 198.8」
    # 的形态，半拍弱律动会凑出 1.1 倍左右的优势。
    ratio = max(best_bpm, base_bpm) / min(best_bpm, base_bpm)
    is_octave = 1.9 <= ratio <= 2.1 or 0.475 <= ratio <= 0.525
    margin = config.JUDGE_OCTAVE_MARGIN if is_octave else config.JUDGE_MARGIN
    if best_score <= base_score * margin:
        return base_bpm, base_score, "keep-margin"
    return best_bpm, best_score, "switch"


def _fit_period(
    beats: list[float] | np.ndarray,
) -> tuple[float, float, float]:
    """用拍点序列拟合出**精确的节拍周期**，返回 ``(周期, 首拍截距, 残差标准差)``。

    为什么不能直接用 ``beat_track`` 返回的 tempo：那个值是被量化过的，只能落在
    ``60 * sr / (hop * L)`` 这张网格上。实测《漂移》真值 322.589 ms，它给出
    325.027 ms（差 0.76%）—— 拿它铺一条 4 分钟的均匀网格，到曲末已经漂了
    1.8 秒，网格后半段和鼓点完全错开（贴合度 3.77x → 1.06x，等于随机落点）。

    拍点序列自己就带着「整首歌一共走了多长时间」这个信息，最小二乘拟合能跳出
    量化网格，精度提高一个数量级。中间迭代剔除残差大的点：漏拍 / 跳拍会让它后面
    所有点的序号错位，不剔掉会把斜率整个带偏。

    ``残差标准差`` 顺便成了「这首曲子的鼓点规不规则」的度量 —— 很小说明速度恒定，
    均匀网格很合适；偏大说明曲子在飘（现场版 / 渐快渐慢），那是曲子本身的问题。
    """
    arr = np.asarray(beats, dtype=np.float64).ravel()
    if arr.size < 4:
        return 0.0, 0.0, 0.0

    idx = np.arange(arr.size, dtype=np.float64)
    slope, intercept = np.polyfit(idx, arr, 1)
    for _ in range(max(0, config.GRID_FIT_ROUNDS)):
        resid = arr - (slope * idx + intercept)
        keep = np.abs(resid) <= config.GRID_FIT_SIGMA * float(resid.std())
        if keep.all() or int(keep.sum()) < 4:
            break
        slope, intercept = np.polyfit(idx[keep], arr[keep], 1)

    resid = arr - (slope * idx + intercept)
    return float(slope), float(intercept), float(resid.std())


def _uniform_grid(
    env: np.ndarray,
    hop: int,
    sr: int,
    period: float,
    duration: float,
) -> tuple[np.ndarray, float, float]:
    """铺一条**严格等间隔**的拍点网格，返回 ``(网格点, 贴合度, 最佳相位)``。

    网格只有一个自由参数：整体相位 —— 搜 ``config.GRID_PHASES`` 档，取「网格点处
    起音强度平均最大」的那一档。**间隔永远等于 ``period``，绝不为了贴某个鼓点让步。**

    这是和「逐点吸附」最本质的区别。吸附把每个点独立挪到最近的峰上，看起来每个点
    都更准了，代价是间隔被拉扯得忽长忽短（实测 CV 2.8% → 4.9%、max 380 → 454 ms）。
    跟着跑的节拍必须稳 —— 忽快忽慢比落不准更难受，所以宁可整体相位差几毫秒，
    也要保证每一格的间隔完全一致。

    ``贴合度`` = 网格点处的平均起音强度 ÷ 全曲平均起音强度。1.0 左右说明这条网格
    和鼓点无关（等于随机落点），3 以上说明踩得相当准。
    """
    n = env.size
    if period <= 1e-3 or n < 4 or duration <= period:
        return np.asarray([], dtype=np.float64), 0.0, 0.0

    frame_dt = hop / float(sr)
    mean_all = float(env.mean()) + 1e-9
    steps = max(1, int(config.GRID_PHASES))

    best_hit = -1.0
    best_phase = 0.0
    for i in range(steps):
        phase = period * i / float(steps)
        idx = np.rint(np.arange(phase, duration, period) / frame_dt).astype(np.int64)
        idx = idx[(idx >= 0) & (idx < n)]
        if idx.size == 0:
            continue
        hit = float(env[idx].mean())
        if hit > best_hit:
            best_hit, best_phase = hit, phase

    if best_hit < 0.0:
        return np.asarray([], dtype=np.float64), 0.0, 0.0

    grid = np.arange(best_phase, duration, period)
    return grid, best_hit / mean_all, best_phase


def _peaks_from(y: np.ndarray, buckets: int) -> list[int]:
    """把整条波形压成固定数量的峰值（0~1000），供前端画示波器。

    归一化到最大峰值 = 1000，所以前端拿到的是一组与音量无关的相对形状。
    """
    if buckets <= 0 or y.size == 0:
        return []

    n = min(int(buckets), int(y.size))
    edges = np.linspace(0, y.size, n + 1).astype(np.int64)
    abs_y = np.abs(y)
    out = np.empty(n, dtype=np.float32)
    for i in range(n):
        a = int(edges[i])
        b = int(edges[i + 1])
        if b <= a:
            b = a + 1
        out[i] = float(abs_y[a:b].max())

    top = float(out.max())
    if top <= 0.0:
        return [0] * n
    norm = np.clip(out / top, 0.0, 1.0)
    return [int(round(float(v) * 1000.0)) for v in norm]


def _extend_beat_grid(
    beats: list[float], duration: float, fallback_bpm: float
) -> list[float]:
    """把拍点网格向两端补齐到整首，避免开头 / 结尾出现没有拍点线的空档。

    beat_track 只在「有起音」的位置给拍点，前奏或尾奏很轻时两头会缺一截，
    画到波形上看起来就像检测坏了。这里按拍点间隔的中位数向两端外推补齐。

    中间部分的拍点**原样保留** —— 曲子本身速度有漂移的话，该看出来还是要能看出来。
    """
    if not beats:
        return []

    arr = np.asarray(beats, dtype=np.float64)
    if arr.size >= 2:
        period = float(np.median(np.diff(arr)))
    else:
        period = 60.0 / fallback_bpm if fallback_bpm else 0.5
    if not np.isfinite(period) or period <= 1e-3:
        return [round(float(b), 3) for b in arr]

    head: list[float] = []
    t = float(arr[0]) - period
    while t > 1e-6 and len(head) < 20000:
        head.append(t)
        t -= period

    tail: list[float] = []
    t = float(arr[-1]) + period
    while t <= duration + 1e-6 and len(tail) < 20000:
        tail.append(t)
        t += period

    grid = list(reversed(head)) + [float(b) for b in arr] + tail
    return [round(v, 3) for v in grid]


def analyze(
    wav_path: Path,
    buckets: int = 0,
    forced_bpm: float | None = None,
) -> dict:
    """一次解码拿到：BPM、拍点时间、清晰度、拍点规整度，以及（可选的）波形峰值。

    ``beats`` 是**原曲时间轴**上的拍点秒数，并且已经向两端补齐到整首。
    检测前会掐掉首尾静音，所以必须把掐掉的偏移补回去 —— 否则拍点画到原始波形上
    会整体错位，看着像「轻微没对齐」，比明显错位更容易误导人。

    自动检测走「DP 锚点 → 多频带投票 → 能量终审 → 周期拟合 → 均匀网格」五步
    （详见 ``_vote_candidates`` / ``_energy_judge`` / ``_fit_period``）：
    librosa 的 DP 拍点当终审 base（锚点），14 个频带各测各的 BPM 折叠投票
    生成候选池，候选再各铺网格比「能量总和 ÷ √点数」定终审，最后用终审 BPM
    引导出的 DP 拍点拟合出精确周期、铺一条**严格等间隔**的网格。
    ``bpm_raw`` 是 DP 原始值，``adjudicated`` 记录终审是否改判。

    ``forced_bpm`` 用来按用户指定的 BPM 重新生成拍点（候选值切换 / 手动修正）。
    给了它就跳过投票与终审 —— 用户可能正是在纠正检测，不该被算法改回去；网格照铺。
    """
    import librosa  # 首次导入较慢，放在函数内延迟加载

    y_raw, sr = librosa.load(str(wav_path), sr=config.ANALYSIS_SR, mono=True)
    if y_raw.size < sr:
        raise ValueError("音频太短，不足 1 秒，无法检测节拍")
    duration = y_raw.size / float(sr)

    # 掐掉首尾静音，避免长前奏 / 淡出尾奏把节拍估计带偏
    trimmed, index = librosa.effects.trim(y_raw, top_db=40.0)
    offset = 0.0
    if trimmed.size >= sr:
        offset = float(index[0]) / float(sr)
        y = trimmed
    else:
        y = y_raw

    # trim=False 是必须的：默认的 trim=True 会拿「平滑拍点包络的一半 RMS」当阈值，
    # 从头逐帧扫到第一个超过阈值的帧，把这段里的拍点全部抹掉。节奏偏轻的前奏
    # 因此会整段丢拍点。音频级的静音修剪上面已经做过了，这里不需要它再裁一次。
    hop = config.ANALYSIS_HOP
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    if onset_env.size:
        clarity = float(onset_env.max() / (onset_env.mean() + 1e-9))
    else:
        clarity = 1.0

    # ---- ① DP 锚点：librosa 的全局时序最优解，作为终审 base。
    # 用户手动填了 BPM 就直接听他的（他可能就是在纠正检测）。
    verdict = "user" if forced_bpm is not None else "keep"
    adj_score = 0.0
    vote_top2 = 0.0
    tempo, frames = librosa.beat.beat_track(
        y=y, sr=sr, hop_length=hop, start_bpm=120.0, trim=False
    )
    bpm_raw = float(np.atleast_1d(tempo)[0])

    if forced_bpm is not None:
        chosen = float(forced_bpm)
    else:
        dp_fold = _fold_bpm(bpm_raw)
        pool, _vote_top1, vote_top2 = _vote_candidates(y, sr, hop, dp_fold)
        # 终审 env 用全频段谱通量（onset_env）：实测它在「半拍有弱律动」的歌上
        # 区分度极好 —— 弱律动被 onset 检测的局部均值归一压低，拍点贴合度的差距
        # 拉得很开（实测一首歌 606ms 网格 3.77x vs 302ms 只有 1.1x）。多带
        # 正差分反而会把半拍切分律动算进来（实测半拍能量达拍点的 82%），让
        # 快侧候选白占便宜。
        chosen, adj_score, verdict = _energy_judge(
            onset_env, hop, sr, pool, dp_fold
        )
        # ---- 倍频复核：终审改判且与 base 差 2 倍 / 半速时，用**精细贴合度**
        # 复核一次（这正是「99.4 被推成 198.8」的形态 —— 能量 sum/√N 会因半拍
        # 律动偏爱快侧，而贴合度是独立的证据：实测一首歌 99.4 侧 3.85x、
        # 198.8 侧 2.95x）。base 的贴合度显著更高 → 否决改判；贴合度接近
        # （等强拍的两种读法物理等价）→ 放行能量结论 —— 那时快侧对跑步更实用。
        if verdict == "switch" and _is_octave(chosen, dp_fold):
            q_best = _grid_quality(y, onset_env, hop, sr, chosen)
            q_base = _grid_quality(y, onset_env, hop, sr, dp_fold)
            if q_base > q_best * config.JUDGE_OCTAVE_MARGIN:
                chosen, verdict = dp_fold, "keep-margin"

    # ---- ② 拍点：终审定下 BPM 后，让 beat_track 拿着它去找 DP 拍点序列
    #（bpm= 参数给定时 librosa 跳过 tempo 估计，拍点直接按这个速度跟踪）。
    if chosen > 0 and abs(chosen - bpm_raw) > 0.15:
        tempo, frames = librosa.beat.beat_track(
            y=y, sr=sr, hop_length=hop,
            start_bpm=chosen, bpm=chosen, trim=False,
        )
    bpm = bpm_raw if abs(bpm_raw - chosen) <= 0.15 else chosen
    if bpm <= 0:
        bpm = 120.0

    times = librosa.frames_to_time(frames, sr=sr, hop_length=hop)
    raw_times = [round(float(t) + offset, 3) for t in np.atleast_1d(times)]

    # ---- 拍点走严格等间隔的均匀网格，周期由 DP 拍点拟合出来（详见 _fit_period）。
    # 为什么要拟合而不是直接用 beat_track 的 tempo，见 _fit_period 的 docstring。
    fit_period, _fit_phase, fit_resid = _fit_period(
        np.asarray(raw_times, dtype=np.float64) - offset
    )
    if not np.isfinite(fit_period) or fit_period <= 0.01:
        # 拍点太少（极短的片段 / 起音极弱）—— 没有拟合的余地，退回 beat_track 的 tempo
        fit_period = 60.0 / max(bpm, 1e-6)
        fit_resid = 0.0

    grid_trim, grid_hit, grid_phase = _uniform_grid(
        onset_env, hop, sr, fit_period, y.size / float(sr)
    )
    detected = [round(float(t) + offset, 3) for t in grid_trim]
    if not detected:
        detected = list(raw_times)

    # 「BPM」必须和网格周期自洽 —— 倍率换算是拿它算的，不一致实际步频就会偏。
    # 注意这里是**拟合值**（《漂移》184.6 → 186.0），比 beat_track 的 tempo 更接近真值。
    bpm = 60.0 / fit_period

    # 拍点间隔的相对波动，走的是**DP 拍点**。最终网格是严格等间隔的、CV 恒等于 0，
    # 没有信息量；这个数回答的是另一件事 ——「这首曲子的速度稳不稳」。速度飘忽的
    # 曲子（现场版 / 古典）本身就不存在唯一 BPM，grid_fit_resid 也一起说明这件事。
    def _cv(values: list[float]) -> float:
        if len(values) < 3:
            return 0.0
        gaps = np.diff(np.asarray(values, dtype=np.float64))
        return float(np.std(gaps) / (float(np.mean(gaps)) + 1e-9))

    interval_cv = _cv(raw_times)

    grid = _extend_beat_grid(detected, duration, bpm)

    return {
        "bpm": round(bpm, 1),
        "bpm_raw": round(bpm_raw, 1),
        "adjudicated": verdict,
        "candidates": _bpm_candidates(bpm),
        "clarity": round(clarity, 2),
        "beats": grid,
        "beat_count": len(grid),
        "detected_count": len(detected),
        # interval_cv 走 DP 拍点（曲子的速度稳不稳）；grid_* 说明最终那条网格的情况
        "interval_cv": round(interval_cv, 4),
        "grid_period": round(fit_period, 6),
        "grid_phase": round(grid_phase + offset, 3),
        "grid_hit": round(grid_hit, 2),
        "grid_fit_resid": round(fit_resid, 4),
        "adjudicate_score": round(adj_score, 4),
        "vote_top2": round(vote_top2, 1),
        "trim_offset": round(offset, 3),
        "peaks": _peaks_from(y_raw, buckets),
    }


def detect_bpm(wav_path: Path) -> dict:
    """只要节拍信息、不取波形的轻量入口（自检脚本用）。"""
    return analyze(wav_path, buckets=0)


def warmup() -> None:
    """跑一遍最小规模的节拍检测，把 librosa / numba 的首次编译开销挪到服务启动时。

    实测 3 分钟音频：冷启动 10.4 秒，编译完成后 2.2 秒。不预热的话，
    第一个上传的人要多等 8 秒。
    """
    import librosa  # noqa: PLC0415

    y = np.zeros(int(config.ANALYSIS_SR * 12.0), dtype=np.float32)
    y[:: int(config.ANALYSIS_SR * 0.5)] = 0.9      # 每 0.5 秒一个脉冲，凑够 DP 的最小长度
    librosa.effects.trim(y, top_db=40.0)
    librosa.onset.onset_strength(y=y, sr=config.ANALYSIS_SR, hop_length=config.ANALYSIS_HOP)
    librosa.stft(y, n_fft=config.BAND_N_FFT, hop_length=config.ANALYSIS_HOP)
    librosa.beat.beat_track(
        y=y, sr=config.ANALYSIS_SR, hop_length=config.ANALYSIS_HOP, trim=False
    )


# --------------------------------------------------------------------------
# 听节拍对齐：把检测到的拍点变成可听的 click，叠在原曲上
# --------------------------------------------------------------------------


def build_click_track(
    beat_times: list[float] | np.ndarray,
    start: float,
    length: float,
    sr: int | None = None,
) -> np.ndarray:
    """按拍点时间生成一条 click 轨（单声道）。

    click 打在**检测到的拍点**上，而不是按均匀周期铺 —— 这样听到的就是算法
    真实的判断：click 和鼓点重合说明对，click 落在两个鼓点正中间说明错。
    """
    sr = sr or config.METRONOME_SR
    n = max(1, int(round(sr * length)))
    click = np.zeros(n, dtype=np.float32)

    tpl_len = max(1, int(sr * 0.05))
    tt = np.arange(tpl_len, dtype=np.float32) / float(sr)
    tpl = (np.exp(-70.0 * tt) * np.sin(2.0 * np.pi * config.METRONOME_FREQ * tt)).astype(
        np.float32
    )

    for bt in np.atleast_1d(np.asarray(beat_times, dtype=np.float64)):
        rel = float(bt) - start
        if rel < -0.05 or rel >= length:
            continue
        i0 = int(round(rel * sr))
        a = max(0, i0)
        b = min(n, i0 + tpl_len)
        if b > a:
            click[a:b] += tpl[: b - a]

    np.clip(click, -1.0, 1.0, out=click)
    return click


def click_times(
    beat_times: list[float] | np.ndarray,
    ratio: float,
    mapping: str = "1:1",
) -> np.ndarray:
    """把**原曲时间轴**上的拍点换算到**变速之后**的时间轴，并决定 click 打在哪。

    ``atempo`` 是整曲等比伸缩，所以原曲第 t 秒在成品里就是第 ``t / ratio`` 秒。

    ``1:1``：每个拍点打一声。
    ``1:2``：每两步踩一拍，所以拍点上打一声、相邻两拍的中点再补一声 ——
    成品里仍然是**每步一响**，不会变成每两步才响一次。
    """
    arr = np.asarray(beat_times, dtype=np.float64).ravel()
    if arr.size == 0:
        return arr
    ratio = float(ratio)
    if ratio <= 0:
        raise ValueError("倍率必须大于 0")

    if mapping == "1:2" and arr.size >= 2:
        mids = (arr[:-1] + arr[1:]) * 0.5
        arr = np.sort(np.concatenate([arr, mids]))

    return arr / ratio


def count_in_window(times: list[float] | np.ndarray, start: float, length: float) -> int:
    """数一数落在 ``[start, start+length)`` 里的打点个数，用于回显给用户核对。"""
    arr = np.asarray(times, dtype=np.float64).ravel()
    if arr.size == 0:
        return 0
    return int(np.count_nonzero((arr >= start - 1e-6) & (arr < start + length - 1e-6)))


def render_metronome(
    src: Path,
    dst: Path,
    beat_times: list[float] | np.ndarray,
    start: float,
    length: float,
    gain: float | None = None,
) -> Path:
    """把 click 轨混到原曲片段上，用于「听节拍对齐」。

    注意这里**不做时间伸缩**：目的是校对「检测出的拍点对不对」，音频和 click
    一起变速的话对齐关系不会改变，反而失去了验证意义。
    """
    import soundfile as sf  # 延迟导入，和 librosa 保持一致的做法

    gain = config.METRONOME_GAIN if gain is None else gain
    sr = config.METRONOME_SR
    dst.parent.mkdir(parents=True, exist_ok=True)

    click = build_click_track(beat_times, start, length, sr)
    click_path = config.TMP_DIR / f"click_{uuid.uuid4().hex[:12]}.wav"
    sf.write(str(click_path), click, sr, subtype="PCM_16")

    codec_args, _ext = output_settings()
    try:
        # 两路统一成同一采样率与声道布局，避免 amix 因格式不一致报错。
        # 原曲压到 0.82 倍是为了给 click 留头空间 —— 两边直接相加会超过 1.0，
        # 编码成 mp3 时被削平。末尾再加一道限幅兜底。
        fmt = f"aformat=sample_rates={sr}:channel_layouts=stereo"
        _run_checked(
            [
                "-y",
                "-ss", f"{start:.3f}",
                "-t", f"{length:.3f}",
                "-i", str(src),
                "-i", str(click_path),
                "-filter_complex",
                f"[0:a]{fmt},volume={config.METRONOME_BED_GAIN:.3f}[a0];"
                f"[1:a]{fmt},volume={gain:.3f}[c];"
                "[a0][c]amix=inputs=2:duration=first:normalize=0,"
                f"alimiter=limit={config.METRONOME_LIMIT:.3f}[a]",
                "-map", "[a]",
                "-vn",
                "-map_metadata", "-1",
                *codec_args,
                str(dst),
            ]
        )
    finally:
        # 中转的 click 轨只是顺手打扫，删不掉也不该让整次请求失败
        # （受管环境里的受控删除失败时会抛 SystemExit，会直接穿透成 500）。
        store.safe_unlink(click_path)

    return dst


# --------------------------------------------------------------------------
# 步频 → 倍率 换算
# --------------------------------------------------------------------------


@dataclass
class RatioPlan:
    ratio: float             # 最终采用的倍率
    requested_ratio: float   # 达成目标步频所需的倍率
    clamped: bool            # 是否被用户设定的范围截断
    target_bpm: float        # 目标音乐 BPM
    actual_spm: float        # 实际达成的步频
    natural: str             # green / yellow / red / clamped


def plan_ratio(
    source_bpm: float,
    target_spm: float,
    mapping: str,
    min_ratio: float,
    max_ratio: float,
) -> RatioPlan:
    """把「目标步频」换算成实际使用的变速倍率，并套上用户设定的范围。

    mapping ``"1:1"`` —— 每步踩一拍，需要的音乐 BPM = 目标步频。
    mapping ``"1:2"`` —— 每两步踩一拍，需要的音乐 BPM = 目标步频 ÷ 2。
    """
    if source_bpm <= 0:
        raise ValueError("原曲 BPM 必须大于 0")

    beats_per_step = 2.0 if mapping == "1:2" else 1.0
    target_bpm = target_spm / beats_per_step
    requested = target_bpm / source_bpm

    lo = max(config.HARD_MIN_RATIO, min(min_ratio, max_ratio))
    hi = min(config.HARD_MAX_RATIO, max(min_ratio, max_ratio))

    ratio = min(max(requested, lo), hi)
    clamped = abs(ratio - requested) > 1e-6
    actual_spm = source_bpm * ratio * beats_per_step

    if clamped:
        level = "clamped"
    else:
        drift = abs(ratio - 1.0)
        if drift <= config.NATURAL_GREEN:
            level = "green"
        elif drift <= config.NATURAL_YELLOW:
            level = "yellow"
        else:
            level = "red"

    return RatioPlan(
        ratio=round(ratio, 6),
        requested_ratio=round(requested, 6),
        clamped=clamped,
        target_bpm=round(target_bpm, 2),
        actual_spm=round(actual_spm, 1),
        natural=level,
    )


# --------------------------------------------------------------------------
# 时间伸缩（变速不变调）
# --------------------------------------------------------------------------


def atempo_chain(ratio: float) -> str:
    """构造 atempo 滤镜串。单级有效范围 0.5~2.0，超出就拆成多级串联。"""
    if ratio <= 0:
        raise ValueError("倍率必须大于 0")

    parts: list[str] = []
    remaining = float(ratio)
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.6f}")
    return ",".join(parts)


def render(
    src: Path,
    dst: Path,
    ratio: float,
    start: float | None = None,
    length: float | None = None,
    beat_times: list[float] | np.ndarray | None = None,
    mapping: str = "1:1",
    click_gain: float | None = None,
    bed_gain: float | None = None,
) -> Path:
    """时间伸缩渲染 —— 预览与导出共用。

    ``ratio > 1`` 变快（成品更短），``ratio < 1`` 变慢（成品更长），音高始终不变。
    ``start`` / ``length`` 以**原曲时间轴**为准，只渲染其中一段时使用。

    给了 ``beat_times``（原曲时间轴秒数）就额外叠一层节拍声。注意顺序是
    **先变速、再打点**（位置见 :func:`click_times`）—— 反过来的话 click 不会
    落在成品的鼓点上。传空列表得到一个「有混音链路但没有 click」的对照版本，
    校验脚本靠它把 click 单独减出来。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    if beat_times is not None:
        return _render_with_click(
            src, dst, ratio, start, length, beat_times, mapping, click_gain, bed_gain
        )

    args: list[str] = ["-y"]
    # -ss / -t 放在 -i 之前是输入选项：先按原曲时间轴截取，再变速。
    if start is not None and start > 0:
        args += ["-ss", f"{start:.3f}"]
    if length is not None and length > 0:
        args += ["-t", f"{length:.3f}"]
    args += ["-i", str(src)]

    codec_args, _ext = output_settings()
    args += [
        "-vn",
        "-map_metadata", "-1",
        "-filter:a", atempo_chain(ratio),
        *codec_args,
        str(dst),
    ]
    _run_checked(args)
    return dst


def _render_with_click(
    src: Path,
    dst: Path,
    ratio: float,
    start: float | None,
    length: float | None,
    beat_times: list[float] | np.ndarray,
    mapping: str,
    click_gain: float | None,
    bed_gain: float | None,
) -> Path:
    """变速 + 叠节拍声。两条路都按同一套滤镜走，保证试听与成品的听感一致。"""
    import soundfile as sf  # 延迟导入，和 librosa 保持一致的做法

    gain = config.METRONOME_GAIN if click_gain is None else float(click_gain)
    bed = config.METRONOME_EXPORT_BED_GAIN if bed_gain is None else float(bed_gain)
    sr = config.METRONOME_SR

    beats_out = click_times(beat_times, ratio, mapping)
    out_start = (start or 0.0) / ratio
    if length is not None and length > 0:
        out_length = length / ratio
    else:
        # 整首：拍点网格已经补齐到曲末，取最后一个拍点再留两秒余量。
        # 多出来的部分会被 amix 的 duration=first 截掉，少了反而会掉尾巴。
        tail = float(beats_out[-1]) if beats_out.size else out_start
        out_length = (tail - out_start) + 2.0

    click = build_click_track(beats_out, out_start, max(out_length, 0.1), sr)
    click_path = config.TMP_DIR / f"click_{uuid.uuid4().hex[:12]}.wav"
    sf.write(str(click_path), click, sr, subtype="PCM_16")

    codec_args, _ext = output_settings()
    try:
        # 两路统一成同一采样率与声道布局，否则 amix 会因格式不一致报错。
        # [0:a] 先按原曲时间轴截取（-ss/-t 是输入选项），再变速；
        # [1:a] 是已经在「变速后时间轴」上摆好的 click 轨。
        fmt = f"aformat=sample_rates={sr}:channel_layouts=stereo"
        args: list[str] = ["-y"]
        if start is not None and start > 0:
            args += ["-ss", f"{start:.3f}"]
        if length is not None and length > 0:
            args += ["-t", f"{length:.3f}"]
        args += [
            "-i", str(src),
            "-i", str(click_path),
            "-filter_complex",
            f"[0:a]volume={bed:.3f},{fmt},{atempo_chain(ratio)}[a0];"
            f"[1:a]{fmt},volume={gain:.3f}[c];"
            "[a0][c]amix=inputs=2:duration=first:normalize=0,"
            f"alimiter=limit={config.METRONOME_LIMIT:.3f}[a]",
            "-map", "[a]",
            "-vn",
            "-map_metadata", "-1",
            *codec_args,
            str(dst),
        ]
        _run_checked(args)
    finally:
        # 中转的 click 轨只是顺手打扫，删不掉也不该让整次请求失败。
        store.safe_unlink(click_path)

    return dst


def scaled_duration(duration: float, ratio: float) -> float:
    """变速后的时长：速度变成 ratio 倍，时长就除以 ratio。"""
    if ratio <= 0:
        raise ValueError("倍率必须大于 0")
    return duration / ratio
