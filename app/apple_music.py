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

    实测崩溃点：搜「稻香」时日志里带出 ``ển``（越南语），抛
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
    另外返回 ``total`` 之类的精确总数 Apple 并不给（只有 next 链接），
    所以页数用「有没有下一页」推算 —— 见 ``has_next``。
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
        raw = await api.get_search_results(
            keyword, types="songs", limit=limit, offset=offset
        )
        return raw

    try:
        raw = _run_coro(_run())
    except Exception as exc:  # noqa: BLE001
        raise AppleMusicError(f"搜索失败：{_humanize(exc)}") from exc

    items = _shape_songs(raw)

    # 满一页就认为还有下一页；最后一页不足 12 条自然是 false。
    # 不做额外探测请求 —— 多打一次 Apple 换一个「是否还有」不值当。
    has_next = len(items) >= limit

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


async def _api_for_search():
    """搜索用的 API 客户端：有 cookies 就带上（能拿到更多本地化结果），没有也能搜。"""
    from gamdl.api import AppleMusicApi

    if cookies_ready():
        try:
            return await AppleMusicApi.create_from_netscape_cookies(
                str(config.COOKIES_FILE), language=language()
            )
        except Exception:  # noqa: BLE001 —— 凭证有问题不该拖累搜索
            pass
    return await AppleMusicApi.create(storefront=storefront(), language=language())


def _shape_songs(raw: dict) -> list[dict]:
    """把 Apple 的原始结构压成前端好用的扁平行。"""
    items: list[dict] = []
    data = (((raw or {}).get("results") or {}).get("songs") or {}).get("data") or []
    for song in data:
        attrs = song.get("attributes") or {}
        artwork = attrs.get("artwork") or {}
        art_url = artwork.get("url") or ""
        # Apple 给的模板是 ``{w}x{h}bb.jpg``，按 200px 取缩略图够列表用
        if art_url:
            art_url = art_url.replace("{w}x{h}", "200x200")
        duration_ms = attrs.get("durationInMillis") or 0
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
                "has_lyrics": bool(attrs.get("hasLyrics")),
                "release_date": attrs.get("releaseDate") or "",
                "isrc": attrs.get("isrc") or "",
            }
        )
    return items


# --------------------------------------------------------------------------
# 下载完整音源
# --------------------------------------------------------------------------


def download_song(
    url: str,
    output_dir: Path | None = None,
    progress=None,
    log=None,
) -> Path:
    """下载一首歌的完整音源，返回下载到的本地文件路径。

    每次都用一个**独占子目录**当 output_path：

    * amdl 会按元数据模板把文件写进树状目录（``艺人/专辑/曲目.mp3``），
      文件名由 Apple 的标题决定，调用方事先猜不到；
    * 独占目录让「本次下到了哪个文件」变成一次目录扫描，
      不需要前后 diff，也不会和别的任务串味。

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


def _download_locked(url: str, output_dir: Path | None, progress, log) -> Path:
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
    return found


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


def adopt(downloaded: Path, shown_name: str | None = None) -> Path:
    """把下载产物搬进 ``UPLOAD_DIR``，返回新的本地路径。

    搬离 ``imports/`` 的原因：``imports/`` 是暂存区，会被清理逻辑当垃圾删；
    而 ``uploads/`` 里的文件才被 ``store`` 登记、被后续变速流程正常使用。

    ``shown_name`` 是给界面看的名字（``歌名-歌手``），会写进登记表。
    物理文件名仍用随机 id，避免同名/非法字符问题。
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
        raise AppleMusicError(f"保存文件失败：{exc}") from exc
    finally:
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


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _build_api():
    """同步构造一个带 cookies 的 API 客户端（供状态检查用）。"""

    async def _run():
        from gamdl.api import AppleMusicApi

        return await AppleMusicApi.create_from_netscape_cookies(
            str(config.COOKIES_FILE), language=language()
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
