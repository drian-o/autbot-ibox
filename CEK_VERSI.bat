@echo off
cd /d "%~dp0"
echo Memeriksa server StockPilot baru pada http://127.0.0.1:8777...
powershell -NoProfile -Command "try { $s=Invoke-RestMethod -Uri 'http://127.0.0.1:8777/api/state' -TimeoutSec 5; Write-Host ('Versi: '+$s.version+' | Mode: '+$s.mode); if ($s.version -eq '2.1.0' -and $s.mode -eq 'live') {Write-Host 'OK: StockPilot iBox LIVE terbaru berjalan.'} else {Write-Host 'SALAH VERSI: cek folder tempat kamu menjalankan aplikasi.'} } catch {Write-Host 'Server baru belum berjalan. Buka MULAI_IBOX_LIVE.bat dulu.'}"
pause
