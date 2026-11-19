@echo off
REM One-click start: creates the virtual env on first run, then launches the dashboard with the simulator.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  python -m venv .venv || goto :error
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt || goto :error
)
start "" http://localhost:8000
.venv\Scripts\python -m gate serve --simulate %*
goto :eof
:error
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
pause
