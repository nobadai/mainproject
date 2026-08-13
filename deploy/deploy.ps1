<#
.SYNOPSIS
    대상 Windows PC 에서 실행되는 컨테이너 배포 스크립트.

.DESCRIPTION
    GHCR 에서 Backend / Frontend 이미지를 받아 Docker Compose 로 컨테이너를
    교체하고, Nginx 를 통한 Frontend 와 Backend 응답을 확인합니다.
    실패하면 직전에 돌던 Backend / Frontend 이미지로 되돌립니다.

    이전 방식(가상환경 + 스케줄드 태스크)의 잔재가 남아 있으면 함께 정리합니다.
    그대로 두면 앱 포트를 계속 점유해 컨테이너가 뜨지 못합니다.

.NOTES
    BACKEND_IMAGE / FRONTEND_IMAGE / APP_VERSION 은 워크플로에서 주입됩니다.
    Docker Desktop 이 실행 중이어야 합니다(데몬이 사용자 세션에 붙어 있음).
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Windows PowerShell 5.1 의 기본 출력 인코딩은 시스템 코드페이지(한국어=CP949)라
# Actions 로그에서 한글이 깨집니다. UTF-8 로 맞춰 로그를 읽을 수 있게 합니다.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ── 설정 ──────────────────────────────────────────────────────────
$BackendImage       = $env:BACKEND_IMAGE
$FrontendImage      = $env:FRONTEND_IMAGE
$AppVersion         = if ($env:APP_VERSION) { $env:APP_VERSION } else { 'dev' }
$ProjectName        = if ($env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME } else { 'mainproject' }
$RepositoryRoot     = Split-Path -Parent $PSScriptRoot
$ComposeFile        = Join-Path $RepositoryRoot 'compose.yml'
$BackendContainer   = 'mainproject-backend'
$FrontendContainer  = 'mainproject-frontend'
$NginxContainer     = 'mainproject-nginx'
$LegacyAppName      = if ($env:APP_NAME) { $env:APP_NAME } else { 'mainproject' }
$LegacyAppPort      = if ($env:APP_PORT) { $env:APP_PORT } else { '8000' }
$FrontendUrl        = 'http://127.0.0.1/'
$BackendHealthUrl   = 'http://127.0.0.1/api/health'
$LegacyTask         = "app-$LegacyAppName"
$LegacyDir          = 'C:\apps\mainproject'

if (-not $BackendImage) { throw 'BACKEND_IMAGE 환경변수가 설정되지 않았습니다.' }
if (-not $FrontendImage) { throw 'FRONTEND_IMAGE 환경변수가 설정되지 않았습니다.' }
if (-not (Test-Path $ComposeFile)) { throw "Compose 파일을 찾을 수 없습니다: $ComposeFile" }

function Write-Step($Message) {
    Write-Host "`n=== $Message ===" -ForegroundColor Cyan
}

# docker 는 실패해도 예외를 던지지 않으므로 종료 코드를 직접 확인합니다.
function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]] $DockerArgs)
    & docker @DockerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($DockerArgs -join ' ') 실패 (exit $LASTEXITCODE)"
    }
}

# Compose 도 실패해도 예외를 던지지 않으므로 종료 코드를 직접 확인합니다.
function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]] $ComposeArgs)
    & docker compose --project-name $ProjectName --file $ComposeFile @ComposeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $($ComposeArgs -join ' ') 실패 (exit $LASTEXITCODE)"
    }
}

function Get-ContainerImage([string] $ContainerName) {
    $image = & docker inspect --format '{{.Config.Image}}' $ContainerName 2>$null
    if ($LASTEXITCODE -eq 0 -and $image) {
        return $image.Trim()
    }
    return $null
}

function Test-Deployment {
    for ($i = 1; $i -le 30; $i++) {
        Start-Sleep -Seconds 2
        try {
            $frontendResponse = Invoke-WebRequest -Uri $FrontendUrl -UseBasicParsing -TimeoutSec 5
            $backendResponse = Invoke-WebRequest -Uri $BackendHealthUrl -UseBasicParsing -TimeoutSec 5
            if ($frontendResponse.StatusCode -eq 200 -and $backendResponse.StatusCode -eq 200) {
                return $true
            }
        } catch {
            Write-Host "  대기 중... ($i/30)"
        }
    }
    return $false
}

