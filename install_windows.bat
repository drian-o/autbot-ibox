@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "SP_PYTHON="
set "SP_PY_ARGS="
set "SP_EXIT=1"

echo.
echo StockPilot iBox - pemasangan Python environment dan browser

echo.
if exist ".venv\Scripts\python.exe" (
    call :try_python "%CD%\.venv\Scripts\python.exe"
    if not defined SP_PYTHON (
        echo Environment .venv tidak dapat dipakai atau versi Python kurang dari 3.10.
        echo Tutup aplikasi, ubah nama folder .venv, lalu jalankan pemasangan lagi.
        goto :finish
    )
    goto :install_packages
)

for /f "delims=" %%P in ('where python 2^>nul') do call :try_python "%%P"
if defined SP_PYTHON goto :create_environment

for %%V in (314 313 312 311 310) do call :try_python "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
if defined SP_PYTHON goto :create_environment
for %%V in (314 313 312 311 310) do call :try_python "%ProgramFiles%\Python%%V\python.exe"
if defined SP_PYTHON goto :create_environment
for %%V in (3.14 3.13 3.12 3.11 3.10) do call :try_python "%ProgramFiles%\Python %%V\python.exe"
if defined SP_PYTHON goto :create_environment

where py >nul 2>nul
if errorlevel 1 goto :missing_python
py -3 -c "import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] < (4,0) else 1)" >nul 2>nul
if errorlevel 1 goto :missing_python
set "SP_PYTHON=py"
set "SP_PY_ARGS=-3"

:create_environment
echo [1/3] Membuat environment Python lokal...
"%SP_PYTHON%" %SP_PY_ARGS% -m venv .venv
if errorlevel 1 (
    echo GAGAL membuat .venv. Pastikan folder ini dapat ditulis dan Python memiliki modul venv.
    goto :finish
)

:install_packages
echo [2/3] Memasang dependency Python...
".venv\Scripts\python.exe" -m ensurepip --upgrade
if errorlevel 1 (
    echo GAGAL menyiapkan pip. Periksa instalasi Python pada komputer ini.
    goto :finish
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo GAGAL memasang dependency. Periksa koneksi internet dan pesan pip di atas.
    goto :finish
)

echo [3/3] Memeriksa Microsoft Edge / Google Chrome yang sudah terpasang...
".venv\Scripts\python.exe" browser_detection.py
if not errorlevel 1 goto :browser_ready

echo Edge/Chrome tidak terdeteksi. Mengunduh Chromium Playwright...
echo Pengunduhan dan ekstraksi memerlukan beberapa ratus MB ruang kosong.
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo GAGAL memasang browser. Jika muncul ENOSPC, ruang disk C: atau D: tidak cukup.
    echo Kosongkan ruang disk, atau instal Microsoft Edge / Chrome lalu ulangi.
    goto :finish
)
:browser_ready

".venv\Scripts\python.exe" -c "import playwright; import sqlite3; print('Python, Playwright, dan SQLite siap.')"
if errorlevel 1 goto :finish
set "SP_EXIT=0"
echo.
echo Selesai. Buka start_windows.bat untuk menjalankan StockPilot iBox.
echo Tidak perlu mengubah config.json untuk membaca halaman produk iBox.
goto :finish

:missing_python
echo Python 3.10 atau lebih baru belum ditemukan.
echo Jika Python 3.13 sudah dipasang, tutup terminal lalu buka ulang file ini.
echo Anda juga dapat menjalankan perintah berikut di Command Prompt:
echo winget install --exact --id Python.Python.3.13 --source winget --scope user
echo Setelah instalasi selesai, jalankan install_windows.bat lagi.
goto :finish

:try_python
if defined SP_PYTHON exit /b 0
if not exist "%~1" exit /b 0
"%~1" -c "import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] < (4,0) else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
set "SP_PYTHON=%~1"
set "SP_PY_ARGS="
exit /b 0

:finish
echo.
if /i not "%~1"=="--no-pause" pause
exit /b %SP_EXIT%
