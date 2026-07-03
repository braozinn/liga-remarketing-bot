@echo off
REM ============================================================
REM  Liga o bot de remarketing (roda no seu PC)
REM  Duplo-clique neste arquivo pra iniciar.
REM ============================================================
cd /d "%~dp0"

echo.
echo ============================================================
echo   Ligando o bot de remarketing...
echo   (Ctrl+C nesta janela para desligar)
echo ============================================================
echo.

REM Confere se o .env existe e nao esta vazio
for %%A in (".env") do set ENVSIZE=%%~zA
if not exist ".env" (
    echo [ERRO] Arquivo .env nao encontrado. Preencha as credenciais primeiro.
    pause
    exit /b 1
)
if "%ENVSIZE%"=="0" (
    echo [ERRO] O .env esta VAZIO. Preencha as credenciais antes de ligar.
    pause
    exit /b 1
)

.venv\Scripts\python.exe main.py

echo.
echo Bot encerrado.
pause
