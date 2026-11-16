"""pyplayready 兼容垫片（stub）。

本项目的在线音源只走 **Widevine** 解密路径（pywidevine，GPL-3.0），
从不使用 PlayReady 后端。但 gamdl 3.9.1 的 ``interface/base.py`` 在
import 时就无条件加载 pyplayready —— 不装它 gamdl 直接挂掉，
装它又引入 CC BY-NC-ND（禁商用、禁演绎）授权冲突。

所以用这个空壳顶替真实的 pyplayready：

* 提供 gamdl import 时需要的全部符号（类与类型标注），让导入不再报错；
* 任何符号被**真正调用**时立即抛错 —— 意味着有人试图走 PlayReady
  解密路径，这是本项目明确不支持的能力；
* 行为与未安装完全等效，且把错误信息说清楚，而不是甩一个 ImportError。

注意：这个文件**必须**与真实 pyplayready 的模块路径一致
（pyplayready/ 下有 __init__.py、cdm.py 等），Python 才会认。
真实的 pyplayready 装了反而会遮住这个垫片 —— 二选一，不要共存。
"""

__version__ = "0.0.0-stub"

_STUB_MSG = (
    "PlayReady 解密路径在本项目中已被禁用（仅支持 Widevine）。"
    "如果你看到这个错误，说明代码试图走 PlayReady 后端 —— "
    "本项目不支持，也不该走到这里。"
)


class _Stub:
    """所有 PlayReady 类的基座：被实例化/调用即抛错。"""

    def __init__(self, *args, **kwargs):
        raise RuntimeError(_STUB_MSG)

    def __getattr__(self, name):
        # 类方法和静态方法也一样：一碰就报
        def _blocked(*args, **kwargs):
            raise RuntimeError(_STUB_MSG)
        return _blocked


def _make_stub_class(name: str):
    return type(name, (_Stub,), {"__module__": __name__, "__qualname__": name})


# gamdl/interface/base.py 顶部 import 的四个符号
Cdm = _make_stub_class("Cdm")
Device = _make_stub_class("Device")
