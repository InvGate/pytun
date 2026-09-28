@echo off
rem Builds dist\pytun.exe from a clean virtualenv.
rem Expects mac_address_pub_key in the current directory (copied by Jenkins).
rem Override the interpreter with: set PYTHON=C:\path\to\python.exe
setlocal

if "%PYTHON%"=="" set "PYTHON=C:\Python310-32\python.exe"

if not exist mac_address_pub_key (
    echo mac_address_pub_key not found
    exit /b 1
)

"%PYTHON%" --version || exit /b 1

if exist venv rmdir /s /q venv
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

"%PYTHON%" -m venv venv || exit /b 1
venv\Scripts\python.exe -m pip install pip==26.2.1 || exit /b 1
venv\Scripts\python.exe -m pip install -r requirements-build.txt || exit /b 1

venv\Scripts\pyinstaller.exe -F pytun.py --log-level=DEBUG --runtime-tmpdir ./tmp --exclude-module urlib3 --hidden-import=ssl --hidden-import=_ssl --uac-admin --icon=invgate.ico --add-data "mac_address_pub_key;." || exit /b 1
