#!/usr/bin/env bash
# =============================================================================
# Liga Remarketing Bot — instalação 1-shot em VPS Ubuntu 22.04 / 24.04
#
# Como usar (depois de fazer git clone do repo no /opt):
#   cd /opt/telegram-bot-remarketing
#   sudo bash deploy/install.sh
#
# O que faz:
#   1. apt update + instala Python 3.11+, ffmpeg, git, sqlite3, nginx (opcional)
#   2. Cria virtualenv em .venv/
#   3. Instala requirements.txt
#   4. Cria pasta media/, logs/, /var/lib/liga-bot (vault opcional)
#   5. Copia bot.service pra /etc/systemd/system/, faz enable
#   6. NÃO sobe ainda — você precisa colocar .env, userbot.session, data.db antes
# =============================================================================

set -euo pipefail

# ---- cores pra log
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
log()  { echo -e "${G}[install]${N} $*"; }
warn() { echo -e "${Y}[install]${N} $*"; }
err()  { echo -e "${R}[install]${N} $*" >&2; }

# ---- detecta diretório do projeto (assume que esse script roda dentro de deploy/)
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
log "Diretório do projeto: $PROJECT_DIR"

# ---- precisa ser root pra apt + systemd
if [[ $EUID -ne 0 ]]; then
    err "Esse script precisa rodar como root. Use: sudo bash deploy/install.sh"
    exit 1
fi

# ---- 1. dependências do sistema
log "Atualizando apt..."
apt-get update -qq

log "Instalando dependências do sistema (Python, ffmpeg, git, sqlite3)..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 python3-venv python3-pip python3-dev \
    ffmpeg \
    git \
    sqlite3 \
    build-essential \
    ca-certificates \
    curl \
    tzdata \
    > /dev/null

# Verifica versão Python
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
log "Python: $PY_VERSION (precisa >= 3.10)"

# ---- 2. timezone (BA / São Paulo)
log "Setando timezone para America/Sao_Paulo..."
timedatectl set-timezone America/Sao_Paulo || warn "Não consegui setar timezone (não crítico)"

# ---- 3. venv + dependências Python
cd "$PROJECT_DIR"

if [[ ! -d ".venv" ]]; then
    log "Criando virtualenv em .venv/..."
    python3 -m venv .venv
fi

log "Instalando requirements.txt no venv..."
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# ---- 4. estrutura de pastas
log "Criando pastas media/, logs/..."
mkdir -p media/proofs logs

# Vault opcional (se OBSIDIAN_VAULT_PATH no .env apontar pra cá)
if [[ ! -d "/var/lib/liga-vault" ]]; then
    log "Criando vault opcional em /var/lib/liga-vault (configure OBSIDIAN_VAULT_PATH no .env se quiser usar)"
    mkdir -p /var/lib/liga-vault
fi

# ---- 5. systemd service
log "Instalando systemd service..."
SERVICE_SRC="$PROJECT_DIR/deploy/bot.service"
SERVICE_DST="/etc/systemd/system/liga-bot.service"

if [[ ! -f "$SERVICE_SRC" ]]; then
    err "$SERVICE_SRC não encontrado!"
    exit 1
fi

# Substitui {{PROJECT_DIR}} pelo path real
sed "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" "$SERVICE_SRC" > "$SERVICE_DST"

systemctl daemon-reload
systemctl enable liga-bot.service > /dev/null
log "Service liga-bot.service instalado e habilitado"

# ---- 6. firewall (UFW) — abre só SSH e (opcionalmente) painel web
if command -v ufw &> /dev/null; then
    log "Configurando UFW (libera SSH apenas)..."
    ufw allow 22/tcp > /dev/null
    # Painel web NÃO é exposto por padrão — use SSH tunnel pra acessar
    # Se quiser nginx + HTTPS depois, libera 80 e 443
    if ! ufw status | grep -q "Status: active"; then
        warn "UFW não está ativo. Pra ativar: ufw enable (cuidado: cai SSH se 22 não tá liberado)"
    fi
fi

# ---- 7. checklist final
log ""
log "==================================================================="
log "  Instalação concluída! Próximos passos:"
log "==================================================================="
log ""
log "  1. Copiar .env para o VPS (do seu PC, via scp):"
log "     scp /c/telegram-bot-remarketing/.env root@SEU_IP:$PROJECT_DIR/.env"
log ""
log "  2. Copiar userbot.session (sua sessão Telegram):"
log "     scp /c/telegram-bot-remarketing/userbot.session root@SEU_IP:$PROJECT_DIR/userbot.session"
log ""
log "  3. Copiar data.db (banco com leads, scripts, métricas):"
log "     scp /c/telegram-bot-remarketing/data.db root@SEU_IP:$PROJECT_DIR/data.db"
log ""
log "  4. Iniciar o bot:"
log "     systemctl start liga-bot"
log "     systemctl status liga-bot   # verifica se subiu"
log "     journalctl -u liga-bot -f   # logs em tempo real"
log ""
log "  5. Acessar o painel web (SSH tunnel — seguro, sem expor publicamente):"
log "     No seu PC: ssh -L 8080:127.0.0.1:8080 root@SEU_IP"
log "     Depois: http://localhost:8080 no navegador"
log ""
log "==================================================================="
