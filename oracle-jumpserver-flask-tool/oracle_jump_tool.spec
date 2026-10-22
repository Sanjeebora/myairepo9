# PyInstaller spec: builds a single console exe. Run build_exe.bat (or
# `pyinstaller --noconfirm --clean oracle_jump_tool.spec`) on Windows.
from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ["run_tool.py"],
    pathex=["."],
    datas=[
        ("oracle_jump_tool/templates", "oracle_jump_tool/templates"),
        ("oracle_jump_tool/specs", "oracle_jump_tool/specs"),
    ],
    hiddenimports=collect_submodules("waitress") + ["dotenv"],
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="oracle-jump-tool",
    console=True,
    upx=False,
)
