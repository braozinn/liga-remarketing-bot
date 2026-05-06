#
# Instala ffmpeg na pasta do projeto.
# Roda este script e ele baixa, extrai e configura tudo automaticamente.
#
# Uso: clique direito neste arquivo -> "Executar com PowerShell"
#      OU rode `instalar_ffmpeg.bat` que fez a mesma coisa.
#

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$tempDir    = Join-Path $env:TEMP "ffmpeg_install_$(Get-Random)"
$zipPath    = Join-Path $tempDir "ffmpeg.zip"
$extractDir = Join-Path $tempDir "extracted"

Write-Host "===================================================" -ForegroundColor Cyan
Write-Host " INSTALADOR DE FFMPEG - Bot de Remarketing" -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Pasta do projeto: $projectDir" -ForegroundColor Gray
Write-Host ""

# Verifica se ja existe
$existingFfmpeg = Join-Path $projectDir "ffmpeg.exe"
if (Test-Path $existingFfmpeg) {
    Write-Host "[!] ffmpeg.exe ja existe em $existingFfmpeg" -ForegroundColor Yellow
    $resp = Read-Host "Deseja reinstalar? (s/n)"
    if ($resp -ne "s") {
        Write-Host "Cancelado." -ForegroundColor Yellow
        Read-Host "Pressione ENTER para fechar"
        exit 0
    }
}

# Cria pasta temp
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null

# URL oficial do build essentials da gyan.dev
$url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

Write-Host "[1/4] Baixando ffmpeg (~85 MB, pode demorar 1-3 minutos)..." -ForegroundColor Cyan
Write-Host "      URL: $url" -ForegroundColor Gray
try {
    # Mostra progresso de download
    $ProgressPreference = "Continue"
    Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
    $size = (Get-Item $zipPath).Length / 1MB
    Write-Host "      Baixado: $([math]::Round($size, 1)) MB" -ForegroundColor Green
} catch {
    Write-Host "[!] FALHA no download: $_" -ForegroundColor Red
    Write-Host "    Verifique sua conexao com a internet e tente de novo." -ForegroundColor Yellow
    Read-Host "Pressione ENTER para fechar"
    exit 1
}

Write-Host ""
Write-Host "[2/4] Extraindo (pode demorar ~30s)..." -ForegroundColor Cyan
try {
    Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
    Write-Host "      OK" -ForegroundColor Green
} catch {
    Write-Host "[!] FALHA na extracao: $_" -ForegroundColor Red
    Read-Host "Pressione ENTER para fechar"
    exit 1
}

Write-Host ""
Write-Host "[3/4] Procurando os executaveis..." -ForegroundColor Cyan
$ffmpegSrc = Get-ChildItem -Path $extractDir -Filter "ffmpeg.exe" -Recurse | Select-Object -First 1
$ffprobeSrc = Get-ChildItem -Path $extractDir -Filter "ffprobe.exe" -Recurse | Select-Object -First 1

if (-not $ffmpegSrc -or -not $ffprobeSrc) {
    Write-Host "[!] Nao encontrei ffmpeg.exe ou ffprobe.exe no zip." -ForegroundColor Red
    Read-Host "Pressione ENTER para fechar"
    exit 1
}
Write-Host "      Achei em: $($ffmpegSrc.DirectoryName)" -ForegroundColor Green

Write-Host ""
Write-Host "[4/4] Copiando para a pasta do projeto..." -ForegroundColor Cyan
Copy-Item -Path $ffmpegSrc.FullName -Destination (Join-Path $projectDir "ffmpeg.exe") -Force
Copy-Item -Path $ffprobeSrc.FullName -Destination (Join-Path $projectDir "ffprobe.exe") -Force
Write-Host "      OK" -ForegroundColor Green

# Limpeza
Write-Host ""
Write-Host "Limpando arquivos temporarios..." -ForegroundColor Gray
Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue

# Verifica que funciona
Write-Host ""
Write-Host "[VERIFICACAO]" -ForegroundColor Cyan
$ffmpegExe = Join-Path $projectDir "ffmpeg.exe"
& $ffmpegExe -version 2>&1 | Select-Object -First 1 | ForEach-Object {
    Write-Host "  $_" -ForegroundColor Green
}

Write-Host ""
Write-Host "===================================================" -ForegroundColor Green
Write-Host " INSTALACAO CONCLUIDA!" -ForegroundColor Green
Write-Host "===================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Arquivos instalados:" -ForegroundColor White
Write-Host "  - $projectDir\ffmpeg.exe" -ForegroundColor Gray
Write-Host "  - $projectDir\ffprobe.exe" -ForegroundColor Gray
Write-Host ""
Write-Host "Agora reinicia o bot (Ctrl+C, depois rode main.py de novo)." -ForegroundColor Yellow
Write-Host "No log de inicio voce vera: '[ffmpeg] OK - <versao>'" -ForegroundColor Yellow
Write-Host ""
Write-Host "Quando enviar video bolinha de novo, vai sair no formato nativo." -ForegroundColor White
Write-Host ""
Read-Host "Pressione ENTER para fechar"
