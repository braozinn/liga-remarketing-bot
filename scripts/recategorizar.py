#!/usr/bin/env python3
"""Recategoriza TODOS os leads (tags de engajamento) — rápido, só banco.

Re-roda a lógica de classificação (deposited / account_no_deposit / etc.)
usando os dados que já estão no banco (depósito, saldo, conta). NÃO conecta
no Telegram nem usa Vision — pode rodar até com o bot ligado.

USO:
    .venv\\Scripts\\python.exe scripts\\recategorizar.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")


async def main():
    from userbot.categorizer import recategorize_all_leads
    print("Recategorizando todos os leads (só banco, sem Telegram)...")
    res = await recategorize_all_leads(scan_messages=False)
    print("Resultado:", res)


if __name__ == "__main__":
    asyncio.run(main())
