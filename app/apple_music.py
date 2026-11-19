"""Apple Music 在线音源：按歌名搜索 + 下载完整音源。

设计约束（产品的「仅本机」定位）：

* **不联网之外的任何第三方**：只和 Apple Music 官方接口通信；
* **凭证只落本机**：cookies / Widevine CDM 都写在 ``data/`` 下，绝不上传；
* **不监听外网**：服务由 ``runbeat_desktop.py`` 绑在 ``127.0.0.1``，这里不做鉴权。

搜索结果本身**不需要 cookies**（走公开 catalog 接口），只有真正下载完整音源
才要订阅凭证。所以「没配 cookies 也能搜、能看、能选」——只在点下载时才拦。
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from pathlib import Path

from . import config

# 与 config.ALLOWED_SUFFIXES 对齐；下载产物只认这些，避免把封面图当音频
AUDIO_SUFFIXES: tuple[str, ...] = tuple(sorted(config.ALLOWED_SUFFIXES))

# 一次下载的硬上限。完整音源通常 3~8 MB，无损可能 30 MB+；
# 这里卡的是「别让一条异常逻辑把磁盘写满」，不是产品限制。
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

# 同时只跑一条下载：本机单用户，并发只会互相抢带宽，还让进度提示失真
_download_lock = threading.Lock()

# --------------------------------------------------------------------------
# 下载任务表（进度上报）
#
# 下载一首歌要经历「初始化 → 解析 → 下载 → 保存 → BPM 分析」五步，全程
# 20~60 秒。前端要进度条就必须能轮询 —— 所以下载不再塞在 HTTP 请求里同步
# 等完，而是登记成任务丢后台线程，前端拿 task_id 轮询状态。
#
# 仅本机单用户：内存表即可，服务重启丢掉也无妨（前端本来就要刷新重试）；
# 表项在任务结束（成功/失败）后保留 10 分钟供前端收尾读取，然后清掉。
# --------------------------------------------------------------------------

_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()

# 任务完结后保留多久（秒），给前端留出收尾轮询的窗口
_TASK_TTL = 600.0

# 五个阶段的权重（加起来 = 100%）。BPM 分析在最前面测过要 2~4 秒，
# 值得单独给一档，而不是混在「保存」里让进度条卡 90% 卡半天。
STAGE_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("init", 5),      # 初始化（拿 token、建 API 客户端）
    ("parse", 10),    # 解析歌曲链接
    ("download", 55), # 下载音轨（大头，由 amdl 的字节级 progress 细分）
    ("save", 10),     # 保存入库
    ("analyze", 20),  # 转 wav + BPM 检测
)


def _now() -> float:
    return time.time()


def _task_cleanup_locked() -> None:
    """清掉完结超时的任务。调用方必须已持有锁。"""
    deadline = _now() - _TASK_TTL
    for tid in [t for t, d in _tasks.items()
                if d["status"] in ("done", "error") and d.get("ended_at", 0) < deadline]:
        _tasks.pop(tid, None)


def task_create(title: str) -> str:
    """登记一个新下载任务，返回 task_id。"""
    tid = uuid.uuid4().hex[:16]
    with _tasks_lock:
        _task_cleanup_locked()
        _tasks[tid] = {
            "task_id": tid,
            "title": title,
            "status": "pending",       # pending → running → done / error
            "stage": "init",           # STAGE_WEIGHTS 里的键
            "stage_label": "排队中",
            "percent": 0,              # 0~100（整数，前端直接用）
            "detail": "",              # 当前步骤的一句人话说明
            "logs": [],                # 给用户看的日志行（倒序存，新的在前）
            "created_at": _now(),
            "ended_at": 0.0,
            "error": "",
            "record": None,            # 成功时放 _ingest_one 的返回
        }
    return tid


def task_get(tid: str) -> dict | None:
    with _tasks_lock:
        t = _tasks.get(tid)
        return dict(t) if t else None


def task_snapshot(tid: str) -> dict | None:
    """给前端的精简快照：不带 record 全文，成功时带 file_id/name。"""
    t = task_get(tid)
    if not t:
        return None
    snap = {
        "task_id": t["task_id"],
        "title": t["title"],
        "status": t["status"],
        "stage": t["stage"],
        "stage_label": t["stage_label"],
        "percent": t["percent"],
        "detail": t["detail"],
        "logs": t["logs"][:5],
    }
    if t["status"] == "done" and t["record"]:
        snap["data"] = {
            "file_id": t["record"].get("file_id"),
            "name": t["record"].get("name"),
        }
    if t["status"] == "error":
        snap["error"] = t["error"]
    return snap


def task_update(tid: str, **fields) -> None:
    with _tasks_lock:
        t = _tasks.get(tid)
        if t is None:
            return
        t.update(fields)


def task_log(tid: str, msg: str) -> None:
    """追加一行用户可读的日志（新的在前，最多留 40 行）。"""
    with _tasks_lock:
        t = _tasks.get(tid)
        if t is None:
            return
        t["logs"] = [msg, *t["logs"]][:40]


def task_stage(tid: str, stage: str, detail: str = "", base_percent: int | None = None) -> None:
    """推进到某个阶段。

    ``base_percent`` 是该阶段的起始百分比（从 STAGE_WEIGHTS 累加得出）；
    不传就按权重表算。detail 是当前步骤的人话说明。
    """
    label_map = dict(STAGE_WEIGHTS)
    if stage not in label_map:
        return
    if base_percent is None:
        acc = 0
        for key, weight in STAGE_WEIGHTS:
            if key == stage:
                break
            acc += weight
        base_percent = acc

    task_update(
        tid,
        stage=stage,
        stage_label={"init": "准备中", "parse": "解析歌曲", "download": "下载音源",
                     "save": "保存文件", "analyze": "分析节拍"}.get(stage, stage),
        percent=base_percent,
        detail=detail or "",
    )


def task_progress(tid: str, stage_base: int, stage_weight: int, done: int, total: int) -> None:
    """下载阶段的字节级进度：把 amdl 的 (completed, total) 折算到全局百分比。"""
    if total <= 0:
        return
    frac = max(0.0, min(1.0, done / total))
    percent = int(stage_base + stage_weight * frac)
    task_update(tid, percent=percent)

# 上一次探测到的「可用性」结果缓存，避免每次点开搜索都重新握手
_status_cache: dict = {"at": 0.0, "data": None}
_STATUS_TTL = 30.0


class AppleMusicError(RuntimeError):
    """给前端看的、已经翻译成人话的错误。"""


# --------------------------------------------------------------------------
# 日志接管（Windows 中文环境的关键修复）
# --------------------------------------------------------------------------

_log_ready = False
_log_lock = threading.Lock()


def _install_safe_logging() -> None:
    """把 gamdl/structlog 的输出接到一个**不会因编码崩掉**的流上。

    gamdl 是库，自己不配置 structlog，于是用默认的 ``PrintLoggerFactory``
    往 ``sys.stdout`` 写 debug 日志。Windows 下 stdout 是 GBK（cp936），
    而 Apple 的搜索结果里常混着越南语/日语曲名 —— 一旦日志里出现 GBK
    编不出来的字符，``UnicodeEncodeError`` 会把**整个搜索请求**打崩。

    实测崩溃点：某中文歌的搜索结果里带出 ``ển``（越南语字符），抛
    ``'gbk' codec can't encode character '\\u1eb1'``。

    修法：换成一个 ``errors="replace"`` 的包装流。字符编不出来就写 ``?``，
    日志丑一点无所谓，请求必须活下来。
    """
    global _log_ready
    with _log_lock:
        if _log_ready:
            return
        try:
            import io
            import sys

            import structlog

            # 拿底层二进制 buffer 重新包一层，指定 UTF-8 + 容错替换。
            # 拿不到 buffer（被别的东西接管过）就退到丢弃输出 —— 日志不该影响主流程。
            raw = getattr(sys.stdout, "buffer", None)
            if raw is not None:
                stream = io.TextIOWrapper(
                    raw, encoding="utf-8", errors="replace", line_buffering=True
                )
            else:
                stream = io.StringIO()

            structlog.configure(
                logger_factory=structlog.PrintLoggerFactory(file=stream),
            )
            _log_ready = True
        except Exception:  # noqa: BLE001 —— 日志配置失败绝不能连累业务
            _log_ready = True


# --------------------------------------------------------------------------
# 地区 / 设置
# --------------------------------------------------------------------------


def storefront() -> str:
    """默认地区。``cn`` 对中国区账号最稳；可在设置里改。"""
    return (config.load_settings().get("storefront") or "cn").strip().lower() or "cn"


def language() -> str:
    lang = (config.load_settings().get("language") or "zh-CN").strip()
    return lang or "zh-CN"


# --------------------------------------------------------------------------
# 凭证
# --------------------------------------------------------------------------


def cookies_ready() -> bool:
    """本机是否已存了一份非空的 cookies。只查存在性，不判有效性。"""
    path = config.COOKIES_FILE
    try:
        return path.is_file() and path.stat().st_size > 40
    except OSError:
        return False


def save_cookies(raw: str) -> dict:
    """把用户粘贴/选择的 cookies 内容规范成 Netscape 格式并落盘。

    两种来源都要过这里：

    * 界面粘贴的文本（可能带 ``Cookie:`` 前缀、可能是 ``k=v; k=v`` 一行式）；
    * 用户选的 ``cookies.txt`` 文件（本来就是 Netscape，原样复制）。

    返回 ``{saved: True, lines: n}``；内容不可识别时抛 :class:`AppleMusicError`。
    """
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise AppleMusicError("cookies 内容是空的")

    lines = _to_netscape(text)
    if not lines:
        raise AppleMusicError(
            "认不出这份 cookies 的格式。请用浏览器的 cookies 导出插件"
            "（Netscape 格式）导出，或直接粘贴 cookies.txt 的全部内容。"
        )

    body = "# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n"
    config.COOKIES_FILE.write_text(body, encoding="utf-8")
    _invalidate_status()
    return {"saved": True, "lines": len(lines)}


def _to_netscape(text: str) -> list[str]:
    """把各种形态的 cookies 归一成 Netscape 行。

    已经是 Netscape 的行原样保留（7 字段、tab 分隔）；
    否则尝试按 ``k=v; k=v`` 解析成 ``domain\\tFALSE\\t/\\tFALSE\\t0\\tk\\tv``。
    """
    out: list[str] = []
    loose: list[tuple[str, str]] = []

    for raw in text.split("\n"):
        line = raw.strip()
        # 浏览器导出插件会在文件头写 ``# Netscape HTTP Cookie File``，
        # 其它 ``#HttpOnly_`` 前缀的行是真数据，不能当注释丢掉
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue

        fields = line.split("\t")
        if len(fields) >= 7:
            # 顺手把 #HttpOnly_ 前缀还原成真正的域名
            if fields[0].startswith("#HttpOnly_"):
                fields[0] = fields[0].removeprefix("#HttpOnly_")
            out.append("\t".join(fields[:7]))
            continue

        # 一行式：去掉可选 ``Cookie:`` 前缀后按分号切
        body = line.removeprefix("Cookie:").strip()
        for pair in body.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            name = name.strip()
            if name:
                loose.append((name, value.strip()))

    # 一行式解析出的 cookie 补上 Apple Music 域名 —— 这些通常就是从
    # music.apple.com 复制的，缺域名会不被接受
    for name, value in loose:
        out.append(f".music.apple.com\tTRUE\t/\tTRUE\t{0}\t{name}\t{value}")

    return out


def save_cookies_from_file(source: Path) -> dict:
    """把用户选中的本地 cookies.txt 读进来再走一遍规范化。"""
    path = Path(source)
    if not path.is_file():
        raise AppleMusicError(f"找不到文件：{path.name}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise AppleMusicError(f"读不了这个文件：{exc}") from exc
    return save_cookies(text)


def clear_cookies() -> dict:
    removed = False
    try:
        config.COOKIES_FILE.unlink()
        removed = True
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AppleMusicError(f"删除失败：{exc}") from exc
    _invalidate_status()
    return {"removed": removed}


def wvd_path() -> Path | None:
    """用户自备的 Widevine CDM；没有就返回 ``None``（退回 gamdl 内置）。"""
    path = config.WVD_FILE
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def save_wvd(source: Path) -> dict:
    path = Path(source)
    if not path.is_file():
        raise AppleMusicError(f"找不到文件：{path.name}")
    if path.suffix.lower() != ".wvd":
        raise AppleMusicError("请选择 .wvd 文件（Widevine Device）")
    try:
        shutil.copyfile(path, config.WVD_FILE)
    except OSError as exc:
        raise AppleMusicError(f"复制失败：{exc}") from exc
    return {"saved": True, "size": config.WVD_FILE.stat().st_size}


def clear_wvd() -> dict:
    try:
        config.WVD_FILE.unlink()
        return {"removed": True}
    except FileNotFoundError:
        return {"removed": False}
    except OSError as exc:
        raise AppleMusicError(f"删除失败：{exc}") from exc


def _invalidate_status() -> None:
    _status_cache["at"] = 0.0
    _status_cache["data"] = None


def account_status(force: bool = False) -> dict:
    """探测凭证有效性：能否初始化 API、是否有有效订阅。

    结果缓存 30 秒 —— 设置页会反复调它，每次真去握手太浪费。
    """
    now = time.time()
    if not force and _status_cache["data"] and now - _status_cache["at"] < _STATUS_TTL:
        return dict(_status_cache["data"])

    data = {
        "cookies": cookies_ready(),
        "storefront": storefront(),
        "ready": False,
        "subscription": False,
        "wvd": "builtin" if wvd_path() is None else "custom",
        "message": "",
    }

    if not data["cookies"]:
        data["message"] = "还没有配置 cookies，只能搜索，不能下载完整音源"
    else:
        _install_safe_logging()
        try:
            api = _build_api()
            data["ready"] = True
            data["subscription"] = bool(getattr(api, "active_subscription", False))
            if not data["subscription"]:
                data["message"] = "cookies 可用，但账号没有有效的 Apple Music 订阅"
            else:
                data["message"] = "凭证有效，可以下载完整音源"
        except Exception as exc:  # noqa: BLE001 —— 一律翻译成人话给界面
            data["message"] = f"cookies 无法使用：{_humanize(exc)}"

    _status_cache["at"] = now
    _status_cache["data"] = dict(data)
    return data


# --------------------------------------------------------------------------
# 搜索（不需要 cookies）
# --------------------------------------------------------------------------


PAGE_SIZE = 12          # 每页条数：三列 × 四行，正好铺满一屏
MAX_PAGE = 30           # 翻页上限，防止一路点到底把 Apple 惹毛


def search(term: str, limit: int = PAGE_SIZE, offset: int = 0) -> dict:
    """按关键词搜歌曲。走公开 catalog 接口，**不需要 cookies 或订阅**。

    分页语义：``offset`` 是「跳过前多少条」，前端按 page 换算。
    ``has_next`` 直接看响应里的 ``next`` 链接（Apple 真实给出）。
    """
    keyword = (term or "").strip()
    if not keyword:
        raise AppleMusicError("搜索关键词是空的")
    if len(keyword) > 120:
        keyword = keyword[:120]

    limit = max(1, min(int(limit or PAGE_SIZE), 50))
    offset = max(0, min(int(offset or 0), PAGE_SIZE * MAX_PAGE))

    # 必须在真正调用 gamdl 之前接管日志，否则非 GBK 字符会打崩请求
    _install_safe_logging()

    async def _run() -> dict:
        api = await _api_for_search()
        return await api.get_search_results(
            keyword, types="songs", limit=limit, offset=offset
        )

    try:
        try:
            raw = _run_coro(_run())
        except Exception as exc:  # noqa: BLE001
            if not _looks_like_token_error(exc):
                raise
            # token 失效：清缓存重抓一次再试
            _invalidate_token()
            raw = _run_coro(_run())
    except Exception as exc:  # noqa: BLE001
        raise AppleMusicError(f"搜索失败：{_humanize(exc)}") from exc

    items, has_next = _shape_songs(raw)

    page = offset // limit + 1
    return {
        "term": keyword,
        "items": items,
        "page": page,
        "page_size": limit,
        "offset": offset,
        "has_next": has_next,
        "has_prev": page > 1,
        "storefront": storefront(),
    }


# --------------------------------------------------------------------------
# token 缓存
#
# 每次 AppleMusicApi.create() 都会重新抓 music.apple.com 首页 + index.js
# 提取 token，实测固定花 ~2.4 秒；真正的搜索只要 ~0.5 秒。token 是长期
# 有效的 JWT（exp 在 payload 里），缓存它能让翻页从 3 秒降到半秒。
#
# 只缓存 token 字符串，不缓存 client 对象：_run_coro 每次调用都可能开新的
# 事件循环，httpx 连接池绑定在创建它的循环上，跨循环复用 client 会炸；
# 而带 token 建对象实测只要 0.03 秒，重建无所谓。
# --------------------------------------------------------------------------

_token_cache: dict = {"token": None, "expires_at": 0.0}
_token_lock = threading.Lock()

# 拿不到 exp 时的保底有效期（秒）。JWT 本身有效期很长（数月），
# 12 小时足够保守，过期重新抓一次也就 2 秒。
_TOKEN_FALLBACK_TTL = 12 * 3600.0


def _jwt_exp(token: str) -> float | None:
    """从 JWT payload 里解出 exp 时间戳；解不出返回 None（不硬失败）。"""
    import base64
    import json

    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # 补齐 base64url 填充
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return float(exp) if exp else None
    except Exception:  # noqa: BLE001 —— 解析失败就退回固定 TTL
        return None


def _cached_token() -> str:
    """拿一个有效 token：缓存命中直接用，过期/为空才真去抓。"""
    with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
            return _token_cache["token"]

    async def _fetch() -> str:
        from gamdl.api import AppleMusicApi

        return await AppleMusicApi.get_token()

    token = _run_coro(_fetch())

    exp = _jwt_exp(token)
    # 过期前 1 小时就算过期，留出时钟偏差余量
    expires_at = (exp - 3600.0) if exp else (time.time() + _TOKEN_FALLBACK_TTL)

    with _token_lock:
        # 双检：可能另一个线程已经抓到更新的
        if not (_token_cache["token"] and time.time() < _token_cache["expires_at"]):
            _token_cache["token"] = token
            _token_cache["expires_at"] = expires_at
        return _token_cache["token"] or token


def _invalidate_token() -> None:
    with _token_lock:
        _token_cache["token"] = None
        _token_cache["expires_at"] = 0.0


def _looks_like_token_error(exc: Exception) -> bool:
    """判断异常是不是 token 失效类 —— 是就清缓存重试一次。"""
    text = str(exc).lower()
    return "401" in text or "unauthorized" in text or "token" in text


async def _api_for_search():
    """搜索用的 API 客户端：有 cookies 就带上（能拿到更多本地化结果），没有也能搜。

    token 走缓存（见 _cached_token），跳过每次 2.4 秒的抓取。
    """
    from gamdl.api import AppleMusicApi

    token = _cached_token()

    if cookies_ready():
        try:
            return await AppleMusicApi.create_from_netscape_cookies(
                str(config.COOKIES_FILE), language=language(), token=token
            )
        except Exception:  # noqa: BLE001 —— 凭证有问题不该拖累搜索
            pass
    return await AppleMusicApi.create(
        storefront=storefront(), language=language(), token=token
    )


def _shape_songs(raw: dict) -> tuple[list[dict], bool]:
    """把 Apple 的原始结构压成前端好用的扁平行。

    返回 ``(条目列表, 是否还有下一页)``：下一页判断直接看响应里的
    ``next`` 链接（Apple 真实给出），不再用「满一页就猜有」估算 ——
    恰好 12 条结果时会多出一个空页。
    """
    items: list[dict] = []
    songs = ((raw or {}).get("results") or {}).get("songs") or {}
    data = songs.get("data") or []
    for song in data:
        attrs = song.get("attributes") or {}
        artwork = attrs.get("artwork") or {}
        art_url = artwork.get("url") or ""
        # Apple 给的模板是 ``{w}x{h}bb.jpg``，按 200px 取缩略图够列表用
        if art_url:
            art_url = art_url.replace("{w}x{h}", "200x200")
        duration_ms = attrs.get("durationInMillis") or 0
        # 30 秒公开预览（无需 cookies），前端试听按钮用
        previews = attrs.get("previews") or []
        preview_url = (previews[0].get("url") or "") if previews else ""
        items.append(
            {
                "id": song.get("id"),
                "name": attrs.get("name") or "未知曲目",
                "artist": attrs.get("artistName") or "未知艺人",
                "album": attrs.get("albumName") or "",
                # 直接给下载器用的完整歌曲 URL（带 ?i= 才是单曲，否则是整张专辑）
                "url": attrs.get("url") or "",
                "duration": round(duration_ms / 1000.0, 1) if duration_ms else 0.0,
                "artwork": art_url,
                "preview": preview_url,
                "has_lyrics": bool(attrs.get("hasLyrics")),
                "release_date": attrs.get("releaseDate") or "",
                "isrc": attrs.get("isrc") or "",
            }
        )
    return items, bool(songs.get("next"))


# --------------------------------------------------------------------------
# 下载完整音源
# --------------------------------------------------------------------------


def download_song(
    url: str,
    output_dir: Path | None = None,
    progress=None,
    log=None,
) -> tuple[Path, Path]:
    """下载一首歌的完整音源，返回 ``(音频文件路径, 本次工作目录)``。

    每次都用一个**独占子目录**当 output_path：

    * amdl 会按元数据模板把文件写进树状目录（``艺人/专辑/曲目.mp3``），
      文件名由 Apple 的标题决定，调用方事先猜不到；
    * 独占目录让「本次下到了哪个文件」变成一次目录扫描，
      不需要前后 diff，也不会和别的任务串味。

    工作目录随文件一起返回：amdl 的目录模板经过它的清洗函数后可能再派生
    子目录（历史上 ``"."`` 被清洗成 ``"_"``），**不要**靠 ``file.parent``
    往上猜根目录 —— 让调用方把 work 显式传给 :func:`adopt` 做清理。

    ``progress`` 收到 ``(completed, total)``，``log`` 收到日志行。
    """
    song_url = (url or "").strip()
    if not song_url.startswith("http"):
        raise AppleMusicError("歌曲链接无效，请重新搜索后再试")
    if not cookies_ready():
        raise AppleMusicError("还没有配置 cookies，无法下载完整音源（设置里可以粘贴或选文件）")

    # 串行化：本机单用户，并发下载只会互相抢带宽
    if not _download_lock.acquire(timeout=1):
        raise AppleMusicError("已经有一首歌在下载了，等它完成再试")
    try:
        return _download_locked(song_url, output_dir, progress, log)
    finally:
        _download_lock.release()


def _download_locked(url: str, output_dir: Path | None, progress, log) -> tuple[Path, Path]:
    from amdl.core_downloader import download_urls
    from amdl.enums import CoverFormat, SongCodec

    _install_safe_logging()

    target_root = Path(output_dir or config.IMPORT_DIR)
    work = target_root / f"dl_{uuid.uuid4().hex[:12]}"
    work.mkdir(parents=True, exist_ok=True)

    _log = log or (lambda _msg: None)
    _log(f"开始下载：{url}")

    kwargs: dict = dict(
        urls=[url],
        cookies_path=config.COOKIES_FILE,
        output_path=work,
        temp_path=config.TMP_DIR,
        # 统一走单文件模板：不管是不是专辑曲目，都直接落在 work 根下，
        # 扫描时不用递归猜层级
        template_folder_album=".",
        template_folder_compilation=".",
        template_folder_no_album=".",
        template_file_single_disc="{title}",
        template_file_multi_disc="{disc}-{track:02d} {title}",
        template_file_no_album="{title}",
        template_file_playlist="{title}",
        codec_song=SongCodec.AAC_WEB,
        cover_format=CoverFormat.JPG,
        save_cover=False,
        save_playlist=False,
        no_synced_lyrics=True,
        overwrite=True,
        # 元数据语言跟「音源设置」走（下载接口支持这个参数，之前漏传，
        # 曲名/艺人恒为 en-US，界面显示和搜索结果对不上）
        language=language(),
        ffmpeg_path=_ffmpeg_path(),
        log_callback=_log,
        log_level="INFO",
    )

    # 用户自备了 CDM 就用自己的，否则让 gamdl 退回内置那份
    custom_wvd = wvd_path()
    if custom_wvd is not None:
        kwargs["wvd_path"] = custom_wvd
        _log("使用自定义 Widevine CDM")
    else:
        _log("使用内置 Widevine CDM")

    if progress is not None:
        kwargs["progress_callback"] = progress

    try:
        download_urls(**kwargs)
    except Exception as exc:  # noqa: BLE001
        _cleanup_dir(work)
        raise AppleMusicError(f"下载失败：{_humanize(exc)}") from exc

    found = _pick_audio_file(work)
    if found is None:
        _cleanup_dir(work)
        raise AppleMusicError(
            "下载没有产出音频文件。常见原因：cookies 已过期、账号没有有效订阅，"
            "或该曲目在当前地区不可用。"
        )

    size = found.stat().st_size
    if size <= 0:
        _cleanup_dir(work)
        raise AppleMusicError("下载到的文件是空的，请重试")
    if size > MAX_DOWNLOAD_BYTES:
        _cleanup_dir(work)
        raise AppleMusicError(
            f"文件超过 {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB，已放弃"
        )

    _log(f"下载完成：{found.name}（{size / 1024 / 1024:.1f} MB）")
    return found, work


def _pick_audio_file(folder: Path) -> Path | None:
    """在下载目录里挑出音频文件。

    优先按扩展名白名单找；找不到再退一步按「不是图片/歌词/字幕」筛，
    避免 Apple 换了容器格式时直接判失败。
    """
    candidates: list[Path] = []
    try:
        for path in folder.rglob("*"):
            if path.is_file() and path.stat().st_size > 0:
                candidates.append(path)
    except OSError:
        return None

    for suffix in AUDIO_SUFFIXES:
        for path in sorted(candidates):
            if path.suffix.lower() == suffix:
                return path

    junk = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".lrc", ".ttml", ".txt", ".json"}
    for path in sorted(candidates):
        if path.suffix.lower() not in junk:
            return path
    return None


def adopt(downloaded: Path, shown_name: str | None = None, work_dir: Path | None = None) -> Path:
    """把下载产物搬进 ``UPLOAD_DIR``，返回新的本地路径。

    搬离 ``imports/`` 的原因：``imports/`` 是暂存区，会被清理逻辑当垃圾删；
    而 ``uploads/`` 里的文件才被 ``store`` 登记、被后续变速流程正常使用。

    ``shown_name`` 是给界面看的名字（``歌名-歌手``），会写进登记表。
    物理文件名仍用随机 id，避免同名/非法字符问题。

    ``work_dir`` 是 :func:`download_song` 返回的本次工作目录，搬完就整个删掉。
    必须由调用方显式传入：amdl 的目录模板经过它的清洗函数后可能再派生子目录
    （历史上 ``"."`` 被清洗成 ``"_"``），靠 ``downloaded.parent`` 往上猜会停在
    子目录上，``dl_`` 前缀判断不中，残留目录就永远清不掉 —— ``imports/`` 下
    实测堆过 24 个这样的孤儿。
    """
    src = Path(downloaded)
    suffix = src.suffix.lower()
    if suffix not in config.ALLOWED_SUFFIXES:
        suffix = ".m4a"

    file_id = uuid.uuid4().hex[:16]
    dest = config.UPLOAD_DIR / f"{file_id}{suffix}"
    try:
        shutil.move(str(src), str(dest))
    except OSError as exc:
        # 移动失败时**不能**删工作目录 —— 音频文件还在里面，
        # 删了用户就得整首重下；留着手动排查或重试。
        raise AppleMusicError(f"保存文件失败：{exc}") from exc

    if work_dir is not None:
        _cleanup_dir(work_dir)
    elif src.parent.name.startswith("dl_"):
        # 兼容旧调用：拿不到显式 work 目录时才按父目录猜
        _cleanup_dir(src.parent)

    return dest


def display_name(song_name: str, artist: str) -> str:
    """界面上显示的名字：``歌名-歌手``。

    刻意**不带扩展名**：``.m4a`` 这类后缀对用户没意义，还会把歌名挤短。
    手动上传的文件仍保留原文件名（那是用户自己给的，不该动）。
    """
    title = (song_name or "").strip() or "未知曲目"
    who = (artist or "").strip()
    if not who:
        return _safe_stem(title)
    return _safe_stem(f"{title}-{who}")


def _safe_stem(name: str) -> str:
    """清掉文件名里的非法字符，并压到合理长度。"""
    cleaned = "".join("_" if ch in '\\/:*?"<>|' else ch for ch in str(name)).strip()
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        cleaned = "apple music"
    return cleaned[:120]


def _cleanup_dir(folder: Path) -> None:
    """删掉一次下载的整个工作目录。失败就留着，不影响主流程。"""
    try:
        if folder.is_dir() and folder.name.startswith("dl_"):
            shutil.rmtree(folder, ignore_errors=True)
    except OSError:
        pass


def cleanup_imports_stale(max_age_sec: float = 0.0) -> int:
    """清扫 ``imports/`` 里的孤儿工作目录，返回删掉的个数。

    正常路径下 :func:`adopt` 会删掉本次的 ``dl_*`` 目录；但下载中途断电、
    进程被杀、或历史版本留下的目录没人管。这里按目录名前缀 ``dl_`` 识别、
    按目录 mtime 判断是否超龄（``max_age_sec`` 默认 0 = 全部清扫），
    由 :func:`app.store.purge_expired` 周期性调用兜底。
    """
    removed = 0
    try:
        candidates = [p for p in config.IMPORT_DIR.iterdir() if p.is_dir()]
    except OSError:
        return 0
    deadline = time.time() - max_age_sec
    for folder in candidates:
        if not folder.name.startswith("dl_"):
            continue
        try:
            if folder.stat().st_mtime > deadline:
                continue
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _build_api():
    """同步构造一个带 cookies 的 API 客户端（供状态检查用）。token 走缓存。"""

    async def _run():
        from gamdl.api import AppleMusicApi

        return await AppleMusicApi.create_from_netscape_cookies(
            str(config.COOKIES_FILE), language=language(), token=_cached_token()
        )

    return _run_coro(_run())


def _run_coro(coro):
    """在同步上下文里跑一个协程。

    优先复用当前线程的事件循环；没有就新开一个。注意 amdl/gamdl 内部
    有些地方会自己建 loop，所以这里只在**没有**运行中 loop 时才跑，
    避免 ``RuntimeError: This event loop is already running``。
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # 已有 loop 在跑（FastAPI 的接口里就是这样），换线程跑完再回来
    result: dict = {}

    def _worker():
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            result["error"] = exc

    import threading

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _ffmpeg_path() -> str:
    """复用 runbeats 已有的 ffmpeg 定位逻辑（imageio 捆绑版）。"""
    from . import audio

    try:
        return audio.ffmpeg()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _humanize(exc: Exception) -> str:
    """把底层异常翻译成用户能读懂的一句话。"""
    text = str(exc) or exc.__class__.__name__
    low = text.lower()

    if "no active subscription" in low or "subscription" in low:
        return "账号没有有效的 Apple Music 订阅"
    if "cookies" in low and ("invalid" in low or "format" in low):
        return "cookies 格式不对或已失效，请重新导出"
    if "decryption" in low or "widevine" in low or "pssh" in low:
        return "解密失败：可能需要换成你自己的 Widevine CDM（高级设置里可以指定）"
    if "not available" in low or "unavailable" in low:
        return "该曲目在当前地区不可用，换个地区或换一首试试"
    if "timeout" in low or "timed out" in low:
        return "网络超时，请检查网络后重试"
    if "connect" in low:
        return "网络连接失败，请检查网络或代理设置"
    return text[:220]


