@echo off
setlocal
cd /d "%~dp0"
py -3.11 tasmodesk.py %*
