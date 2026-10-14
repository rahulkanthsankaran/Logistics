@echo off
cd /d "%~dp0"
echo ========================================================
echo Starting Parcel Trail - Courier Tracking System
echo URL: http://127.0.0.1:5000
echo Staff Login: http://127.0.0.1:5000/staff/login (staff / courier123)
echo ========================================================
echo.
.venv\Scripts\python.exe app.py
pause
