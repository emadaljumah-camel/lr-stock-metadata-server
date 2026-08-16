@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
for /f "usebackq tokens=1,2" %%A in (`python -c "from server.config import HOST, PORT; print(HOST, PORT)"`) do (
    set LR_SERVER_HOST=%%A
    set LR_SERVER_PORT=%%B
)
.venv\Scripts\python.exe -m uvicorn server.main:app --host %LR_SERVER_HOST% --port %LR_SERVER_PORT%
