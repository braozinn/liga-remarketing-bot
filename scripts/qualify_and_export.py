#!/usr/bin/env python3
"""Qualificação + exportação de leads em UMA passada.

Faz, na ordem:
  1. (opcional) Deep sync BIDIRECIONAL do grupo VIP:
     - busca cada lead por nome no grupo (fura o limite de 200)
     - ACHOU  → marca in_private_group=True
     - SUMIU  → tira in_private_group de quem estava marcado mas saiu
  2. Qualificação de cada lead:
     - pega TODOS os IDs achados na conversa (texto + prints) → multi-conta
     - valida no @QuotexPartnerBot (país, depósito, saldo, transações)
     - conta ativa = o ID mais recente que validar (heurística)
     - quem mandou print mas não deu pra ler o ID → liga_id_status='needs_review'
  3. Categoriza: Lead | Registro | VIP | Ativo
  4. Gera CSV (abre no Excel) ordenado por depósito (maior → menor)

USO (no servidor, com o BOT PARADO pra não brigar pela sessão Telethon):
    cd /opt/telegram-bot-remarketing
    .venv/bin/python scripts/qualify_and_export.py [flags]

FLAGS:
    --skip-deep-sync   não verifica o grupo (usa as flags in_private_group atuais)
    --skip-vision      não lê prints (só texto + dados já no banco) — RÁPIDO e GRÁTIS
    --only-deep-sync   só corrige o VIP, não qualifica nem gera CSV
    --limit N          processa só N leads (pra testar)
    --min-deposit X    valor (USD) que conta como "pagou" (default 1)

DICA pra apresentar HOJE:
    # 1) planilha rápida agora (1 min, grátis) — usa só o que já está no banco:
    .venv/bin/python scripts/qualify_and_export.py --skip-deep-sync --skip-vision
    # 2) depois o scan completo (algumas horas) atualiza tudo:
    .venv/bin/python scripts/qualify_and_export.py
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func  # noqa: E402
from db import SessionLocal  # noqa: E402
from db.models import Lead, LeadMessage, AIUsage  # noqa: E402

# Config
ATIVO_FEEDBACK_DAYS = int(os.getenv("ATIVO_FEEDBACK_DAYS", "30"))
ATIVO_MIN_IMAGES = int(os.getenv("ATIVO_MIN_IMAGES", "1"))
PARTNER_DELAY = float(os.getenv("PARTNER_QUERY_DELAY", "1.5"))  # entre queries ao partner bot
_DEP_COUNT_RE = re.compile(r"Deposits?\s*Count[:\s]+(\d+)", re.I)


def _ai_cost_total() -> float:
    """Soma o custo IA acumulado (pra medir o delta do scan)."""
    with SessionLocal() as s:
        return float(s.query(func.coalesce(func.sum(AIUsage.cost_usd), 0.0)).scalar() or 0.0)


def _count_recent_feedback_images(lead_id: int) -> int:
    cutoff = datetime.utcnow() - timedelta(days=ATIVO_FEEDBACK_DAYS)
    with SessionLocal() as s:
        return int(
            s.query(func.count(LeadMessage.id))
            .filter(LeadMessage.lead_id == lead_id)
            .filter(LeadMessage.direction == "in")
            .filter(LeadMessage.kind == "image")
            .filter(LeadMessage.created_at >= cutoff)
            .scalar() or 0
        )


def _has_any_image(lead_id: int) -> bool:
    with SessionLocal() as s:
        return bool(
            s.query(LeadMessage.id)
            .filter(LeadMessage.lead_id == lead_id)
            .filter(LeadMessage.direction == "in")
            .filter(LeadMessage.kind == "image")
            .first()
        )


async def _deep_sync_bidirectional(client, group_entity, limit=None):
    """Corrige in_private_group nos DOIS sentidos."""
    from userbot.vip_group_deep_sync import deep_check_lead_in_group

    with SessionLocal() as s:
        q = s.query(Lead).filter(Lead.telegram_id.isnot(None))
        if limit:
            q = q.limit(limit)
        leads = [
            {"id": l.id, "telegram_id": l.telegram_id, "username": l.username,
             "first_name": l.first_name, "last_name": l.last_name,
             "in_private_group": bool(l.in_private_group),
             "display": l.display_name}
            for l in q.all()
        ]

    total = len(leads)
    entered = left = unchanged = errors = 0
    print(f"[deep-sync] verificando {total} leads no grupo (bidirecional)...")

    class _L:
        pass

    for i, ld in enumerate(leads, 1):
        try:
            lobj = _L()
            lobj.username = ld["username"]
            lobj.first_name = ld["first_name"]
            lobj.last_name = ld["last_name"]
            lobj.telegram_id = ld["telegram_id"]
            is_in, _method = await deep_check_lead_in_group(client, group_entity, lobj)

            was_in = ld["in_private_group"]
            if is_in and not was_in:
                with SessionLocal() as s:
                    fresh = s.query(Lead).get(ld["id"])
                    if fresh:
                        fresh.in_private_group = True
                        s.commit()
                entered += 1
            elif (not is_in) and was_in:
                with SessionLocal() as s:
                    fresh = s.query(Lead).get(ld["id"])
                    if fresh:
                        fresh.in_private_group = False
                        s.commit()
                left += 1
            else:
                unchanged += 1
        except Exception as e:
            errors += 1
            print(f"[deep-sync] erro lead={ld['display']}: {e}")
        if i % 25 == 0:
            print(f"[deep-sync] {i}/{total} · entraram={entered} saíram={left} erros={errors}")
        await asyncio.sleep(0.5)

    print(f"[deep-sync] FIM: {entered} entraram, {left} saíram, {unchanged} sem mudança, {errors} erros")
    return {"entered": entered, "left": left, "unchanged": unchanged, "errors": errors,
            "found_in_group": entered + sum(1 for l in leads if l["in_private_group"])}


async def _qualify_lead(client, lead_row: dict, skip_vision: bool) -> dict:
    """Extrai IDs (multi-conta), valida no partner bot, monta a linha do lead."""
    from userbot.leads import find_recent_account_id_in_dms, validate_id_via_partner_bot

    lead_id = lead_row["id"]
    tg_id = lead_row["telegram_id"]

    # Se já tem ID validado no banco, reusa (grátis, sem re-consultar)
    accounts = []  # lista de dicts validados: {id, country, balance, deposits_sum, deposits_count}
    id_status = lead_row.get("liga_id_status")

    already_validated = (
        id_status == "validated" and lead_row.get("liga_account_id")
    )
    if already_validated:
        accounts.append({
            "id": lead_row["liga_account_id"],
            "country": lead_row.get("liga_id_country"),
            "balance": lead_row.get("liga_id_balance") or 0.0,
            "deposits_sum": lead_row.get("liga_id_deposits_sum") or 0.0,
            "deposits_count": None,
            "active": True,
        })

    # Se não tem validado ainda, escaneia a conversa
    new_status = id_status
    if not accounts:
        try:
            found = await find_recent_account_id_in_dms(
                client, tg_id,
                max_messages=50,
                scan_images=(not skip_vision),
                max_images=2,
                return_all=True,
            )
        except Exception as e:
            found = {"candidates": []}
            print(f"[qualify] erro lendo DMs lead={lead_row['display']}: {e}")

        candidates = found.get("candidates") or []
        # valida cada candidato (mais novo → mais velho) até esgotar
        for idx, cand in enumerate(candidates):
            cid = cand.get("id")
            if not cid:
                continue
            try:
                val = await validate_id_via_partner_bot(client, cid)
            except Exception:
                val = {"status": "error"}
            await asyncio.sleep(PARTNER_DELAY)
            if val.get("status") == "validated":
                dc = None
                m = _DEP_COUNT_RE.search(val.get("raw") or "")
                if m:
                    dc = int(m.group(1))
                accounts.append({
                    "id": cid,
                    "country": val.get("country"),
                    "balance": val.get("balance") or 0.0,
                    "deposits_sum": val.get("deposits_sum") or 0.0,
                    "deposits_count": dc,
                    "active": (idx == 0),  # o mais recente é o "ativo"
                })

        # define status: validou algo? senão, mandou print mas não deu → needs_review
        if accounts:
            new_status = "validated"
        elif candidates:
            new_status = "invalid"  # achou ID mas partner rejeitou
        elif (not skip_vision) and _has_any_image(lead_id):
            new_status = "needs_review"  # mandou print mas Vision não leu ID
        else:
            new_status = id_status  # sem print/sem ID → deixa como estava

        # persiste o melhor (ativo) no Lead pra próximas rodadas
        if accounts:
            active = max(accounts, key=lambda a: (a.get("active"), a.get("balance") or 0))
            with SessionLocal() as s:
                fresh = s.query(Lead).get(lead_id)
                if fresh:
                    fresh.liga_account_id = active["id"]
                    fresh.liga_id_status = "validated"
                    fresh.liga_id_country = (active.get("country") or "")[:50]
                    fresh.liga_id_balance = active.get("balance")
                    fresh.liga_id_deposits_sum = active.get("deposits_sum")
                    fresh.liga_id_validated_at = datetime.utcnow()
                    s.commit()
        elif new_status and new_status != id_status:
            with SessionLocal() as s:
                fresh = s.query(Lead).get(lead_id)
                if fresh:
                    fresh.liga_id_status = new_status
                    s.commit()

    return {"accounts": accounts, "id_status": new_status}


def _categorize(lead_row: dict, accounts: list, min_deposit: float) -> tuple[str, str]:
    """Retorna (categoria, pagou_sim_nao)."""
    in_group = bool(lead_row.get("in_private_group"))
    total_dep = sum((a.get("deposits_sum") or 0.0) for a in accounts)
    pagou = "sim" if total_dep >= min_deposit else "não"

    if in_group:
        recent_fb = _count_recent_feedback_images(lead_row["id"])
        cat = "Ativo" if recent_fb >= ATIVO_MIN_IMAGES else "VIP"
    elif accounts:
        cat = "Registro"   # tem conta, ainda não entrou no grupo
    else:
        cat = "Lead"       # sem conta
    return cat, pagou


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-deep-sync", action="store_true")
    ap.add_argument("--skip-vision", action="store_true")
    ap.add_argument("--no-scan", action="store_true",
                    help="não lê conversas nem consulta partner bot — só usa o que já está no banco (instantâneo)")
    ap.add_argument("--only-deep-sync", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--min-deposit", type=float, default=1.0)
    args = ap.parse_args()

    cost_before = _ai_cost_total()

    # Só conecta ao Telegram se REALMENTE precisar (deep sync ou scan de conversas).
    # Modo --no-scan + --skip-deep-sync = leitura pura do data.db, ZERO Telegram/.env.
    need_telegram = (not args.skip_deep_sync) or (not args.no_scan)
    client = None
    if need_telegram:
        from userbot.client import get_client, get_private_group_entity
        client = await get_client()
        print("[main] conectado ao Telegram")
    else:
        print("[main] modo offline (só data.db) — sem conectar ao Telegram")

    # ── Fase 1: deep sync bidirecional ──
    deep_stats = {}
    if not args.skip_deep_sync:
        try:
            group = await get_private_group_entity()
            deep_stats = await _deep_sync_bidirectional(client, group, limit=args.limit)
        except Exception as e:
            print(f"[main] deep sync falhou: {e}")
    else:
        print("[main] deep sync PULADO (--skip-deep-sync)")

    if args.only_deep_sync:
        print("[main] --only-deep-sync: encerrando sem qualificar.")
        return

    # ── Fase 2+3: qualificação + categorização ──
    with SessionLocal() as s:
        q = s.query(Lead).filter(Lead.telegram_id.isnot(None))
        if args.limit:
            q = q.limit(args.limit)
        lead_rows = [
            {"id": l.id, "telegram_id": l.telegram_id, "username": l.username,
             "first_name": l.first_name, "last_name": l.last_name,
             "display": l.display_name, "in_private_group": bool(l.in_private_group),
             "liga_account_id": l.liga_account_id, "liga_id_status": l.liga_id_status,
             "liga_id_country": l.liga_id_country, "liga_id_balance": l.liga_id_balance,
             "liga_id_deposits_sum": l.liga_id_deposits_sum,
             "last_dm_at": l.last_dm_at, "opted_out": bool(l.opted_out)}
            for l in q.all()
        ]

    total = len(lead_rows)
    print(f"[qualify] qualificando {total} leads (skip_vision={args.skip_vision})...")

    rows_out = []
    cat_counts = {"Lead": 0, "Registro": 0, "VIP": 0, "Ativo": 0}
    for i, lr in enumerate(lead_rows, 1):
        if args.no_scan:
            # Modo instantâneo: só usa dados já no banco, sem ler Telegram
            if lr.get("liga_id_status") == "validated" and lr.get("liga_account_id"):
                accounts = [{
                    "id": lr["liga_account_id"], "country": lr.get("liga_id_country"),
                    "balance": lr.get("liga_id_balance") or 0.0,
                    "deposits_sum": lr.get("liga_id_deposits_sum") or 0.0,
                    "deposits_count": None, "active": True,
                }]
            else:
                accounts = []
            res = {"accounts": accounts, "id_status": lr.get("liga_id_status")}
        else:
            try:
                res = await _qualify_lead(client, lr, skip_vision=args.skip_vision)
                accounts = res["accounts"]
            except Exception as e:
                print(f"[qualify] erro lead={lr['display']}: {e}")
                accounts = []
                res = {"accounts": [], "id_status": lr.get("liga_id_status")}
        cat, pagou = _categorize(lr, accounts, args.min_deposit)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

        active = accounts[0] if accounts else {}
        other_ids = ", ".join(a["id"] for a in accounts[1:]) if len(accounts) > 1 else ""
        total_dep = sum((a.get("deposits_sum") or 0.0) for a in accounts)
        total_bal = sum((a.get("balance") or 0.0) for a in accounts)

        rows_out.append({
            "categoria": cat,
            "nome": (lr["first_name"] or "") + (" " + lr["last_name"] if lr["last_name"] else ""),
            "username": ("@" + lr["username"]) if lr["username"] else "",
            "telegram_id": lr["telegram_id"],
            "id_ativo": active.get("id", ""),
            "n_contas": len(accounts),
            "outras_contas": other_ids,
            "pais": active.get("country") or "",
            "deposito_total_usd": round(total_dep, 2),
            "saldo_total_usd": round(total_bal, 2),
            "n_depositos": active.get("deposits_count") or "",
            "pagou": pagou,
            "status_id": res["id_status"] if accounts or not args.skip_vision else lr["liga_id_status"],
            "ultima_conversa": lr["last_dm_at"].strftime("%Y-%m-%d") if lr["last_dm_at"] else "",
            "opt_out": "sim" if lr["opted_out"] else "",
        })
        if i % 25 == 0:
            print(f"[qualify] {i}/{total} · {cat_counts}")

    # ── Fase 4: CSV ordenado por depósito ──
    rows_out.sort(key=lambda r: r["deposito_total_usd"], reverse=True)
    export_dir = ROOT / "data" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    csv_path = export_dir / f"leads_{stamp}.csv"

    cols = ["categoria", "nome", "username", "telegram_id", "id_ativo", "n_contas",
            "outras_contas", "pais", "deposito_total_usd", "saldo_total_usd",
            "n_depositos", "pagou", "status_id", "ultima_conversa", "opt_out"]
    # UTF-8 com BOM → Excel abre com acentos certinho
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows_out:
            w.writerow(r)

    cost_after = _ai_cost_total()
    delta = cost_after - cost_before

    print("\n" + "=" * 60)
    print("RESUMO")
    print("=" * 60)
    print(f"Leads processados : {total}")
    print(f"Categorias        : {cat_counts}")
    if deep_stats:
        print(f"Grupo (deep sync) : {deep_stats.get('entered',0)} entraram, "
              f"{deep_stats.get('left',0)} saíram")
    print(f"Custo IA do scan  : ${delta:.4f}")
    print(f"Planilha (CSV)    : {csv_path}")
    print("=" * 60)
    print(f"\nAbrir no Excel:  start \"\" \"{csv_path}\"")


if __name__ == "__main__":
    asyncio.run(main())
