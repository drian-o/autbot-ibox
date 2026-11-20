@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Environment belum terpasang. Menjalankan install_windows.bat...
    call install_windows.bat --no-pause
    if errorlevel 1 goto :setup_failed
)

".venv\Scripts\python.exe" -c "import playwright" >nul 2>nul
if errorlevel 1 (
    echo Dependency belum lengkap. Menjalankan install_windows.bat...
    call install_windows.bat --no-pause
    if errorlevel 1 goto :setup_failed
)

echo.
echo Menjalankan StockPilot iBox dengan data situs asli.
echo Dashboard LIVE: http://127.0.0.1:8777
echo Port 8777 berbeda dari port demo lama 8765.
echo.
".venv\Scripts\python.exe" app.py --port 8777 %*
set "SP_EXIT=%ERRORLEVEL%"
echo.
if not "%SP_EXIT%"=="0" echo Aplikasi berhenti dengan kesalahan. Baca pesan di atas.
pause
exit /b %SP_EXIT%

:setup_failed
echo Pemasangan belum selesai. Perbaiki pesan kesalahan di atas lalu coba lagi.
pause
exit /b 1
