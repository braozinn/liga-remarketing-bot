"""Setup automático - rode UMA VEZ antes de tudo.

   python setup.py

Esse script:
1. Verifica versão do Python
2. Cria o ambiente virtual (.venv)
3. Instala dependências
4. Cria a pasta .vscode com launch.json + settings.json corretos
5. Cria o .env a partir do .env.example
6. Abre o .env no Notepad pra você preencher
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
VSCODE_DIR = ROOT / ".vscode"
SETUP_DIR = ROOT / "vscode-setup"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"


def ok(msg):
    print(f"[OK] {msg}")


def err(msg):
    print(f"[ERRO] {msg}")


def head(msg):
    print()
    print("=" * 60)
    print(f"  {msg}")
    print("=" * 60)


def main() -> int:
    head("SETUP - Bot de Remarketing Telegram")

    # ---------------------------------------------------------------- 1) Python
    py_ver = sys.version_info
    print(f"Python detectado: {py_ver.major}.{py_ver.minor}.{py_ver.micro}")
    if py_ver < (3, 10):
        err("Versão muito antiga. Instale Python 3.10+ em https://python.org")
        return 1
    if py_ver >= (3, 14):
        print("[AVISO] Python 3.14+ é muito novo. Algumas dependências podem dar erro.")
        print("        Recomendado: Python 3.12.")
        print("        Continuar mesmo assim? (s/n): ", end="", flush=True)
        if input().strip().lower() not in ("s", "sim", "y", "yes"):
            print("Cancelado. Instale Python 3.12 em https://python.org/downloads/")
            return 1

    # ---------------------------------------------------------------- 2) venv
    head("Criando ambiente virtual (.venv)")
    venv_python = _venv_python_path()
    if venv_python.exists():
        ok(f".venv já existe em {VENV_DIR}")
    else:
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
        ok(f".venv criado em {VENV_DIR}")

    # ---------------------------------------------------------------- 3) deps
    head("Instalando dependências (1-3 minutos)")
    subprocess.check_call(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "-q"]
    )
    try:
        subprocess.check_call(
            [str(venv_python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]
        )
    except subprocess.CalledProcessError:
        err("Falha ao instalar dependências.")
        print("        Tente Python 3.12 (algumas libs podem não ter wheels pra 3.14).")
        return 1
    ok("Dependências instaladas no .venv")

    # ---------------------------------------------------------------- 4) .vscode
    head("Configurando .vscode")
    VSCODE_DIR.mkdir(exist_ok=True)
    if SETUP_DIR.exists():
        for fname in ("launch.json", "settings.json"):
            src = SETUP_DIR / fname
            if src.exists():
                shutil.copyfile(src, VSCODE_DIR / fname)
                ok(f".vscode/{fname}")
    else:
        # Fallback: cria os arquivos do zero
        _write_vscode_files(VSCODE_DIR)
        ok(".vscode criado (fallback)")

    # ---------------------------------------------------------------- 5) .env
    head("Configurando .env")
    if not ENV_FILE.exists():
        if ENV_EXAMPLE.exists():
            shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
            ok(".env criado a partir do .env.example")
        else:
            err(".env.example não encontrado!")
            return 1
    else:
        ok(".env já existe")

    # ---------------------------------------------------------------- 6) Aviso
    head("PRÓXIMO PASSO")
    print()
    print("  1. Vou abrir o .env no Bloco de Notas.")
    print("     Preencha com:")
    print("       - TELEGRAM_API_ID e TELEGRAM_API_HASH (https://my.telegram.org)")
    print("       - TELEGRAM_PHONE (seu telefone com DDI)")
    print("       - PRIVATE_GROUP (@grupo dos pagos / convertidos)")
    print("       - LEADS_SOURCE_GROUP (@grupo VIP / leads)")
    print("       - ANTHROPIC_API_KEY (se quiser usar modo AI)")
    print()
    print("     SALVE (Ctrl+S) e FECHE o Bloco de Notas.")
    print()
    print("  2. No VS Code, aperte F5 pra rodar o bot.")
    print()
    print("  3. Abra http://127.0.0.1:8080 no navegador.")
    print()
    input("Pressione ENTER pra abrir o .env no Bloco de Notas...")
    try:
        if os.name == "nt":
            os.startfile(str(ENV_FILE))
        else:
            subprocess.call(["xdg-open", str(ENV_FILE)])
    except Exception:
        print(f"Não consegui abrir automaticamente. Abra manualmente: {ENV_FILE}")

    head("SETUP CONCLUÍDO")
    print("Quando terminar de preencher o .env, aperte F5 no VS Code.")
    return 0


def _venv_python_path() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _write_vscode_files(d: Path) -> None:
    launch = """{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Rodar Bot Remarketing",
            "type": "debugpy",
            "request": "launch",
            "program": "${workspaceFolder}/main.py",
            "cwd": "${workspaceFolder}",
            "console": "integratedTerminal",
            "python": "${workspaceFolder}/.venv/Scripts/python.exe",
            "justMyCode": true,
            "envFile": "${workspaceFolder}/.env"
        }
    ]
}
"""
    settings = """{
    "python.defaultInterpreterPath": "${workspaceFolder}/.venv/Scripts/python.exe",
    "python.terminal.activateEnvironment": true
}
"""
    (d / "launch.json").write_text(launch, encoding="utf-8")
    (d / "settings.json").write_text(settings, encoding="utf-8")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(1)
