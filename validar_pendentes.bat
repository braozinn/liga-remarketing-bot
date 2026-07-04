@echo off
REM ============================================================
REM  Valida os IDs PENDENTES no QuotexPartnerBot.
REM  Pega os leads que ja tem ID achado mas nao foram validados
REM  (status=extracted) e confirma cada um: pais, deposito, saldo.
REM  GRATIS (sem Vision) e rapido. Depois gera a planilha.
REM  Rode com o bot DESLIGADO (conflito de sessao).
REM ============================================================
cd /d "%~dp0"

echo.
echo ============================================================
echo   Validando IDs pendentes (gratis, sem Vision)
echo   Confirma pais/deposito/saldo no QuotexPartnerBot.
echo   NAO ligue o bot ao mesmo tempo.
echo ============================================================
echo.

.venv\Scripts\python.exe scripts\qualify_and_export.py --validate-pending

echo.
echo Planilha gerada em data\exports\
echo.
pause
