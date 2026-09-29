"""RunBeat 桌面版启动器（也是 PyInstaller 的打包入口）。

开发环境同样能直接跑：

    .venv\\Scripts\\python.exe runbeat_desktop.py

它只做"打包后会变的那几件事"，业务逻辑一概不碰：

1. 把 numba 编译缓存指到可写目录 —— 必须在 librosa/numba 被导入**之前**做；
2. 端口自适应：8000 被占用时自动往后找，不再写死；
3. 服务就绪后自动打开浏览器；
4. 日志同时落一份到数据目录，出问题时有据可查。
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
READY_TIMEOUT_SEC = 90.0

BANNER = (
    "  RunBeat 已启动\n"
    "  请在浏览器打开：{url}\n"
    "  数据目录：{root}\n"
    "  关闭本窗口即退出程序"
)


def _bootstrap_env() -> Path:
    """准备可写的运行环境，返回数据目录。

    numba 找不到 ``NUMBA_CACHE_DIR`` 时会尝试写进程序目录 —— 打包后那是只读的，
    甚至是一次性的临时解压目录，必须提前改道。
    """
    from app import config  # 只碰路径常量，不会拉起重依赖

    root = config.DATA_DIR
    (root / "logs").mkdir(parents=True, exist_ok=True)
    cache = root / "numba_cache"
    cache.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("NUMBA_CACHE_DIR", str(cache))
    os.environ.setdefault("PYTHONUTF8", "1")
    return root


def _attach_file_log(root: Path) -> None:
    """让 uvicorn 的日志同时落盘（窗口关掉后就只剩这份了）。

    uvicorn 的 logger 设了 ``propagate=False``，光配 root logger 收不到，
    得逐个挂上。
    """
    handler = RotatingFileHandler(
        root / "logs" / "runbeat.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def _pick_port() -> int:
    """从 8000 起找一个空闲端口。"""
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


def _wait_and_open(url: str) -> None:
    """轮询健康检查，服务真起来了再开浏览器（否则用户会看到一个白页）。"""
    deadline = time.time() + READY_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1.5) as resp:
                if resp.status == 200:
                    webbrowser.open(url)
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    print(f"[RunBeat] 服务在 {READY_TIMEOUT_SEC:.0f} 秒内没起来，请把上面的报错发出来")


def main() -> int:
    root = _bootstrap_env()
    _attach_file_log(root)

    try:
        port = _pick_port()
    except RuntimeError as exc:
        print(f"[RunBeat] 启动失败：{exc}")
        return 1

    url = f"http://{HOST}:{port}"
    print("=" * 60)
    print(BANNER.format(url=url, root=root))
    print("=" * 60)

    threading.Thread(target=_wait_and_open, args=(url,), daemon=True).start()

    import uvicorn  # noqa: PLC0415 —— 放在环境准备好之后再导入

    from app.main import app  # noqa: PLC0415

    # 传 app 对象而不是 "app.main:app" 字符串：冻结环境里 uvicorn 的
    # 字符串导入会去找源码文件，容易扑空。
    uvicorn.run(app, host=HOST, port=port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
