@echo off
cd /d %~dp0
call .venv\Scripts\activate.bat
set HF_ENDPOINT=https://hf-mirror.com
python -m server.app
