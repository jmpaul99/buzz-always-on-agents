@echo off
REM One-command deploy. Forwards all args to deploy.ps1 (e.g. -SkipDesktop).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
exit /b %ERRORLEVEL%
