@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python nba_elo.py %*
python nba_jugadores.py %*
python cuotas.py
pause
