@echo off
REM Source-tree helper. Desktop PATH discovery is buzz-backend-*; do not put this
REM folder (or buzz-backend-cloud.py) on PATH or Run on shows cloud.py.
REM Use windows\install-path.ps1 — it installs buzz-backend-cloud.exe (Desktop
REM stages PATH plugins as provider.exe; a .cmd copy cannot run).
python "%~dp0buzz-backend-cloud.py" %*