# --------------------------------------------------------------------------
# 后台下载任务：下载 + 入库一条龙，全程可轮询进度
# --------------------------------------------------------------------------

# 进度看护线程的巡检间隔与启动等待（秒）。启动后等 3 秒还没看到
# 临时文件增长就放弃（多半是还没走到传输那一步），退回日志节点跳变。
_WATCH_INTERVAL = 0.5
_WATCH_GRACE = 3.0

# 下载段进度条的起止（与 run_import_task 的 stage_base 对齐）
_WATCH_BASE = 45
_WATCH_SPAN = 23   # 45 → 68


class _ProgressWatcher:
    """盯 TMP_DIR 里正在增长的临时文件，把字节量折算成下载段进度。

    amdl 下载时把加密分片写到 ``temp_path``（即 config.TMP_DIR），
    文件单调变大直到完成。下载锁保证同一时刻只有一个任务，所以
    「最新的那个在长的文件」就是本次的 —— 不需要身份标记。
    """

    def __init__(self, tid: str, expected_bytes: int) -> None:
        self.tid = tid
        self.expected = max(1, int(expected_bytes or 0))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_seen = 0.0   # 最近一次看到文件在长的时间

    def start(self) -> "_ProgressWatcher":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _scan_tmp(self) -> int:
        """TMP_DIR 里最大文件的字节数；空目录/读不了就是 0。"""
        try:
            return max(
                (p.stat().st_size for p in config.TMP_DIR.iterdir() if p.is_file()),
                default=0,
            )
        except OSError:
            return 0

    def _run(self) -> None:
        # 宽限期内没等到文件增长就退出，别为一个空目录空转整个下载期
        deadline = _now() + _WATCH_GRACE
        while not self._stop.is_set():
            size = self._scan_tmp()
            now = _now()
            if size > 0:
                self._last_seen = now
            elif self._last_seen == 0.0 and now > deadline:
                return

            frac = min(1.0, size / self.expected)
            mb = size / 1024 / 1024
            task_update(
                self.tid,
                percent=int(_WATCH_BASE + _WATCH_SPAN * frac),
                detail=f"下载音源… 已下载 {mb:.1f} MB",
            )
            self._stop.wait(_WATCH_INTERVAL)


