# Script PowerShell pra baixar backups do servidor pro PC.
# Como usar:
#   1. Copia este arquivo pro Desktop: baixar_backup.ps1
#   2. Click direito > "Run with PowerShell"
#   3. Vai baixar TODOS os backups recentes pro pasta C:\Users\<seu_user>\Documents\liga-backups\
#
# Requer: ssh + scp instalados (vem com Windows 10+).

$ErrorActionPreference = "Stop"

# ─── Configurações ───
$ServerIP = "157.230.222.177"
$ServerUser = "root"
$RemoteBackupDir = "/opt/telegram-bot-remarketing/data/backups"
$LocalBackupDir = "$env:USERPROFILE\Documents\liga-backups"

Write-Host ""
Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  Baixar Backups Liga · Remarketing Bot" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

# Cria pasta local se não existir
if (-not (Test-Path $LocalBackupDir)) {
    Write-Host "Criando pasta local: $LocalBackupDir" -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $LocalBackupDir -Force | Out-Null
}

Write-Host "Servidor: $ServerUser@$ServerIP" -ForegroundColor Green
Write-Host "Pasta servidor: $RemoteBackupDir" -ForegroundColor Green
Write-Host "Pasta local: $LocalBackupDir" -ForegroundColor Green
Write-Host ""

# Lista backups disponíveis no servidor
Write-Host "Listando backups no servidor..." -ForegroundColor Yellow
$ListaCommand = "ls -lh $RemoteBackupDir/*.gz 2>/dev/null | tail -10"
$ListaResultado = ssh "$ServerUser@$ServerIP" $ListaCommand

if (-not $ListaResultado) {
    Write-Host "Nenhum backup encontrado no servidor." -ForegroundColor Red
    Write-Host "Verifica se /opt/telegram-bot-remarketing/data/backups/ tem arquivos .gz" -ForegroundColor Red
    Read-Host "Aperta ENTER pra fechar"
    exit 1
}

Write-Host ""
Write-Host "Backups disponíveis (10 mais recentes):" -ForegroundColor Green
Write-Host $ListaResultado
Write-Host ""

# Baixa TODOS os backups recentes
Write-Host "Baixando backups pra $LocalBackupDir..." -ForegroundColor Yellow
Write-Host ""

# Usa scp com glob pra puxar todos .gz
scp "${ServerUser}@${ServerIP}:${RemoteBackupDir}/*.gz" $LocalBackupDir

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "✓ Backups baixados com sucesso!" -ForegroundColor Green

    $arquivos = Get-ChildItem -Path $LocalBackupDir -Filter "*.gz" | Sort-Object LastWriteTime -Descending
    $total = $arquivos.Count
    $tamanho = ($arquivos | Measure-Object -Property Length -Sum).Sum / 1MB

    Write-Host ""
    Write-Host "Total: $total arquivos ($([math]::Round($tamanho, 2)) MB) em $LocalBackupDir" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Últimos 5 backups:" -ForegroundColor Cyan
    $arquivos | Select-Object -First 5 | ForEach-Object {
        $sizeMB = [math]::Round($_.Length / 1MB, 2)
        Write-Host "  - $($_.Name) ($sizeMB MB, $($_.LastWriteTime))"
    }
} else {
    Write-Host "✗ Erro ao baixar backups. Verifica conexão SSH." -ForegroundColor Red
}

Write-Host ""
Write-Host "Pra abrir pasta dos backups:" -ForegroundColor Cyan
Write-Host "  explorer $LocalBackupDir" -ForegroundColor White
Write-Host ""

Read-Host "Aperta ENTER pra fechar"
