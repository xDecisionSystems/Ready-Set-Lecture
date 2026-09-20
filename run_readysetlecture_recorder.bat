@echo off
cd /d "%~dp0"
"C:\Users\Adan Ernesto Vela\anaconda3\envs\readysetlecture-qtmm\python.exe" -m app.recorder.main
if errorlevel 1 (
    echo.
    echo Ready, Set, Lecture! Recorder exited with an error. Press any key to close this window.
    pause >nul
)
