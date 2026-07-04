@echo off
REM ============================================================
REM  Separa VIP (grupo privado) dos leads normais.
REM  Deep sync BIDIRECIONAL: busca cada lead por nome no grupo.
REM   - Achou  -> marca VIP (in_private_group=True)
REM   - Sumiu  -> tira o VIP (in_private_group=False)
REM  GRATIS (so busca do Telegram, sem Vision). DEMORA ~2h.
REM  Rode com o bot DESLIGADO (conflito de sessao).
REM ============================================================
cd /d "%~dp0"

echo.
echo ============================================================
echo   Separando VIP (grupo) dos leads normais - deep sync
echo   Busca cada lead por nome no grupo. GRATIS. Demora ~2h.
echo   NAO ligue o bot ao mesmo tempo.
echo ============================================================
echo.

.venv\Scripts\python.exe scripts\qualify_and_export.py --only-deep-sync

echo.
echo Separacao concluida. Veja a dash filtrando por VIP.
echo.
pause