def _start_progress_watcher(tid: str, expected_bytes: int) -> _ProgressWatcher:
    return _ProgressWatcher(tid, expected_bytes).start()

def run_import_task(
    tid: str,
    url: str,
    song_name: str,
    artist: str,
    ingest_fn,
    expected_bytes: int = 0,
) -> None:
    """后台线程入口：完整跑一遍「下载 → 保存 → 入库」，实时更新任务状态。

    ``ingest_fn`` 是 main._ingest_one（避免循环 import，由调用方注入）。
    ``expected_bytes`` 是搜索结果估的文件大小（时长 × 码率），给进度看护
    线程当分母；估不准只影响进度条快慢，不影响下载本身。

    五个阶段与百分比（STAGE_WEIGHTS 的累加）：
        init 0-5 → parse 5-15 → download 15-70 → save 70-80 → analyze 80-100
    """
    stage_base = {"init": 0, "parse": 5, "download": 15, "save": 70, "analyze": 80}

    def say(msg: str) -> None:
        task_log(tid, msg)

    def go_stage(stage: str, detail: str) -> None:
        task_stage(tid, stage, detail, base_percent=stage_base[stage])

    try:
        task_update(tid, status="running")
        go_stage("init", "连接 Apple Music…")

        if not cookies_ready():
            raise AppleMusicError("还没有配置 cookies，无法下载完整音源")

        go_stage("parse", f"解析《{song_name}》…")
        say(f"开始：《{song_name}-{artist}》")

        # ---- 下载 ----
        #
        # amdl 的 progress_callback 只在**每首**歌完成后回调一次 (n, total)，
        # 单曲场景拿不到字节级进度 —— 这是 amdl 的设计，不是能改的。
        # 进度这样给：
        #   1) 日志关键字先跳几个节点（15→22→30→45），让界面有反应；
        #   2) 起一个看护线程盯 TMP_DIR 里正在增长的临时文件，
        #      按文件大小折算 45→68 的真实进度并显示已下载 MB ——
        #      下载锁保证同时只有一个任务，临时文件归属不会错；
        #   3) 找不到临时文件时看护线程自动退出，退回节点跳变。
        milestones = {
            "creating applemusicbaseinterface": 22,
            "applemusicinterface ready": 30,
            'downloading "': 45,
        }
        reached: set[str] = set()

        def on_log(msg: str) -> None:
            say(msg)
            low = msg.lower()
            for key, pct in milestones.items():
                if key in low and key not in reached:
                    reached.add(key)
                    task_update(tid, percent=pct, detail="下载音源…")

        def on_progress(done: int, total: int) -> None:
            # 单曲 = 完成时的一把 (1,1)：到这就把下载段收尾
            task_update(tid, percent=68, detail="音源已取得，正在保存")

        go_stage("download", "下载音源…")
        watcher = _start_progress_watcher(tid, expected_bytes)
        try:
            downloaded, work = download_song(url, progress=on_progress, log=on_log)
        finally:
            watcher.stop()

        # ---- 保存 ----
        go_stage("save", "保存文件…")
        shown = display_name(song_name, artist)
        local_path = adopt(downloaded, shown, work_dir=work)

        # ---- BPM 分析入库 ----
        go_stage("analyze", "分析节拍中…")
        handle = _ImportHandle(local_path, shown)
        # _ingest_one 是 async 的，而本函数跑在普通工作线程 ——
        # 这里没有运行中的事件循环，直接 asyncio.run 开一个跑完即可。
        import asyncio

        record = asyncio.run(ingest_fn(handle))

        task_update(
            tid,
            status="done",
            percent=100,
            stage="analyze",
            stage_label="完成",
            detail="已加入歌曲库",
            record=record,
            ended_at=_now(),
        )
        say(f"完成：{shown}（{record.get('detected_bpm', '?')} BPM）")

    except BaseException as exc:  # noqa: BLE001 —— 后台线程必须自己兜住一切
        # 分支不能写反：AppleMusicError 已经是人话（_humanize 或手写的提示），
        # 原样展示；其它异常（httpx 超时、OSError 等）是英文技术串，
        # 才需要翻译。写反了用户看到的是 "Server disconnected" 这种原文。
        if isinstance(exc, AppleMusicError):
            msg = str(exc)
        else:
            msg = _humanize(exc)
        task_update(
            tid,
            status="error",
            stage_label="失败",
            detail=msg,
            error=msg,
            ended_at=_now(),
        )
        say(f"失败：{msg}")


class _ImportHandle:
    """给 ``_ingest_one`` 的轻量文件句柄（见 main._LocalFileHandle）。"""

    __slots__ = ("filename", "display_name", "path", "_handle")

    def __init__(self, path: Path, shown: str) -> None:
        self.path = Path(path)
        # filename 带扩展名只用于格式判断；display_name 是给人看的
        self.filename = shown + self.path.suffix
        self.display_name = shown
        self._handle = None

    async def read(self, size: int = -1) -> bytes:
        if self._handle is None:
            self._handle = self.path.open("rb")
        return self._handle.read(size)

    async def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0
