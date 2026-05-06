"""Diagnóstico - lista todos os grupos da conta + testa o PRIVATE_GROUP atual.

Roda assim (com o bot PARADO):
    .\.venv\Scripts\python.exe diagnose.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from userbot.client import get_client  # noqa: E402


def _format_id_options(entity_id: int, entity_type: str) -> str:
    """Mostra todos os formatos válidos pra esse ID."""
    options = []
    if entity_type in ("Channel", "Megagroup", "Supergroup"):
        options.append(f"-100{entity_id}")
    options.append(str(entity_id))
    options.append(f"-{entity_id}")
    return " / ".join(options)


async def main():
    print("=" * 70)
    print("  DIAGNÓSTICO - Bot de Remarketing Telegram")
    print("=" * 70)

    client = await get_client()
    if not await client.is_user_authorized():
        print("[!] Conta não logada. Rode `python main.py` primeiro pra fazer login.")
        return

    me = await client.get_me()
    print(f"\n✓ Logado como: {me.first_name} (@{me.username or '-'}) id={me.id}")

    # ------------------------------------------------------------ Lista TODOS os grupos
    print("\n" + "=" * 70)
    print("  TODOS OS GRUPOS DA SUA CONTA (incluindo arquivados)")
    print("=" * 70)
    print(f"\n{'#':<4}{'Tipo':<14}{'Membros':<10}{'ID pro .env':<25}{'Nome'}")
    print("-" * 70)

    all_groups = []
    idx = 0
    try:
        async for dialog in client.iter_dialogs(archived=None):  # None = todos
            if not (dialog.is_group or dialog.is_channel):
                continue
            entity = dialog.entity
            etype = type(entity).__name__
            members = getattr(entity, "participants_count", None)
            if members is None:
                members = "?"
            # Determina o ID a usar no .env
            if etype in ("Channel",) or getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False):
                env_id = f"-100{entity.id}"
            else:
                env_id = f"-{entity.id}"

            archived = "📦" if getattr(dialog, "archived", False) else "  "
            all_groups.append((idx, entity, env_id, members, dialog.name))
            big_marker = "  "
            try:
                if isinstance(members, int) and members >= 500:
                    big_marker = "⭐"
            except Exception:
                pass
            print(f"{idx:<4}{etype:<14}{str(members):<10}{env_id:<25}{archived}{big_marker} {dialog.name}")
            idx += 1
    except Exception as e:
        print(f"[erro ao listar dialogs] {e}")

    if not all_groups:
        print("\n[!] Sua conta não está em nenhum grupo? Verifique.")
        await client.disconnect()
        return

    # ------------------------------------------------------------ Testa PRIVATE_GROUP atual
    print("\n" + "=" * 70)
    print("  TESTANDO O PRIVATE_GROUP DO SEU .env")
    print("=" * 70)
    target = os.getenv("PRIVATE_GROUP", "").strip()
    print(f"\nValor atual no .env: '{target}'")

    if not target:
        print("[!] Vazio. Use o ID de um dos grupos da lista acima.")
        await client.disconnect()
        return

    try:
        if target.lstrip("-").isdigit():
            entity = await client.get_entity(int(target))
        else:
            entity = await client.get_entity(target)
        print(f"✓ Resolveu pra: '{getattr(entity, 'title', '?')}' (id real: {entity.id}, tipo: {type(entity).__name__})")
    except Exception as e:
        print(f"[!] FALHOU ao resolver: {e}")
        print("\n→ O ID '%s' não é válido." % target)
        print("  Use um dos IDs da coluna 'ID pro .env' da lista acima.")
        await client.disconnect()
        return

    # Conta membros — modo aggressive
    print("\nContando membros (aggressive=True, demora ~30s pra grupo grande)...")
    count = 0
    try:
        async for _ in client.iter_participants(entity, aggressive=True):
            count += 1
            if count % 200 == 0:
                print(f"   ...{count}")
        print(f"\n→ Encontrou {count} membros.")
    except Exception as e:
        print(f"[!] Erro contando: {e}")

    # Permissões
    try:
        perms = await client.get_permissions(entity, me)
        print(f"\nVocê é admin? {perms.is_admin}  /  Criador? {perms.is_creator}")
    except Exception as e:
        print(f"\n[!] Erro permissões: {e}")

    # ------------------------------------------------------------ Conclusão
    print("\n" + "=" * 70)
    print("  CONCLUSÃO")
    print("=" * 70)

    server_count = getattr(entity, "participants_count", None)
    if isinstance(server_count, int) and isinstance(count, int):
        if count >= server_count - 5:
            print(f"\n✓ TUDO CERTO. Bot vê {count}/{server_count} membros.")
            print("  Pode rodar o bot e clicar em 'Sincronizar do Telegram' que")
            print("  os ~%d vão ficar EXCLUDED automaticamente." % count)
        elif count < server_count * 0.5:
            print(f"\n[!] Bot só vê {count} de {server_count} membros do servidor.")
            print("    Mesmo sendo admin, o aggressive=True não pegou todos.")
            print("    Possível bug do Telethon ou rate limiting do Telegram.")
            print("    Tente rodar de novo daqui a alguns minutos.")
        else:
            print(f"\n[~] Bot vê {count} de {server_count} membros.")
    else:
        print(f"\n→ Bot conseguiu listar {count} membros.")

    print("\nSe o número parece OK, está tudo certo. Inicie o bot com:")
    print("    .\\.venv\\Scripts\\python.exe main.py")
    print()
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
