@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Nexus Distributed Collaboration

if not exist ".venv\Scripts\python.exe" (
    echo Створення локального Python-середовища...
    python -m venv .venv
    if errorlevel 1 goto :error
    echo Встановлення залежностей...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :error
)

echo Запуск двох вузлів Nexus...
".venv\Scripts\python.exe" run_demo.py
goto :end

:error
echo.
echo Не вдалося запустити Nexus. Перевірте, чи встановлено Python 3.11 або новіший.
pause

:end
