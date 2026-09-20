@echo off
cd /d "%~dp0"
"C:\Users\Adan Ernesto Vela\anaconda3\envs\readysetlecture-qtmm\python.exe" -m app.main
if errorlevel 1 (
    echo.
    echo Ready, Set, Lecture! exited with an error. Press any key to close this window.
    pause >nul
)
