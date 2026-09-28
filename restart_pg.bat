@echo off
echo ============================================
echo  PostgreSQL service fix (run as admin)
echo ============================================
echo.

echo [1/3] Kill residual postgres processes...
taskkill /F /IM postgres.exe /T 2>nul
timeout /t 3 /nobreak >nul

echo [2/3] Start PostgreSQL service postgresql-x64-18...
net start postgresql-x64-18

echo [3/3] Check service state...
sc query postgresql-x64-18 | findstr /C:"STATE" /C:"RUNNING" /C:"STOPPED"

echo.
echo Done. If STATE shows RUNNING above, the database is back.
echo If it still fails, send me a screenshot of this window.
echo.
pause
