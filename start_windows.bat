@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  python -m venv .venv
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py --init-db
echo.
echo Open http://127.0.0.1:5000 in your browser.
echo Keep this window open while using the system.
.venv\Scripts\python.exe app.py
pause
