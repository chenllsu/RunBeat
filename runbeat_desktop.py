"""RunBeat 桌面版启动器（同时是 PyInstaller 的打包入口）。

双击 ``RunBeat.exe`` 之后：

1. 把数据目录、numba 编译缓存指到可写位置（必须在导入 librosa 之前完成）；
2. 从 8000 起找一个空闲端口，在后台线程里起 FastAPI 服务；
3. 等服务真的就绪（轮询 ``/api/health``）再弹出原生窗口 —— 否则用户只会看到白页；
4. **关掉窗口就是退出程序**。

原生窗口起不来时（缺 WebView2 运行时、老系统等）自动退回「默认浏览器」，
并用系统消息框说清楚，不让人对着一闪而过的窗口发愣。出错时也一并给出
日志文件的完整路径 —— 要反馈问题，总得先知道该发哪个文件。

开发环境同样能直接跑：

    .venv\\Scripts\\python.exe runbeat_desktop.py
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 8000
PORT_TRIES = 30
READY_TIMEOUT_SEC = 120.0
LOG_NAME = "runbeat.log"

WINDOW_TITLE = "RunBeat · 跑步步频音乐生成器"
WINDOW_SIZE = (1180, 840)
WINDOW_MIN = (940, 660)

_root: Path | None = None
_console: Path | None = None
_server = None


# --------------------------------------------------------------------------
# 输出与日志
# --------------------------------------------------------------------------


def _say(message: str) -> None:
    """打印一行状态。

    打包成窗口程序后 ``sys.stdout`` 是 ``None``，裸 ``print`` 会直接抛异常，
    所以这里两头都兜住：能打印就打印，同时始终写一份进启动日志。
    """
    line = f"{time.strftime('%H:%M:%S')} {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    if _console is not None:
        try:
            with open(_console, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass


def _log_path() -> str:
    return str((_root or Path(".")) / "logs" / LOG_NAME)


def _alert(title: str, message: str) -> None:
    """弹一个系统消息框。

    只在打包环境弹 —— 开发时控制台已经够用，弹窗反而碍事。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


def _attach_file_log(root: Path) -> None:
    """让 uvicorn 的日志同时落盘。

    两处都得留意：

    * uvicorn 的 logger 设了 ``propagate=False``，只配 root logger 一条都收不到，
      必须逐个挂上；
    * 反过来，``uvicorn.error`` 自己既没 handler 也没设 ``propagate`` —— 如果只挂
      handler 而不关 propagate，同一条日志会先经它自己输出一次、再冒泡到 ``uvicorn``
      又输出一次（实测整份日志重复）。
    """
    handler = RotatingFileHandler(
        root / "logs" / LOG_NAME, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )

    root_logger = logging.getLogger("")
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False      # 否则会被父 logger 再输出一遍


def _patch_std_streams(root: Path) -> None:
    """把缺失的标准输出接上。

    窗口形态（console=False）下 ``sys.stdout`` / ``sys.stderr`` 是 ``None``，
    这时任何裸 ``print``、以及 uvicorn 默认挂在 stderr 上的 StreamHandler
    都会直接抛异常。接到启动日志上，既不出错，出问题时也还有东西可看。
    """
    global _console

    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        stream = open(root / "logs" / "console.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    _console = None      # 已经有常驻 stream 了，_say 不必每次再开一次文件


# --------------------------------------------------------------------------
# 运行环境
# --------------------------------------------------------------------------


def _bootstrap_env() -> Path:
    """准备可写目录，返回数据目录。

    numba 找不到 ``NUMBA_CACHE_DIR`` 时会尝试写进程序目录 —— 打包后那可能是
    只读的，甚至是一次性的解压目录，必须提前改道。
    """
    from app import config  # 只碰路径常量，不会拉起重依赖

    root = config.DATA_DIR
    (root / "logs").mkdir(parents=True, exist_ok=True)
    cache = root / "numba_cache"
    cache.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("NUMBA_CACHE_DIR", str(cache))
    os.environ.setdefault("PYTHONUTF8", "1")
    return root


def _pick_port() -> int:
    """从 8000 起找一个空闲端口。

    Windows 上刻意不设 ``SO_REUSEADDR``：语义与 Linux 不同，
    会让 bind 成功落到已被别人占用的端口上。
    """
    for port in range(DEFAULT_PORT, DEFAULT_PORT + PORT_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, port))
            except OSError:
                continue
            return port
    raise RuntimeError(
        f"{DEFAULT_PORT}~{DEFAULT_PORT + PORT_TRIES - 1} 之间的端口全被占用了"
    )


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------


