# Como subir o projeto pro GitHub (backup remoto)

## ⚠️ ANTES DE TUDO — CONFIRA O `.gitignore`

O `.gitignore` já está configurado pra **NÃO subir**:
- `.env` (suas credenciais Telegram + Anthropic)
- `*.session` (sessão do Telethon — quem tem isso entra na sua conta!)
- `data.db` (banco com leads + scripts + métricas)
- `media/proofs/` (screenshots dos leads)
- `backup_*.zip`

**JAMAIS suba esses arquivos.** Se aparecer pra você no `git status`, algo tá errado.

---

## Passo 1 — Criar repo no GitHub

1. Vai em https://github.com/new
2. Nome sugerido: `liga-remarketing-bot`
3. **Privado** (não público — tem código sensível mesmo sem segredos)
4. **NÃO** marca "Add README" / "Add .gitignore" — já temos
5. Cria. Anota a URL: `https://github.com/SEU_USUARIO/liga-remarketing-bot.git`

---

## Passo 2 — Inicializar git local + 1º commit

No terminal, dentro de `C:\telegram-bot-remarketing`:

```bash
git init
git branch -M main
git add .
```

**Confere o que vai ser comitado** (deve ter ~30-40 arquivos .py + .html + .md, NUNCA `.env`/`*.session`/`data.db`):

```bash
git status
```

Se algo sensível aparecer no status, **PARA AGORA** e adiciona no `.gitignore` antes de continuar.

```bash
git -c user.email="seu@email.com" -c user.name="Seu Nome" commit -m "Initial commit — Liga · Remarketing Bot"
```

---

## Passo 3 — Conectar com o GitHub e push

```bash
git remote add origin https://github.com/SEU_USUARIO/liga-remarketing-bot.git
git push -u origin main
```

Vai pedir login. Em vez de senha, use **Personal Access Token** (Github → Settings → Developer settings → Personal access tokens → Generate, escopo `repo`).

---

## Passo 4 — Subir versões anteriores como tags retroativas

Se você quer marcar pontos importantes da história, crie tags:

```bash
git tag v0.1-mvp -m "MVP: scripts + envios manuais"
git tag v0.2-liga -m "Liga: máquina de estados + 8 jobs cron"
git tag v0.3-design -m "Redesign Claude + dark mode"
git tag v0.4-categorizer -m "Engagement tags + opt-out + rate limit"
git tag v0.5-automation -m "Backup + digest + follow-ups + VIP detection"
git push --tags
```

Adapte os nomes/versões pra refletir sua história real.

---

## Passo 5 — Backup contínuo

Daqui pra frente, sempre que mexer:

```bash
git add .
git status              # confere o que vai
git commit -m "feat: descrição curta da mudança"
git push
```

**Recomendo subir todo dia** — pelo menos uma vez. É o seu safety net.

---

## Restaurando em outra máquina

Quando quiser ressuscitar o projeto em outro computador:

```bash
git clone https://github.com/SEU_USUARIO/liga-remarketing-bot.git
cd liga-remarketing-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Depois:
1. Copia o `.env.example` pra `.env` e preenche credenciais
2. Roda `python main.py` — vai pedir código do Telegram, salva nova sessão
3. Banco vem zerado — restaure manualmente do último `backup_*.zip` que o bot mandou pra você no Saved Messages

---

## Recuperação de desastre — restaurando o `data.db`

Se o disco morrer:
1. Pega o `backup_AAAA-MM-DD.zip` mais recente do Telegram (Saved Messages)
2. Extrai pra raiz do projeto
3. `data.db` volta na hora — leads, scripts, comprovantes, tudo

---

## Quero versionar coisas além do código?

Algumas decisões que sugiro **NÃO** versionar:
- `.env` → segredos
- `*.session` → roubam sua conta Telegram
- `data.db` → operacional, atualiza toda hora; tem backup automático
- `media/proofs/` → tamanho cresce, dados de leads

E coisas que SIM:
- ✓ Código Python (`*.py`)
- ✓ Templates (`web/templates/*.html`)
- ✓ CSS (`web/static/style.css`)
- ✓ `requirements.txt`
- ✓ `config.yaml` (config de pesos do score, sem segredos)
- ✓ `.env.example` (template SEM segredos reais)
- ✓ `README.md`, `ROADMAP.md`, `TUTORIAL.md`
- ✓ `deploy/` (scripts pra subir no VPS — sem segredos)

---

## 🚀 Próximo: subir no VPS pra rodar 24/7

Depois que o repo estiver no GitHub, você pode subir o bot numa droplet $6/mês
e o seu PC pode ficar desligado.

Guia completo: **[deploy/README.md](deploy/README.md)**

Resumo do fluxo:
1. Cria droplet no DigitalOcean
2. SSH no VPS → `git clone` desse repo
3. `sudo bash deploy/install.sh` (instala tudo)
4. `scp` dos arquivos sensíveis (`.env`, `userbot.session`, `data.db`)
5. `sudo systemctl start liga-bot`
6. `ssh -L 8080:127.0.0.1:8080 root@IP` pra acessar painel
