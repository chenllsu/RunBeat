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

    ``forced_bpm`` 用来按用户指定的 BPM 重新生成拍点（候选值切换 / 手动修正）。
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
    # 因此会整段丢拍点（实测一个 0.5 秒静音 + 均匀点击的样本，开头 3 秒被清空）。
    # 音频级的静音修剪上面已经做过了，这里不需要它再裁一次。
    tempo, frames = librosa.beat.beat_track(
        y=y,
        sr=sr,
        hop_length=config.ANALYSIS_HOP,
        start_bpm=120.0,
        bpm=forced_bpm,
        trim=False,
    )
    bpm = float(np.atleast_1d(tempo)[0])

    times = librosa.frames_to_time(frames, sr=sr, hop_length=config.ANALYSIS_HOP)
    detected = [round(float(t) + offset, 3) for t in np.atleast_1d(times)]

    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=config.ANALYSIS_HOP)
    if onset_env.size:
        clarity = float(onset_env.max() / (onset_env.mean() + 1e-9))
    else:
        clarity = 1.0

    # 拍点间隔的相对波动，只看检测到的那些（补边是均匀的，会把波动冲淡）。
    # 检测「准不准」和检测「稳不稳」是两件事：速度飘忽的曲子（现场版 / 古典）
    # 本身就不存在唯一 BPM。
    if len(detected) >= 3:
        gaps = np.diff(np.asarray(detected, dtype=np.float64))
        interval_cv = float(np.std(gaps) / (float(np.mean(gaps)) + 1e-9))
    else:
        interval_cv = 0.0

    grid = _extend_beat_grid(detected, duration, bpm)

    return {
        "bpm": round(bpm, 1),
        "candidates": _bpm_candidates(bpm),
        "clarity": round(clarity, 2),
        "beats": grid,
        "beat_count": len(grid),
        "detected_count": len(detected),
        "interval_cv": round(interval_cv, 4),
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
