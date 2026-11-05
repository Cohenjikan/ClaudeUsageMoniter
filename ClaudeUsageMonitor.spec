# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build: one folder holding TWO executables that share one runtime.
#
#   ClaudeUsageMonitor.exe   windowed tray app (no console)
#   cc-usage-statusline.exe  console helper Claude Code runs as its statusLine
#
# One-folder (not one-file) on purpose: Claude Code launches the statusLine
# helper on every status refresh, and a one-file exe would re-extract itself to
# a temp dir each time. Build with:   pyinstaller ClaudeUsageMonitor.spec

app_a = Analysis(
    ["app.py"],
    hiddenimports=["pystray._win32"],
    excludes=["pytest"],
)
app_exe = EXE(
    PYZ(app_a.pure),
    app_a.scripts,
    [],
    exclude_binaries=True,
    name="ClaudeUsageMonitor",
    console=False,
    upx=False,
)

bridge_a = Analysis(
    ["statusline_bridge.py"],
    excludes=["tkinter", "PIL", "pystray", "winotify", "pytest"],
)
bridge_exe = EXE(
    PYZ(bridge_a.pure),
    bridge_a.scripts,
    [],
    exclude_binaries=True,
    name="cc-usage-statusline",
    console=True,
    upx=False,
)

coll = COLLECT(
    app_exe, app_a.binaries, app_a.datas,
    bridge_exe, bridge_a.binaries, bridge_a.datas,
    name="ClaudeUsageMonitor",
    upx=False,
)
