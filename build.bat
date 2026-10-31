@echo off
rem Builds DeskBuddy.exe - one file you can copy to any Windows PC and double-click.
cd /d "%~dp0"

if not exist .venv (
    echo Setting up build tools...
    python -m venv .venv || goto :fail
)
.venv\Scripts\python -m pip install -q --disable-pip-version-check pyinstaller pillow pystray qrcode || goto :fail

.venv\Scripts\python -c "import deskbuddy; deskbuddy.robot_image(256, eye_scale=1.5).save('icon.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])" || goto :fail

.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed ^
    --name DeskBuddy --icon "%~dp0icon.ico" ^
    --add-data "%~dp0face.html;." ^
    --hidden-import pystray._win32 ^
    --distpath . --workpath build --specpath build ^
    deskbuddy.py || goto :fail

echo.
echo   Done!  DeskBuddy.exe is ready - copy it to any PC and double-click it.
echo.
pause
exit /b 0

:fail
echo.
echo   Build failed - see the messages above.
pause
exit /b 1