def _serve(port: int) -> None:
    global _server

    import uvicorn

    from app.main import app  # noqa: PLC0415 —— 环境准备好之后再导入

    # 传 app 对象而不是 "app.main:app" 字符串：冻结环境里 uvicorn 的
    # 字符串导入会去找源码文件，容易扑空。
    config = uvicorn.Config(app, host=HOST, port=port, log_level="info", access_log=False)

    # 文件日志必须在 Config 之后再挂：uvicorn 构造 Config 时会跑一遍
    # logging.config.dictConfig，把此前挂在这些 logger 上的 handler 全部清掉。
    # 挂在前面的话日志文件会一直是空的（实测踩到过）。
    if _root is not None:
        _attach_file_log(_root)

    _server = uvicorn.Server(config)
    _server.run()


def _wait_ready(url: str) -> bool:
    """轮询健康检查，服务真起来了才算就绪。"""
    deadline = time.time() + READY_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    return False


def _stop_server() -> None:
    if _server is not None:
        try:
            _server.should_exit = True
        except Exception:
            pass


# --------------------------------------------------------------------------
# 界面
# --------------------------------------------------------------------------


def _run_window(url: str) -> bool:
    """弹原生窗口；返回 False 表示这条路走不通，得退回浏览器。"""
    try:
        import webview
    except Exception as exc:
        _say(f"原生窗口组件不可用（{exc}），改用浏览器")
        return False

    try:
        webview.create_window(
            WINDOW_TITLE,
            url,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=WINDOW_MIN,
            background_color="#0f1216",
        )
    except Exception as exc:
        _say(f"创建窗口失败（{exc}），改用浏览器")
        return False

    try:
        # private_mode=False 才会把 localStorage 之类落到磁盘，
        # 否则用户每次打开都是全新会话。
        webview.start(
            private_mode=False,
            storage_path=str((_root or Path(".")) / "webview"),
        )
        return True
    except Exception as exc:
        _say(f"窗口启动失败（{exc}），改用浏览器")
        return False


def _run_browser(url: str) -> None:
    """兜底路径：开默认浏览器，服务留在后台直到用户自己结束进程。"""
    webbrowser.open(url)
    _say(f"已用浏览器打开 {url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------


def main() -> int:
    global _root, _console

    _root = _bootstrap_env()
    _console = _root / "logs" / "console.log"
    _patch_std_streams(_root)
    _say(f"RunBeat 启动中 · 数据目录 {_root}")
    # 文件日志在 _serve 里挂：uvicorn 会清掉已挂的 handler，得等它配完日志

    try:
        port = _pick_port()
    except RuntimeError as exc:
        _say(f"启动失败：{exc}")
        _alert("RunBeat 启动失败", f"{exc}\n\n日志文件：\n{_log_path()}")
        return 1

    url = f"http://{HOST}:{port}"
    threading.Thread(target=_serve, args=(port,), daemon=True, name="runbeat-uvicorn").start()

    if not _wait_ready(url):
        message = f"服务在 {READY_TIMEOUT_SEC:.0f} 秒内没有就绪。\n\n日志文件：\n{_log_path()}"
        _say(message)
        _alert("RunBeat 启动失败", message)
        return 1

    _say(f"服务就绪：{url}")

    if _run_window(url):
        _say("窗口已关闭，程序退出")
        _stop_server()
        return 0

    _alert(
        "RunBeat 无法打开应用窗口",
        "已改用默认浏览器打开页面。\n\n"
        "想排查原因的话，日志文件在这里：\n"
        f"{_log_path()}",
    )
    _run_browser(url)
    _stop_server()
    return 0


if __name__ == "__main__":
    sys.exit(main())
