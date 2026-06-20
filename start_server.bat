@echo off
echo Starting BML API (Python/FastAPI + uvicorn) at http://0.0.0.0:8000
echo Docs: http://127.0.0.1:8000/docs
echo Press Ctrl+C to stop.
echo.
"%~dp0venv\Scripts\python.exe" "%~dp0server.py"
