@echo off
setlocal
cd /d "%~dp0"
py -3.11 -m PyInstaller --noconfirm --clean --onefile --windowed --name TasmoDesk --exclude-module tkinter --exclude-module webview --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets --exclude-module PySide6.QtWebEngineQuick tasmodesk.py
if errorlevel 1 exit /b %errorlevel%
echo.
echo Ejecutable creado: dist\TasmoDesk.exe
