@echo off
REM ============================================================
REM  Recategoriza todos os leads (tags de engajamento).
REM  Corrige tags tipo "Depositou", "Conta sem deposito", etc.
REM  usando os dados que ja estao no banco. RAPIDO e GRATIS.
REM  Nao usa Telegram nem Vision - pode rodar ate com o bot ligado.
REM ============================================================
cd /d "%~dp0"

echo.
echo ============================================================
echo   Recategorizando leads (tags de engajamento)...
echo   Rapido, so banco, sem Telegram/Vision.
echo ============================================================
echo.

.venv\Scripts\python.exe scripts\recategorizar.py

echo.
echo Recategorizacao concluida.
echo.
pause
