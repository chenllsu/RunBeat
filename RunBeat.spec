# -*- mode: python ; coding: utf-8 -*-
"""RunBeat 打包配置（PyInstaller）。

用法（在项目根目录）：

    .venv\\Scripts\\python.exe -m PyInstaller --clean --noconfirm RunBeat.spec

产出 ``dist/RunBeat/``：整个文件夹拷到别的 Windows 机器上就能跑，无需装 Python。
"""

a = Analysis(
    ["runbeat_desktop.py"],
    pathex=[],
    binaries=[],
    # 只读资源必须显式收集；DATA_DIR 由 config.py 算到 exe 同级，不在这里
    datas=[("static", "static")],
    hiddenimports=[
        # uvicorn 靠字符串动态导入这些模块，静态分析看不出来
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "h11",
        # 原生窗口。runbeat_desktop 里是在函数内 import 的，显式列出来更稳当；
        # hooks-contrib 的 hook-webview / hook-clr / hook-clr_loader 会把
        # WebView2 的那几个 DLL 和 Python.Runtime.dll 一并收进来。
        "webview",
        "webview.platforms.winforms",
        "clr",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # librosa 只在 decompose.py 的函数体里 import sklearn，静态分析会当成依赖
        # 收进来，但全程用不到 —— 实测排除后检测结果逐位一致，省 45MB
        "sklearn",
        "tkinter",
        "matplotlib",
        "IPython",
        "pytest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RunBeat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 有原生窗口了，不再需要那个黑色控制台 —— 用户看到的就是一个正常应用窗口。
    # 出错时启动器会弹系统消息框并指出日志位置（见 runbeat_desktop._alert）。
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RunBeat",
)
