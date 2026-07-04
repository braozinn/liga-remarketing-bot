@echo off
REM ============================================================
REM  Gera a planilha COMPLETA de leads (tudo num run so).
REM  1) Deep sync bidirecional do grupo (corrige VIP)
REM  2) Le prints (Vision) + valida CADA ID no QuotexPartnerBot
REM     (pais, deposito, saldo) — SEM limite de validacoes
REM  3) Categoriza + gera planilha
REM  DEMORA VARIAS HORAS. Rode com o bot DESLIGADO.
REM ============================================================
cd /d "%~dp0"

echo.
echo ============================================================
echo   Gerando planilha COMPLETA (deep sync + valida TODOS os IDs)
echo   Deep sync do grupo + confirma cada ID no QuotexPartnerBot.
echo   DEMORA VARIAS HORAS. Deixa rodando. NAO ligue o bot junto.
echo ============================================================
echo.

.venv\Scripts\python.exe scripts\qualify_and_export.py

echo.
echo Planilha gerada em data\exports\
echo.
pause
