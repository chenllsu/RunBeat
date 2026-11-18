"""RunBeat 应用包。"""

import sys
from pathlib import Path

__version__ = "1.0.0"

# ---------------------------------------------------------------------------
# pyplayready 垫片
#
# 在线音源只走 Widevine（pywidevine，GPL-3.0）。pyplayready 是 CC BY-NC-ND
# （禁商用、禁演绎），留着它项目就无法合规开源 —— 但 gamdl 3.9.1 import 时
# 无条件加载它，不装直接挂。
#
# 解法：app/_vendor_stubs/pyplayready 是个空壳（见其 __init__ 的说明）。
# 这里把垫片目录插到 sys.path 最前，让 Python 找到它而不是真包。
# 若用户环境已装真 pyplayready，也会被垫片遮住 —— 二者不可共存，这是刻意
# 的：README 已写明本项目仅支持 Widevine。
#
# 注意：必须在任何 gamdl/pyplayready import 之前执行 —— 放在 app/__init__
# 正是为了保证这一点（所有业务模块都从 app.* 进来）。
# ---------------------------------------------------------------------------
_VENDOR_STUBS = Path(__file__).resolve().parent / "_vendor_stubs"
if str(_VENDOR_STUBS) not in sys.path:
    sys.path.insert(0, str(_VENDOR_STUBS))
