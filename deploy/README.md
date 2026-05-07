# 🚀 Deploy do Liga Bot no DigitalOcean

Guia passo-a-passo pra subir o bot 24/7 numa VPS com seu PC podendo desligar.

**Custo total:** **US$6/mês** (~R$30) na droplet básica de SP. Sem custos escondidos — só Anthropic API que você já paga.

---

## 📋 Antes de começar — tenha em mãos

- [ ] Cartão de crédito internacional (DigitalOcean cobra em USD)
- [ ] Acesso ao seu repositório do GitHub (pra clonar no VPS)
- [ ] Acesso ao seu PC com o bot rodando (pra copiar `.env`, `userbot.session`, `data.db`)
- [ ] Cliente SSH (Windows: PowerShell já tem, ou Git Bash, ou PuTTY)

---

## Passo 1 — Criar a Droplet (5 min)

1. Cria conta em **https://www.digitalocean.com** (link de referral pra ganhar US$200 de crédito por 60 dias: https://m.do.co/c/ — pesquise um link de referral antes pra economizar)
2. Confirma email + adiciona cartão
3. Clica em **Create → Droplets**
4. Configura:
   - **Region:** São Paulo (BRA1) — mais próximo, latência baixa pro Telegram BR
   - **Image:** Ubuntu 24.04 (LTS) x64
   - **Plan:** Basic → Regular → **$6/mo** (1GB RAM / 1 vCPU / 25GB SSD / 1TB transfer)
   - **Authentication:** SSH Key (recomendado) ou Password
     - Se nunca criou SSH key, escolha **Password** e anota uma senha forte
   - **Hostname:** `liga-bot` (qualquer coisa, pra você reconhecer)
5. Clica **Create Droplet**. Espera ~30s.
6. Anota o **IP público** que aparece (ex: `143.198.123.45`)

---

## Passo 2 — Conectar via SSH (1 min)

No PowerShell do seu PC (Windows):

```powershell
ssh root@SEU_IP_AQUI
```

Aceita o fingerprint na primeira vez. Cola a senha que você criou (ou usa a SSH key).

Você deve ver algo tipo:
```
Welcome to Ubuntu 24.04 LTS (GNU/Linux ...)
root@liga-bot:~#
```

✅ Você está dentro do servidor.

---

## Passo 3 — Clonar o repo + rodar instalador (3 min)

> ⚠️ **Pré-requisito:** o repo precisa estar no seu GitHub. Se ainda não subiu, segue o `SETUP_GITHUB.md` na raiz do projeto antes.

Dentro do SSH, cola:

```bash
cd /opt
git clone https://github.com/SEU_USUARIO/SEU_REPO.git telegram-bot-remarketing
cd telegram-bot-remarketing
sudo bash deploy/install.sh
```

Vai instalar Python 3.12, ffmpeg, criar o virtualenv, instalar deps, e configurar o systemd. Dura uns 2-3 min.

No fim mostra o checklist com os 5 passos finais.

---

## Passo 4 — Copiar arquivos sensíveis do seu PC pro VPS (3 min)

Esses 3 arquivos **NÃO** estão no git (corretamente, são sensíveis). Você precisa enviá-los manualmente.

**Abra um SEGUNDO terminal no seu PC** (mantém o SSH aberto no primeiro), e cola:

### 4a. `.env` (credenciais)
```powershell
scp C:\telegram-bot-remarketing\.env root@SEU_IP:/opt/telegram-bot-remarketing/.env
```

### 4b. `userbot.session` (sessão do Telegram já logada)
```powershell
scp C:\telegram-bot-remarketing\userbot.session root@SEU_IP:/opt/telegram-bot-remarketing/userbot.session
```

> 💡 **Por que isso é importante:** se você não copiar o `.session`, o bot vai pedir o código SMS de novo no VPS — mas você está em SSH headless, sem como digitar o código. Copiando, ele já entra logado.

### 4c. `data.db` (banco com 2900+ leads, scripts, métricas)
```powershell
scp C:\telegram-bot-remarketing\data.db root@SEU_IP:/opt/telegram-bot-remarketing/data.db
```

---

## Passo 5 — Ajustar paths Linux no `.env` (1 min)

Volta pro SSH (terminal 1) e edita o `.env`:

```bash
cd /opt/telegram-bot-remarketing
nano .env
```

Procura essa linha e muda:

```bash
# DE:
OBSIDIAN_VAULT_PATH=C:\liga-vault

# PRA (uma das opções):
OBSIDIAN_VAULT_PATH=/var/lib/liga-vault   # se quer continuar usando vault
# ou simplesmente vazio (desabilita export pra obsidian):
OBSIDIAN_VAULT_PATH=
```

`Ctrl+O` → `Enter` → `Ctrl+X` pra salvar e sair.

