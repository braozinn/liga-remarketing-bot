@echo off
REM ============================================================
REM  Bot de Remarketing - Telegram Userbot
REM  Inicia o painel web + userbot
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Detecta Python: tenta "py -3" primeiro (launcher oficial Windows),
REM depois "python", depois "python3"
set "PY="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    where python3 >nul 2>nul
    if not errorlevel 1 set "PY=python3"
)

if not defined PY (
    echo.
    echo ============================================================
    echo  ERRO: Python nao encontrado.
    echo ============================================================
    echo.
    echo  1. Baixe Python 3.11+ em: https://www.python.org/downloads/
    echo  2. Na instalacao, MARQUE "Add python.exe to PATH" no rodape.
    echo  3. Reinicie o computador.
    echo  4. Rode esse start.bat de novo.
    echo.
    pause
    exit /b 1
)

echo Python encontrado: %PY%

REM Cria venv se nao existe
if not exist ".venv\Scripts\python.exe" (
    echo Criando ambiente virtual Python...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo ERRO ao criar venv. Verifique sua instalacao do Python.
        pause
        exit /b 1
    )
    echo Instalando dependencias (pode demorar 1-3 minutos)...
    call ".venv\Scripts\activate.bat"
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo ERRO ao instalar dependencias.
        pause
        exit /b 1
    )
) else (
    call ".venv\Scripts\activate.bat"
)

if not exist ".env" (
    echo.
    echo ATENCAO: Arquivo .env nao encontrado.
    echo Copiando .env.example -^> .env
    copy .env.example .env >nul
    echo.
    echo Vou abrir o .env no Bloco de Notas. Preencha com seus dados,
    echo SALVE (Ctrl+S) e FECHE o Bloco de Notas pra continuar.
    notepad .env
)

echo.
echo ============================================================
echo  Iniciando bot... Painel: http://127.0.0.1:8080
echo  Pressione CTRL+C pra parar.
echo ============================================================
echo.
python main.py

pause
