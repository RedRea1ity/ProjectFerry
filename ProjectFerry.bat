@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pyw.exe >nul 2>&1
if not errorlevel 1 (
    start "ProjectFerry" /b pyw.exe -3 "%~dp0ProjectFerry.pyw"
    exit /b 0
)
where pythonw.exe >nul 2>&1
if not errorlevel 1 (
    start "ProjectFerry" /b pythonw.exe "%~dp0ProjectFerry.pyw"
    exit /b 0
)
for /d %%P in ("%ProgramFiles%\WindowsApps\OpenAI.CodexPrimaryRuntime*_x64__3k8sg7r9htsxt") do if exist "%%P\dependencies\python\pythonw.exe" (
    start "ProjectFerry" /b "%%P\dependencies\python\pythonw.exe" "%~dp0ProjectFerry.pyw"
    exit /b 0
)
echo 未找到桌面运行环境，请安装带 Tk 的 Python 3.11 或更高版本后重试。
pause
exit /b 1
