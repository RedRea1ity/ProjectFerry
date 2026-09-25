@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "ProjectFerry" /b pyw "%~dp0ProjectFerry.pyw"
exit /b 0
