"""启动横幅。

单独成一个模块，是为了让 ``run.bat`` 能保持纯 ASCII —— cmd.exe 按系统
代码页逐字节解析批处理文件，UTF-8 中文写在 ``echo`` 里会被解码错乱，
碎片还会被当成命令执行（实测报 ``'Beat' is not recognized``）。
中文放进 Python 打印就没这个问题：解释器按 UTF-8 读源码，控制台编码
由 ``chcp 65001`` + ``PYTHONUTF8=1`` 兜住。

用法：
    python -m app.banner          # 普通模式
    python -m app.banner dev      # 开发模式（带自动重载提示）
"""

from __future__ import annotations

import sys

URL = "http://127.0.0.1:8000"

BANNER = """
  RunBeat{dev} 启动中 ...
  打开 {url}
  按 Ctrl+C 停止
"""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    dev = "dev" in args

    text = BANNER.format(
        dev="（开发模式 · 改代码自动重载）" if dev else "",
        url=URL,
    )

    # 控制台不一定是 UTF-8（父进程没执行 chcp 时就是 GBK），
    # 编不出来就退回 ASCII 版本，别让一句提示把启动流程搞崩。
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(f"\n  RunBeat starting{' (dev)' if dev else ''} ... {URL}\n", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
