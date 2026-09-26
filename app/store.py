"""上传文件登记表、预览片段缓存与过期清理。"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from pathlib import Path

from . import config

_lock = threading.RLock()
_files: dict[str, dict] = {}

# ---------------------------------------------------------------- 可清理的类别
# kind 是接口用的稳定标识，这张表是「哪些东西能删」的唯一出处（前端顺序照抄）
CLEARABLE: tuple[tuple[str, Path], ...] = (
    ("previews", config.PREVIEW_DIR),
    ("tmp", config.TMP_DIR),
    ("analysis", config.ANALYSIS_DIR),
    ("outputs", config.OUTPUT_DIR),
    ("uploads", config.UPLOAD_DIR),
)

CLEARABLE_KINDS: tuple[str, ...] = tuple(kind for kind, _ in CLEARABLE)


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def register(record: dict) -> dict:
    """把一条文件记录放进内存表，并打上创建时间。"""
    record.setdefault("created_at", time.time())
    with _lock:
        _files[record["file_id"]] = record
    return record


def get(file_id: str) -> dict | None:
    with _lock:
        return _files.get(file_id)


def update(file_id: str, **fields):
    with _lock:
        record = _files.get(file_id)
        if record is None:
            return None
        record.update(fields)
        return record


def cache_key(
    file_id: str,
    start: float,
    length: float,
    ratio: float,
    click_gain: float | None = None,
) -> str:
    """预览缓存键：文件 + 位置 + 倍率 + 节拍声音量（若开启）——
    漏掉节拍声这一维，勾开关后会命中旧缓存，听着像开关坏了。
    """
    raw = f"{file_id}|{start:.3f}|{length:.3f}|{ratio:.6f}"
    if click_gain is not None:
        raw += f"|click{click_gain:.3f}"
    return "prev_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def metronome_key(file_id: str, bpm: float, start: float, length: float) -> str:
    """节拍器混音缓存键。与倍率无关 —— 它不参与变速。"""
    raw = f"metro|{file_id}|{bpm:.2f}|{start:.3f}|{length:.3f}"
    return "metro_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def safe_unlink(path: Path) -> bool:
    """删文件，删不掉就算了（纯打扫动作，不影响调用方成败）。

    必须连 ``BaseException`` 一起挡：受管环境会把 ``unlink`` 劫持成受控删除，
    失败抛的是 ``SystemExit``（BaseException 子类），只捕 OSError 会穿透成 500。
    """
    try:
        if path.is_file():
            path.unlink()
            return True
    except KeyboardInterrupt:
        raise
    except BaseException:
        pass
    return False


def purge_expired() -> int:
    """清理超过保留期的中间文件与登记记录，返回删除的文件数。"""
    deadline = time.time() - config.RETENTION_HOURS * 3600
    removed = 0

    # 仍登记在册的上传文件可能正被使用，不按 mtime 删，下面按 created_at 收尾
    with _lock:
        live_uploads = {
            str(Path(rec["source_path"]))
            for rec in _files.values()
            if rec.get("source_path")
        }

    # UPLOAD_DIR 必须一起扫：登记表是易失的，服务重启后旧上传就成了清不掉的孤儿
    for folder in (
        config.PREVIEW_DIR,
        config.OUTPUT_DIR,
        config.ANALYSIS_DIR,
        config.TMP_DIR,
        config.UPLOAD_DIR,
    ):
        if not folder.is_dir():
            continue
        is_upload = folder == config.UPLOAD_DIR
        for path in folder.iterdir():
            try:
                if not path.is_file():
                    continue
                if is_upload and str(path) in live_uploads:
                    continue
                if path.stat().st_mtime < deadline:
                    removed += int(safe_unlink(path))
            except OSError:
                continue

    with _lock:
        stale = [fid for fid, rec in _files.items() if rec.get("created_at", 0.0) < deadline]
        for fid in stale:
            record = _files.pop(fid, None) or {}
            for key in ("source_path", "analysis_path"):
                path = record.get(key)
                if path:
                    removed += int(safe_unlink(Path(path)))

    return removed


def stats() -> dict:
    with _lock:
        tracked = len(_files)
    counts = {}
    for name, folder in (
        ("uploads", config.UPLOAD_DIR),
        ("analysis", config.ANALYSIS_DIR),
        ("previews", config.PREVIEW_DIR),
        ("outputs", config.OUTPUT_DIR),
        ("tmp", config.TMP_DIR),
    ):
        try:
            counts[name] = sum(1 for p in folder.iterdir() if p.is_file())
        except OSError:
            counts[name] = 0
    return {"tracked_files": tracked, "disk": counts}


def storage_report() -> dict:
    """各数据目录的文件数与占用字节（只读目录，删什么由用户勾选）。"""
    items = []
    total_files = 0
    total_bytes = 0
    for kind, folder in CLEARABLE:
        files = 0
        size = 0
        try:
            for path in folder.iterdir():
                try:
                    if path.is_file():
                        files += 1
                        size += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            pass
        items.append({"kind": kind, "files": files, "bytes": size})
        total_files += files
        total_bytes += size

    with _lock:
        tracked = len(_files)

    return {
        "items": items,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "tracked_files": tracked,
    }


def clear(kinds) -> dict:
    """按类别删除运行时文件，返回每类实际删掉的文件数与字节数。

    只认 ``CLEARABLE`` 登记过的目录，不给「拼个路径就删」留口子。
    ``uploads`` 特殊：删文件时必须同步摘掉内存登记记录（顺带删其分析文件），
    否则留下 source_path 已不存在的死记录，stats 计数虚高、页面点开 410。
    """
    wanted = set(kinds)
    result = {"items": [], "files": 0, "bytes": 0, "dropped_records": 0}

    for kind, folder in CLEARABLE:
        if kind not in wanted:
            continue
        try:
            paths = [p for p in folder.iterdir() if p.is_file()]
        except OSError:
            paths = []

        files = 0
        size = 0
        for path in paths:
            try:
                nbytes = path.stat().st_size
            except OSError:
                nbytes = 0
            if safe_unlink(path):
                files += 1
                size += nbytes

        result["items"].append({"kind": kind, "files": files, "bytes": size})
        result["files"] += files
        result["bytes"] += size

    if "uploads" in wanted:
        with _lock:
            gone = [
                fid
                for fid, rec in _files.items()
                if rec.get("source_path") and not Path(rec["source_path"]).is_file()
            ]
            for fid in gone:
                record = _files.pop(fid, None) or {}
                analysis = record.get("analysis_path")
                if analysis:
                    safe_unlink(Path(analysis))
        result["dropped_records"] = len(gone)

    return result