# ── 0. 도커 데몬 확인 ─────────────────────────────────────────────
Write-Step '도커 데몬 확인'
& docker version --format '{{.Server.Version}}'
if ($LASTEXITCODE -ne 0) {
    throw 'Docker 데몬에 연결할 수 없습니다. 대상 PC 에서 Docker Desktop 이 실행 중인지 확인하세요.'
}

& docker compose version
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose 를 사용할 수 없습니다. Docker Desktop 의 Compose 플러그인을 확인하세요.'
}

# ── 1. 이전 배포 방식 정리 (최초 1회만 의미 있음) ─────────────────
Write-Step '레거시(스케줄드 태스크) 방식 정리'
$legacy = Get-ScheduledTask -TaskName $LegacyTask -ErrorAction SilentlyContinue
if ($legacy) {
    Stop-ScheduledTask -TaskName $LegacyTask -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $LegacyTask -Confirm:$false
    Write-Host "스케줄드 태스크 '$LegacyTask' 제거됨."
}

# 태스크를 지워도 자식 python.exe 가 살아남아 포트를 물고 있을 수 있습니다.
# 레거시 경로에서 실행된 것만 종료하고, 무관한 프로세스는 건드리지 않습니다.
foreach ($conn in @(Get-NetTCPConnection -LocalPort $LegacyAppPort -State Listen -ErrorAction SilentlyContinue)) {
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    if ($proc -and $proc.Path -and $proc.Path.StartsWith($LegacyDir, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "레거시 프로세스 종료: $($proc.ProcessName) (PID $($proc.Id))"
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
}

if (Test-Path $LegacyDir) {
    Remove-Item $LegacyDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "레거시 디렉터리 '$LegacyDir' 정리됨."
}

# ── 2. 롤백 대상 기록 ─────────────────────────────────────────────
# 지금 돌고 있는 컨테이너의 이미지를 기억해 둡니다. 새 이미지가 헬스체크를
# 통과하지 못하면 이 이미지로 되돌립니다.
$previousImage = & docker ps -a --filter "name=^$AppName$" --format '{{.Image}}' |
    Select-Object -First 1
if ($previousImage) {
    Write-Host "직전 이미지: $previousImage"
} else {
    Write-Host '실행 중인 컨테이너 없음 (최초 배포).'
}

# ── 3. 이미지 받기 ────────────────────────────────────────────────
Write-Step "이미지 pull: $Image"
Invoke-Docker pull $Image

# ── 4. 컨테이너 교체 ──────────────────────────────────────────────
Write-Step '컨테이너 교체'

function Start-App([string] $ImageRef) {
    if (& docker ps -aq --filter "name=^$AppName$") {
        Invoke-Docker rm --force $AppName | Out-Null
    }
    Invoke-Docker run --detach `
        --name $AppName `
        --restart unless-stopped `
        --publish "${AppPort}:8000" `
        --env "APP_VERSION=$AppVersion" `
        $ImageRef | Out-Null
}

Start-App $Image
Write-Host "컨테이너 '$AppName' 기동됨."

# ── 5. 헬스체크 (실패 시 롤백) ────────────────────────────────────
Write-Step "헬스체크: $HealthUrl"
$healthy = $false
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        if ((Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200) {
            $healthy = $true
            break
        }
    } catch {
        Write-Host "  대기 중... ($i/30)"
    }
}

if (-not $healthy) {
    Write-Host "`n헬스체크 실패." -ForegroundColor Red
    Write-Host "`n--- 컨테이너 로그 (최근 50줄) ---"
    & docker logs --tail 50 $AppName

    if ($previousImage -and $previousImage -ne $Image) {
        Write-Host "`n직전 이미지로 롤백합니다: $previousImage" -ForegroundColor Yellow
        Start-App $previousImage
        Write-Host '롤백 완료.' -ForegroundColor Yellow
    } else {
        Write-Host '롤백할 직전 이미지가 없습니다 (최초 배포).' -ForegroundColor Yellow
    }
    throw '배포 실패: 헬스체크가 통과하지 못했습니다.'
}

Write-Host "`n배포 성공. $HealthUrl 응답 정상." -ForegroundColor Green
& docker ps --filter "name=^$AppName$" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

# ── 6. 오래된 이미지 정리 ─────────────────────────────────────────
# 태그 없는 dangling 이미지만 지웁니다. 롤백용 이전 태그는 남습니다.
Write-Step '사용하지 않는 이미지 정리'
& docker image prune --force | Out-Null
