@echo off
rem Starts Tally Cloud on this PC: the database, the app, and the browser.
rem Double-click it. Close this window (or press Ctrl+C) to stop the app; it can be started again any time.
rem The database runs in its own minimised window "Tally Cloud database"; close that one to stop the database.
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------- settings
rem PostgreSQL programs on this PC, and the app's own database folder and port.
set "PG_BIN=D:\Software Develoment\Postgressql\bin"
set "PGDATA=%~dp0localdb\pgdata"
set "PGPORT=55432"
rem Web address of the app: http://127.0.0.1:%APP_PORT%
set "APP_PORT=8000"
rem true = sync with Tally by itself while Tally is open; false = only when you click Sync now.
set "ERP_AUTO_SYNC=false"

rem ---------------------------------------------------------------- database
if not exist "%PGDATA%\PG_VERSION" (
  echo The database folder %PGDATA% is missing.
  pause & exit /b 1
)
"%PG_BIN%\pg_isready.exe" -h localhost -p %PGPORT% -U erp -d postgres >nul 2>&1
if not errorlevel 1 goto dbready
echo Starting the database in its own window "Tally Cloud database" - leave that window open.
if exist "%PGDATA%\postmaster.pid" del "%PGDATA%\postmaster.pid"
rem Its own window: a database started from this window would stop working once this window is closed.
start "Tally Cloud database" /min cmd /c ""%PG_BIN%\postgres.exe" -D "%PGDATA%" -p %PGPORT% 2>>"%~dp0localdb\postgres.log""
set /a WAITED=0
:waitdb
ping -n 3 127.0.0.1 >nul
"%PG_BIN%\pg_isready.exe" -h localhost -p %PGPORT% -U erp -d postgres >nul 2>&1
if not errorlevel 1 goto dbready
set /a WAITED+=2
if %WAITED% LSS 120 goto waitdb
echo The database did not start. See localdb\postgres.log
pause
exit /b 1
:dbready

rem ---------------------------------------------------------------- Python environment (made once)
if not exist ".venv\Scripts\python.exe" (
  echo First start: preparing Python packages, this takes a minute...
  set "BASEPY="
  for %%P in ("%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "C:\Program Files\Python312\python.exe" "C:\Program Files\Python311\python.exe") do (
    if not defined BASEPY if exist %%P set "BASEPY=%%~P"
  )
  if not defined BASEPY set "BASEPY=python"
  call "%%BASEPY%%" -m venv .venv || (echo Could not make the Python environment. & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -r backend\requirements.txt || (echo Installing Python packages failed. & pause & exit /b 1)
)

rem ---------------------------------------------------------------- app
set "ERP_DATABASE_URL=postgresql+psycopg://erp@localhost:%PGPORT%/tally_erp"
cd backend
"..\.venv\Scripts\python.exe" -m app.cli init-db >nul || (echo Updating the database failed. & pause & exit /b 1)

echo.
echo Tally Cloud is running at http://127.0.0.1:%APP_PORT%
echo Close this window to stop it.
echo.
rem open the browser once the app has had a few seconds to start
start "" /min cmd /c "ping -n 6 127.0.0.1 >nul & start http://127.0.0.1:%APP_PORT%"
"..\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %APP_PORT% --workers 1
