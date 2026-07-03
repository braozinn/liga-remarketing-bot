@echo off
REM ============================================================
REM  Gera a planilha COMPLETA de leads (deep sync + Vision)
REM  Deep sync do grupo + le prints + valida IDs + categoriza.
REM  DEMORA algumas horas. Rode com o bot DESLIGADO.
REM ============================================================
cd /d "%~dp0"

echo.
echo ============================================================
echo   Gerando planilha COMPLETA (deep sync + Vision)
echo   Isso demora algumas horas. Deixa rodando.
echo   NAO ligue o bot ao mesmo tempo (conflito de sessao).
echo ============================================================
echo.

.venv\Scripts\python.exe scripts\qualify_and_export.py

echo.
echo Planilha gerada em data\exports\
echo.
pause
