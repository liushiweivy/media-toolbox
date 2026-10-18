@echo off
rem Media Toolbox desktop edition: native window, no console.
cd /d %~dp0
set HF_ENDPOINT=https://hf-mirror.com
start "" .venv\Scripts\pythonw.exe desktop.py
