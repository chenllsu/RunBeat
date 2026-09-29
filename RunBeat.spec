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
    # 保留控制台窗口：它同时是"运行状态指示"和"退出按钮"（关窗即退出）。
    # 想要无窗口形态，改成 False 并加桌面窗口外壳。
    console=True,
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
