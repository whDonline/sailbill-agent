# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

构建（命令由使用者自行执行）：
    uv add --dev pyinstaller
    uv run pyinstaller sailbill.spec
产物：dist/sailbill.exe（单文件，双击启动桌面版，自动打开浏览器）
"""
from PyInstaller.utils.hooks import collect_all

# Web 静态页随包分发，运行时路径与 app.py 中 Path(__file__).parent/static 对应
datas = [("src/sailbill/server/static", "sailbill/server/static")]
binaries = []
hiddenimports = []
# langgraph/langchain 存在大量动态导入，必须整体收集
for _pkg in ("langgraph", "langchain_core", "langchain_openai", "pydantic", "uvicorn"):
    _res = collect_all(_pkg)
    if _res:
        _d, _b, _h = _res
        datas += _d
        binaries += _b
        hiddenimports += _h

a = Analysis(
    ["run_desktop.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="sailbill",
    console=True,
    disable_windowed_traceback=False,
)
