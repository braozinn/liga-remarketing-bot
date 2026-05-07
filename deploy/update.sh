#!/usr/bin/env bash
# =============================================================================
# Atualização do Liga Bot no VPS
#
# Como usar (no VPS, depois de fazer push do PC pro GitHub):
#   cd /opt/telegram-bot-remarketing
#   sudo bash deploy/update.sh
#
# Faz: backup do db, git pull, reinstala deps se mudou requirements.txt, restart
# =============================================================================

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
log()  { echo -e "${G}[update]${N} $*"; }
warn() { echo -e "${Y}[update]${N} $*"; }
err()  { echo -e "${R}[update]${N} $*" >&2; }

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# ---- 1. backup defensivo do db antes de qualquer mudança
TS=$(date +%Y%m%d_%H%M%S)
if [[ -f "data.db" ]]; then
    log "Backup defensivo: data.db.bak.$TS"
    cp data.db "data.db.bak.$TS"

    # Mantém só os 5 backups mais recentes
    ls -t data.db.bak.* 2>/dev/null | tail -n +6 | xargs -r rm
fi

# ---- 2. para o serviço (evita corromper db durante git pull)
log "Parando liga-bot..."
systemctl stop liga-bot || warn "liga-bot não estava rodando"

# ---- 3. git pull
log "git pull..."
OLD_REQS=$(sha256sum requirements.txt 2>/dev/null | cut -d' ' -f1 || echo "none")
git pull --ff-only
NEW_REQS=$(sha256sum requirements.txt 2>/dev/null | cut -d' ' -f1 || echo "none")

# ---- 4. atualiza deps se requirements.txt mudou
if [[ "$OLD_REQS" != "$NEW_REQS" ]]; then
    log "requirements.txt mudou — reinstalando deps..."
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt
else
    log "requirements.txt sem mudança — pulando pip install"
fi

# ---- 5. reload systemd se bot.service mudou
if [[ -f "deploy/bot.service" ]]; then
    NEW_SVC=$(sed "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" deploy/bot.service | sha256sum | cut -d' ' -f1)
    OLD_SVC=$(sha256sum /etc/systemd/system/liga-bot.service 2>/dev/null | cut -d' ' -f1 || echo "none")
    if [[ "$NEW_SVC" != "$OLD_SVC" ]]; then
        log "bot.service mudou — atualizando systemd..."
        sed "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" deploy/bot.service > /etc/systemd/system/liga-bot.service
        systemctl daemon-reload
    fi
fi

# ---- 6. sobe o bot
log "Iniciando liga-bot..."
systemctl start liga-bot

# Espera 3s e checa se subiu
sleep 3
if systemctl is-active --quiet liga-bot; then
    log "✓ liga-bot rodando"
    log "Logs: journalctl -u liga-bot -f"
else
    err "✗ liga-bot NÃO subiu! Veja: journalctl -u liga-bot -n 50"
    exit 1
fi
