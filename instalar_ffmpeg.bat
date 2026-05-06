@echo off
REM ============================================================
REM  Instala FFMPEG na pasta do projeto
REM  Duplo-clique aqui para instalar
REM ============================================================

cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar_ffmpeg.ps1"

pause
