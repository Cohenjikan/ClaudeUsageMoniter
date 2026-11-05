# Local Windows build. Produces dist\ClaudeUsageMonitor\ with both executables
# (the same thing CI attaches to GitHub Releases as a zip).
$ErrorActionPreference = "Stop"
python -m pip install -r requirements.txt pyinstaller==6.11.1
python -m unittest discover -s tests
pyinstaller --noconfirm ClaudeUsageMonitor.spec
Write-Host "Built: dist\ClaudeUsageMonitor\ClaudeUsageMonitor.exe"