> Se você usa Obsidian no PC pra ver a vault, tem 2 opções:
> 1. Roda Obsidian com o vault em `/var/lib/liga-vault` montado via [Syncthing](https://syncthing.net) (recomendado)
> 2. Desabilita (`OBSIDIAN_VAULT_PATH=`) — você perde a vault mas o bot funciona normal

---

## Passo 6 — Subir o bot 🚀

Ainda no SSH:

```bash
sudo systemctl start liga-bot
sudo systemctl status liga-bot
```

Esperado:
```
● liga-bot.service - Liga Remarketing Bot
   Active: active (running) since ...
```

Pra ver os logs em tempo real:
```bash
sudo journalctl -u liga-bot -f
```

Você deve ver:
```
[INFO] main: Banco inicializado.
[INFO] main: [ffmpeg] OK — ffmpeg version 6.x...
[INFO] main: Agendador iniciado.
[INFO] main: Painel web em http://127.0.0.1:8080
```

✅ **O bot está rodando 24/7 no VPS.** Pode desligar seu PC sem perder nada.

---

## Passo 7 — Acessar o painel web (de qualquer lugar)

O painel web roda em `127.0.0.1:8080` no VPS — propositalmente NÃO exposto na internet pública. Use **SSH tunnel** pra acessar do seu PC com segurança total:

No seu PC, abre PowerShell:

```powershell
ssh -L 8080:127.0.0.1:8080 root@SEU_IP
```

Deixa esse SSH aberto. Depois abre no navegador:

```
http://localhost:8080
```

✅ Painel completo, criptografado via SSH, ninguém na internet vê.

> Se quiser fechar o tunnel, é só fechar o terminal SSH.

---

## Operação do dia-a-dia

### Ver logs
```bash
sudo journalctl -u liga-bot -f          # tempo real
sudo journalctl -u liga-bot --since "1h ago"   # última hora
sudo journalctl -u liga-bot -n 100      # últimas 100 linhas
```

### Reiniciar o bot
```bash
sudo systemctl restart liga-bot
```

### Parar o bot (mantém estado salvo)
```bash
sudo systemctl stop liga-bot
```

### Atualizar o código (depois de fazer git push do PC)
No VPS:
```bash
cd /opt/telegram-bot-remarketing
sudo bash deploy/update.sh
```

O script faz: backup do db → `git pull` → reinstala deps se mudou → restart.

### Backup do banco pro seu PC
No PC:
```powershell
scp root@SEU_IP:/opt/telegram-bot-remarketing/data.db C:\backup-vps\data.db
```

---

## Troubleshooting

### Bot não sobe — `systemctl status liga-bot` mostra `failed`
```bash
sudo journalctl -u liga-bot -n 50
```
Lê as últimas 50 linhas. Erros comuns:

- **"Falha ao logar no Telegram"** → você esqueceu de copiar `userbot.session` (Passo 4b)
- **"OBSIDIAN_VAULT_PATH=C:\liga-vault não existe"** → não muda pra Linux (Passo 5). Não é fatal, só warning.
- **"no module named X"** → dep faltando, roda `sudo bash deploy/install.sh` de novo

### Painel web não abre
- Confirma que o SSH tunnel está aberto (`ssh -L 8080:127.0.0.1:8080 root@IP`)
- Confirma `WEB_HOST=127.0.0.1` e `WEB_PORT=8080` no `.env` do VPS
- `sudo journalctl -u liga-bot -f` deve mostrar "Painel web em http://127.0.0.1:8080"

### Bot é killado por consumo de memória
- Droplet de $6 tem 1GB. Bot usa ~150MB em idle, ~400MB durante análise contextual.
- Se ficar perto do limite, faz upgrade pra droplet de $12 (2GB) — sem reinstalação, só clica resize no painel DO

### Conta do Telegram caiu / banida
- Telegram **detesta** userbots. Mesmo passivo, com volume alto pode banir.
- Sinais: bot para de receber DMs, FloodWaitError nos logs.
- Solução: aquece a conta nova lentamente (1500 → 100 envios/dia primeira semana), aumenta gradualmente.

---

## Custos mensais previstos

| Item | Custo |
|---|---|
| Droplet DigitalOcean (1GB SP) | US$6 |
| Anthropic API (Haiku, ~30 leads/dia ativo) | ~US$2-5 |
| Tráfego de rede (incluso 1TB) | $0 |
| **Total** | **~US$8-11/mês** (~R$45-60) |

Com comissão de R$5-8 por FTD: **break-even em ~10 FTDs/mês**.

---

## Próximos passos opcionais

1. **HTTPS público** (`deploy/nginx.conf`): se quiser acessar painel sem SSH tunnel
2. **Backup automático pro S3/R2**: agendar `data.db` → bucket todo dia
3. **Monitoramento (UptimeRobot)**: ping no painel pra alerta se cair
4. **Syncthing pro vault Obsidian**: vault sincronizada PC ↔ VPS

Avisa se quer eu prepar qualquer um desses.
