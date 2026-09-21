"""FastAPI 应用：上传 → 节拍检测 → 预览 → 导出。

预览与导出调用的是**同一个** :func:`app.audio.render`，区别只有截取范围：
预览传 ``start`` / ``length`` 截一段，导出留空处理整首。
"""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import audio, config, store


def _warmup() -> None:
    """后台预热：解析 ffmpeg 路径、探测编码器、预载 librosa 并触发 numba 编译。"""
    try:
        audio.ffmpeg()
        audio.output_settings()
    except Exception:
        pass
    try:
        audio.warmup()
    except Exception:
        pass


def _cleanup_loop() -> None:
    while True:
        time.sleep(1800)
        try:
            store.purge_expired()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    threading.Thread(target=_warmup, daemon=True).start()
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    yield


app = FastAPI(title="RunBeat", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# 请求模型
# --------------------------------------------------------------------------


class StretchRequest(BaseModel):
    file_id: str
    source_bpm: float = Field(..., gt=20, le=400, description="原曲 BPM，允许用户手动修正")
    target_spm: float = Field(..., gt=40, le=300, description="目标步频")
    mapping: str = Field("1:1", pattern=r"^(1:1|1:2)$", description="1:1 每步一拍 / 1:2 每两步一拍")
    min_ratio: float = Field(config.DEFAULT_MIN_RATIO, gt=0)
    max_ratio: float = Field(config.DEFAULT_MAX_RATIO, gt=0)
    metronome: bool = Field(False, description="是否在成品里叠一层节拍声")
    metronome_gain: float = Field(
        config.METRONOME_GAIN, ge=0.0, le=1.0, description="节拍声音量（0~1）"
    )
    # 裁剪区间，都以**原曲时间轴**为准。默认 0 / None 表示整首，也就是不裁剪。
    # 预览与导出共用这套字段，区别只在「长度留空时算多长」——见 _clip_window。
    start: float = Field(0.0, ge=0, description="从原曲第几秒开始处理")
    length: float | None = Field(None, gt=0, description="处理多少秒，缺省见各接口")


class MetronomeRequest(BaseModel):
    file_id: str
    source_bpm: float = Field(..., gt=20, le=400, description="用来打 click 的 BPM")
    start: float = Field(0.0, ge=0, description="从原曲第几秒开始截取")
    length: float | None = Field(None, gt=0, description="截取多少秒，缺省用默认值")


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------


def _load_record(file_id: str) -> dict:
    record = store.get(file_id)
    if record is None:
        raise HTTPException(404, "文件不存在或已过期，请重新上传")
    if not Path(record["source_path"]).is_file():
        raise HTTPException(410, "原始文件已被清理，请重新上传")
    return record


def _plan(req: StretchRequest) -> audio.RatioPlan:
    try:
        return audio.plan_ratio(
            source_bpm=req.source_bpm,
            target_spm=req.target_spm,
            mapping=req.mapping,
            min_ratio=req.min_ratio,
            max_ratio=req.max_ratio,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _safe_output_name(name: str) -> str:
    if Path(name).name != name or not name:
        raise HTTPException(400, "非法的文件名")
    return name


def _mmss(sec: float) -> str:
    """秒 → ``1m20s``。要进文件名，所以不能带冒号（Windows 不允许）。"""
    minutes, seconds = divmod(int(round(max(0.0, sec))), 60)
    return f"{minutes}m{seconds:02d}s"


def _clip_window(
    req: StretchRequest, total: float, fallback: float | None = None
) -> tuple[float, float]:
    """把请求里的裁剪区间夹进 ``[0, total]``，返回 ``(start, length)``。

    只给了 ``start`` 没给 ``length`` 时，``fallback`` 给定时取它（预览的语义是
    「截一小段试听」），没给就一路取到曲末（导出的语义是「裁到结尾」）。

    太短的片段直接拒绝，而不是悄悄生成一个听不出节奏的文件 —— 那样用户会
    以为是功能坏了。
    """
    start = max(0.0, min(float(req.start), total))
    if req.length is not None:
        length = min(float(req.length), total - start)
    elif fallback is not None:
        length = min(float(fallback), total - start)
    else:
        length = total - start

    if length < config.MIN_CLIP_SEC:
        raise HTTPException(
            400,
            f"这一段只剩 {length:.1f} 秒了，至少要 {config.MIN_CLIP_SEC:.0f} 秒才能听出节奏",
        )
    return start, length


def _beats_for(record: dict, bpm: float) -> dict:
    """取某个 BPM 下的拍点，按 BPM 缓存（候选值切换会反复问，避免重复解码）。

    同步函数，调用方负责丢进线程池。
    """
    key = f"{bpm:.2f}"
    cache = dict(record.get("beats_cache") or {})
    entry = cache.get(key)
    if entry is None:
        info = audio.analyze(Path(record["analysis_path"]), buckets=0, forced_bpm=bpm)
        entry = {
            "beats": info["beats"],
            "beat_count": info["beat_count"],
            "detected_count": info["detected_count"],
            "interval_cv": info["interval_cv"],
        }
        cache[key] = entry
        if len(cache) > config.BEAT_CACHE_MAX:
            for stale in list(cache)[: len(cache) - config.BEAT_CACHE_MAX]:
                cache.pop(stale, None)
        store.update(record["file_id"], beats_cache=cache)
    return entry


async def _beats_for_async(record: dict, bpm: float) -> dict:
    try:
        return await run_in_threadpool(_beats_for, record, bpm)
    except Exception as exc:
        raise HTTPException(500, f"拍点计算失败：{exc}") from exc


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "ffmpeg": audio.ffmpeg(),
        "output": audio.output_settings()[1],
        "store": store.stats(),
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in config.ALLOWED_SUFFIXES:
        allowed = " / ".join(sorted(s.lstrip(".") for s in config.ALLOWED_SUFFIXES))
        raise HTTPException(400, f"不支持这种格式，请上传 {allowed}")

    file_id = store.new_id()
    original_name = file.filename or f"{file_id}{suffix}"
    source_path = config.UPLOAD_DIR / f"{file_id}{suffix}"

    # ---- 分块落盘，边写边卡大小上限，避免大文件把内存吃满
    size = 0
    try:
        with source_path.open("wb") as handle:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        413,
                        f"文件超过 {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB 上限",
                    )
                handle.write(chunk)
    except HTTPException:
        store.safe_unlink(source_path)
        raise
    finally:
        await file.close()

    if size == 0:
        store.safe_unlink(source_path)
        raise HTTPException(400, "上传的文件是空的")

    # ---- 探测时长与采样率
    try:
        info = await run_in_threadpool(audio.probe, source_path)
    except Exception as exc:
        store.safe_unlink(source_path)
        raise HTTPException(400, f"解析失败：{exc}") from exc

    if info.duration < config.MIN_DURATION_SEC:
        store.safe_unlink(source_path)
        raise HTTPException(
            400,
            f"音频只有 {info.duration:.1f} 秒，至少需要 {config.MIN_DURATION_SEC:.0f} 秒才能检测节拍",
        )
    if info.duration > config.MAX_DURATION_SEC:
        store.safe_unlink(source_path)
        raise HTTPException(
            400,
            f"音频长 {info.duration / 60:.1f} 分钟，最多支持 {config.MAX_DURATION_SEC / 60:.0f} 分钟",
        )

    # ---- 转分析用 wav 并检测节拍（波形峰值一并算好，避免二次解码）
    analysis_path = config.ANALYSIS_DIR / f"{file_id}.wav"
    try:
        await run_in_threadpool(audio.to_analysis_wav, source_path, analysis_path)
        detected = await run_in_threadpool(
            audio.analyze, analysis_path, config.WAVEFORM_BUCKETS
        )
    except Exception as exc:
        store.safe_unlink(source_path)
        store.safe_unlink(analysis_path)
        raise HTTPException(422, f"节拍检测失败：{exc}") from exc

    store.register(
        {
            "file_id": file_id,
            "original_name": original_name,
            "source_path": source_path,
            "analysis_path": analysis_path,
            "duration": round(info.duration, 3),
            "sample_rate": info.sample_rate,
            "codec": info.codec,
            "bpm": detected["bpm"],
            "candidates": detected["candidates"],
            "clarity": detected["clarity"],
            "interval_cv": detected["interval_cv"],
            "peaks": detected["peaks"],
            # 拍点按 BPM 缓存，检测值这一套先放进去
            "beats_cache": {
                f"{detected['bpm']:.2f}": {
                    "beats": detected["beats"],
                    "beat_count": detected["beat_count"],
                    "detected_count": detected["detected_count"],
                    "interval_cv": detected["interval_cv"],
                }
            },
        }
    )

    return {
        "file_id": file_id,
        "name": original_name,
        "duration": round(info.duration, 3),
        "sample_rate": info.sample_rate,
        "codec": info.codec,
        "detected_bpm": detected["bpm"],
        "bpm_candidates": detected["candidates"],
        "clarity": detected["clarity"],
        "beat_count": detected["beat_count"],
        "interval_cv": detected["interval_cv"],
        "source_url": f"/media/source/{source_path.name}",
        "defaults": {
            "spm": config.SPM_DEFAULT,
            "spm_min": config.SPM_MIN,
            "spm_max": config.SPM_MAX,
            "min_ratio": config.DEFAULT_MIN_RATIO,
            "max_ratio": config.DEFAULT_MAX_RATIO,
            "preview_length": config.PREVIEW_LENGTH_SEC,
            "wave_window": config.WAVE_WINDOW_SEC,
            "metronome_length": config.METRONOME_LENGTH_SEC,
        },
    }


@app.get("/api/waveform/{file_id}")
async def waveform(file_id: str) -> dict:
    """整首波形峰值。上传时已算好，这里只是从内存里读出来，秒回。

    前端一次取全后缓存，之后滚动播放只是 canvas 平移，不再发请求。
    """
    record = _load_record(file_id)
    peaks = record.get("peaks") or []
    return {
        "buckets": len(peaks),
        "duration": record["duration"],
        "window": config.WAVE_WINDOW_SEC,
        "peaks": peaks,
    }


@app.get("/api/beats/{file_id}")
async def beats(file_id: str, bpm: float = Query(..., gt=20, le=400)) -> dict:
    """按指定 BPM 给出拍点时间（原曲时间轴，秒）。"""
    record = _load_record(file_id)
    entry = await _beats_for_async(record, bpm)
    return {"bpm": round(bpm, 2), **entry}


@app.post("/api/metronome")
async def metronome(req: MetronomeRequest) -> dict:
    """「听节拍对齐」：原曲片段 + 按检测拍点打出的 click。

    刻意不做时间伸缩 —— 要校验的是「检测出的拍点对不对」，
    音频和 click 一起变速的话对齐关系不变，就失去验证意义了。
    """
    record = _load_record(req.file_id)
    source = Path(record["source_path"])
    total = float(record["duration"])

    start = min(max(req.start, 0.0), max(0.0, total - 1.0))
    length = min(req.length or config.METRONOME_LENGTH_SEC, total - start)
    if length <= 0.5:
        raise HTTPException(400, "这个位置太靠近结尾了，换个位置试试")

    entry = await _beats_for_async(record, req.source_bpm)

    key = store.metronome_key(req.file_id, req.source_bpm, start, length)
    _codec_args, ext = audio.output_settings()
    target = config.PREVIEW_DIR / f"{key}{ext}"
    cached = target.is_file()

    if not cached:
        try:
            await run_in_threadpool(
                audio.render_metronome,
                source,
                target,
                entry["beats"],
                start,
                length,
                config.METRONOME_GAIN,
            )
        except Exception as exc:
            store.safe_unlink(target)
            raise HTTPException(500, f"节拍预览生成失败：{exc}") from exc

    in_window = sum(1 for b in entry["beats"] if start <= b < start + length)
    return {
        "url": f"/media/preview/{target.name}",
        "cached": cached,
        "start": round(start, 3),
        "length": round(length, 3),
        "bpm": round(req.source_bpm, 2),
        "beats_in_window": in_window,
    }


@app.post("/api/preview")
async def preview(req: StretchRequest) -> dict:
    record = _load_record(req.file_id)
    plan = _plan(req)

    source = Path(record["source_path"])
    total = float(record["duration"])
    # 没给长度就沿用「精确试听」的老约定：截 20 秒
    start, length = _clip_window(req, total, fallback=config.PREVIEW_LENGTH_SEC)

    want_click = bool(req.metronome and req.metronome_gain > 0)
    key = store.cache_key(
        req.file_id, start, length, plan.ratio, req.metronome_gain if want_click else None
    )
    _codec_args, ext = audio.output_settings()
    target = config.PREVIEW_DIR / f"{key}{ext}"
    cached = target.is_file()

    beats: list[float] | None = None
    clicks = 0
    if want_click:
        entry = await _beats_for_async(record, req.source_bpm)
        beats = entry["beats"]
        clicks = audio.count_in_window(
            audio.click_times(beats, plan.ratio, req.mapping),
            start / plan.ratio,
            length / plan.ratio,
        )

    if not cached:
        try:
            await run_in_threadpool(
                audio.render,
                source,
                target,
                plan.ratio,
                start,
                length,
                beats,
                req.mapping,
                req.metronome_gain,
            )
        except Exception as exc:
            store.safe_unlink(target)
            raise HTTPException(500, f"预览生成失败：{exc}") from exc

    return {
        "url": f"/media/preview/{target.name}",
        "cached": cached,
        "start": round(start, 3),
        "length": round(length, 3),
        "output_length": round(audio.scaled_duration(length, plan.ratio), 3),
        "ratio": plan.ratio,
        "requested_ratio": plan.requested_ratio,
        "actual_spm": plan.actual_spm,
        "clamped": plan.clamped,
        "natural": plan.natural,
        "metronome": want_click,
        "metronome_gain": round(req.metronome_gain, 3) if want_click else 0.0,
        "clicks_in_window": clicks,
    }


@app.post("/api/export")
async def export(req: StretchRequest) -> dict:
    record = _load_record(req.file_id)
    plan = _plan(req)

    source = Path(record["source_path"])
    total = float(record["duration"])
    # 没给长度就是「裁到曲末」；给了就是个真正的裁剪区间
    start, length = _clip_window(req, total)
    clipped = start > 0.005 or length < total - 0.005

    want_click = bool(req.metronome and req.metronome_gain > 0)

    beats: list[float] | None = None
    clicks = 0
    if want_click:
        entry = await _beats_for_async(record, req.source_bpm)
        beats = entry["beats"]
        # 拍点先换算到「变速后的时间轴」，再数落在窗口里的 —— 与真正打点的口径一致
        clicks = audio.count_in_window(
            audio.click_times(beats, plan.ratio, req.mapping),
            start / plan.ratio,
            audio.scaled_duration(length, plan.ratio),
        )

    _codec_args, ext = audio.output_settings()
    # 文件名要带上节拍声和区间这两维，否则同一个 file_id + 同一个倍率下，
    # 「带节拍声 / 不带」「整首 / 某个片段」会互相覆盖对方。
    tag = f"_m{int(round(req.metronome_gain * 100)):03d}" if want_click else ""
    clip_tag = f"_c{_mmss(start)}-{_mmss(start + length)}" if clipped else ""
    target = config.OUTPUT_DIR / f"{req.file_id}_{int(plan.ratio * 1000):04d}{tag}{clip_tag}{ext}"

    started = time.time()
    try:
        await run_in_threadpool(
            audio.render,
            source,
            target,
            plan.ratio,
            # 不裁剪时仍走「整首」那条老分支（它会多留一点尾巴，保证最后一声
            # click 不被切掉），只在真要裁剪时才把区间传下去。
            start if clipped else None,
            length if clipped else None,
            beats,
            req.mapping,
            req.metronome_gain,
        )
    except Exception as exc:
        store.safe_unlink(target)
        raise HTTPException(500, f"生成失败：{exc}") from exc

    out_duration = audio.scaled_duration(length, plan.ratio)
    stem = Path(record["original_name"]).stem[:40] or "runbeat"
    range_suffix = f"_{_mmss(start)}-{_mmss(start + length)}" if clipped else ""
    download_name = (
        f"{stem}_{int(round(plan.actual_spm))}spm{'_beat' if want_click else ''}"
        f"{range_suffix}{ext}"
    )

    return {
        "url": f"/media/output/{target.name}",
        "download_url": f"/api/download/{target.name}?as_name={quote(download_name)}",
        "filename": download_name,
        "ratio": plan.ratio,
        "requested_ratio": plan.requested_ratio,
        "requested_spm": req.target_spm,
        "actual_spm": plan.actual_spm,
        "mapping": req.mapping,
        "clamped": plan.clamped,
        "natural": plan.natural,
        "source_duration": record["duration"],
        "clip_start": round(start, 3),
        "clip_length": round(length, 3),
        "clipped": clipped,
        "output_duration": round(out_duration, 2),
        "metronome": want_click,
        "metronome_gain": round(req.metronome_gain, 3) if want_click else 0.0,
        "click_count": clicks,
        "elapsed": round(time.time() - started, 2),
    }


@app.get("/api/download/{name}")
async def download(name: str, as_name: str | None = None):
    path = config.OUTPUT_DIR / _safe_output_name(name)
    if not path.is_file():
        raise HTTPException(404, "文件不存在或已被清理，请重新生成")
    return FileResponse(
        path,
        media_type="audio/mpeg" if path.suffix == ".mp3" else "audio/mp4",
        filename=as_name or path.name,
    )


# --------------------------------------------------------------------------
# 静态资源（注意：根路径的挂载必须放在最后）
# --------------------------------------------------------------------------

app.mount("/media/source", StaticFiles(directory=str(config.UPLOAD_DIR)), name="source")
app.mount("/media/preview", StaticFiles(directory=str(config.PREVIEW_DIR)), name="preview")
app.mount("/media/output", StaticFiles(directory=str(config.OUTPUT_DIR)), name="output")
app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")
