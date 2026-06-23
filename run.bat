@echo off
REM License Resolver - one-click launcher (Windows). Double-click this file.
cd /d "%~dp0"

where py >nul 2>nul && (set PY=py) || (set PY=python)

echo Ensuring dependencies (requests, beautifulsoup4)...
%PY% -m pip install --quiet --disable-pip-version-check requests beautifulsoup4

echo Launching License Resolver...
%PY% license_resolver_app.py
if errorlevel 1 pause
