@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python nba_elo.py %*
pause
