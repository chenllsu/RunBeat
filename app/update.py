"""版本更新检查。

启动时在后台查一次 GitHub 上的最新 Release，和本地 ``__version__`` 比一比，
结果缓存下来给前端读。

**走 releases.atom，不走 REST API。** 未认证的 GitHub API 每小时只有 60 次配额，
实测很容易撞上 ``403 rate limit exceeded`` —— 一旦撞上就长期查不到任何东西；
atom feed 是静态资源，不限流、不需要令牌，体积也只有一两 KB。

**所有失败路径一律静默**：用户可能在内网、可能断网、也可能正好被限流 ——
任何一种都不该影响软件本身能不能用。所以这里只做「查到了就提示」，
绝不重试、绝不轮询、绝不弹错。
"""

from __future__ import annotations

import re
import threading
import urllib.request

from . import __version__

REPO = "chenllsu/RunBeat"
ATOM_URL = f"https://github.com/{REPO}/releases.atom"
TAG_URL = f"https://github.com/{REPO}/releases/tag/"
TIMEOUT_SEC = 6.0

_TAG_RE = re.compile(r"/releases/tag/([^\"'<>\s]+)")

_state = {
    "checked": False,    # 是否已经尝试过（成功或失败都算）
    "latest": None,      # 远端最新版本号（已去掉 v 前缀），没查到就是 None
    "has_update": False,
    "url": None,         # 对应 Release 页面
}
_lock = threading.Lock()


def _parse(text: str) -> tuple[int, ...] | None:
    """把 ``"v0.5.0"`` 解析成可比较的元组 ``(0, 5, 0)``。

    解析不出来（格式怪、带奇怪后缀）就返回 ``None``，由调用方当作「比不了」处理。
    """
    if not text:
        return None
    parts = []
    for chunk in text.strip().lstrip("vV").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break          # "0-rc1" 这种取到数字部分就停
        if not digits:
            return None
        parts.append(int(digits))
    return tuple(parts) if parts else None


def _check() -> None:
    try:
        request = urllib.request.Request(
            ATOM_URL,
            headers={
                "Accept": "application/atom+xml",
                "User-Agent": f"RunBeat/{__version__}",
            },
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", "replace")

        # feed 里第一条 entry 就是最新 release；去重是防同一条被链接两次
        tags = list(dict.fromkeys(_TAG_RE.findall(body)))
        if not tags:
            raise ValueError("feed 里没有 release 链接")

        latest = tags[0].strip().lstrip("vV")
        current = _parse(__version__)
        remote = _parse(latest)

        with _lock:
            _state["latest"] = latest or None
            _state["url"] = f"{TAG_URL}{tags[0]}"
            _state["has_update"] = bool(current and remote and remote > current)
    except Exception:
        # 断网 / DNS 失败 / 代理 / 限流 / 解析异常……全部当「没查到」
        pass
    finally:
        with _lock:
            _state["checked"] = True


def check_async() -> None:
    """启动后台检查线程，立即返回。"""
    threading.Thread(target=_check, name="runbeat-update-check", daemon=True).start()


def state() -> dict:
    """当前检查结果（含 ``current``，方便前端直接比对展示）。"""
    with _lock:
        return {"current": __version__, **_state}
