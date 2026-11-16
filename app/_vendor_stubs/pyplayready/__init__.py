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

覆盖的符号清单来自 gamdl 3.9.1 interface/base.py 的 import 语句：
    from pyplayready.cdm import Cdm
    from pyplayready.device import Device
    from pyplayready.system.pssh import PSSH
    from pyplayready.system.wrmheader import WRMHeader

注意：这个目录**必须**与真实 pyplayready 的包结构同名（pyplayready/...），
Python 才会认。真实的 pyplayready 装了会遮住这个垫片 —— 二选一，不要共存。
"""

__version__ = "0.0.0-stub"

_STUB_MSG = (
    "PlayReady 解密路径在本项目中已被禁用（仅支持 Widevine）。"
    "如果你看到这个错误，说明代码试图走 PlayReady 后端 —— "
    "本项目不支持，也不该走到这里。"
)


class _StubBase:
    """所有 PlayReady 类的基座：实例化即抛错。"""

    def __init__(self, *args, **kwargs):
        raise RuntimeError(_STUB_MSG)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)


def _stub_class(name: str):
    """造一个名字正确的空壳类；任何类方法/静态方法被调用都会抛错。"""

    class _Meta(type):
        def __getattr__(cls, item):
            def _blocked(*args, **kwargs):
                raise RuntimeError(_STUB_MSG)
            return _blocked

    return _Meta(name, (_StubBase,), {"__module__": __name__, "__qualname__": name})


# gamdl interface/base.py import 到的四个类
Cdm = _stub_class("Cdm")
Device = _stub_class("Device")

# pyplayready.crypto 下的符号（gamdl 主 import 不直接用，
# 但有些版本路径会触碰，兜底给全）
ECM_Key = _stub_class("ECM_Key")
ECC_Key = _stub_class("ECC_Key")
ElGamal = _stub_class("ElGamal")
CipherContext = _stub_class("CipherContext")
