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
# kind 是接口里用的稳定标识，顺序就是前端「缓存与数据」面板的展示顺序。
# 把这张表集中在一处，是为了让「有哪些东西能被删」只有一个出处 ——
# 前端复制一份顺序会导致两边悄悄跑偏。
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
    """预览缓存键：同一个文件 + 同一个位置 + 同一个倍率，只算一次。

    这样用户来回拖进度条、反复微调倍率时，绝大多数请求都能直接命中缓存。
    要不要叠节拍声（以及叠多大声）也是键的一部分 —— 否则勾选开关后
    会命中上一次那份「不带节拍声」的缓存，听上去像开关坏了。
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
    """删掉一个文件，删不掉就算了 —— 纯打扫动作，不该影响调用方的成败。

    这里必须连 ``BaseException`` 一起挡：某些受管运行环境会把 ``unlink``
    劫持成「先移进回收站」的受控删除，失败时抛的是 ``SystemExit``
    （``BaseException`` 的子类，**不是** ``Exception``）。只捕 ``OSError``
    的话它会一路穿透，把一次本该正常的请求或后台清理直接炸掉。
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

    # 仍登记在册的上传文件可能正被这次会话使用，不按 mtime 删；
    # 它们会在下面按 ``created_at`` 统一收尾。
    with _lock:
        live_uploads = {
            str(Path(rec["source_path"]))
            for rec in _files.values()
            if rec.get("source_path")
        }

    # ``UPLOAD_DIR`` 必须一起扫：它原先只靠内存登记表清理，而登记表是易失的 ——
    # 服务一重启，重启前上传的文件就失去登记，成了永远清不掉的孤儿，
    # 只会一直堆在磁盘上（实测积到 850 MB / 89 个孤儿文件）。
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
    """各数据目录的文件数与占用字节，供「缓存与数据」面板显示。

    只读目录、不做任何判断 —— 哪些该删由用户勾选，这里不预设。
    """
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
    """按类别删除运行时文件，返回每类**实际删掉**的文件数与字节数。

    只删 ``CLEARABLE`` 里登记过的目录，调用方传来的未知类别直接忽略 ——
    这个函数会被接口直接调用，不能给「拼个路径就删」留口子。

    ``uploads`` 比较特殊：它是被内存登记表引用着的那一份，删掉之后对应记录
    必须一起摘掉。否则表里会留着一批 ``source_path`` 已经不存在的死记录 ——
    页面上点它只会拿到 410，而 ``stats()`` 的文件计数会一直虚高。
    顺带把它的 ``analysis_path`` 也删掉，免得分析文件变成孤儿。
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
