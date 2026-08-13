<#
.SYNOPSIS
    대상 Windows PC 에서 실행되는 컨테이너 배포 스크립트.

.DESCRIPTION
    GHCR 에서 Backend / Frontend 이미지를 받아 Docker Compose 로 컨테이너를
    교체하고, Frontend 와 Backend 응답을 확인합니다.
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
$LegacyAppName      = if ($env:APP_NAME) { $env:APP_NAME } else { 'mainproject' }
$LegacyAppPort      = if ($env:APP_PORT) { $env:APP_PORT } else { '8000' }
$FrontendUrl        = 'http://127.0.0.1/'
$BackendHealthUrl   = 'http://127.0.0.1:8000/health'
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
    $containerId = & docker ps -aq --filter "name=^${ContainerName}$"
    if (-not $containerId) {
        return $null
    }

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
$previousBackendImage = Get-ContainerImage $BackendContainer
$previousFrontendImage = Get-ContainerImage $FrontendContainer
$legacyContainerId = & docker ps -aq --filter "name=^${LegacyAppName}$"
$legacyContainerExists = [bool]$legacyContainerId
$legacyContainerWasRunning = [bool](& docker ps -q --filter "name=^${LegacyAppName}$")

# 기존 단일 Backend 컨테이너에서 Compose 로 처음 전환할 때도 직전 Backend
# 이미지를 기록합니다. 8000 포트 충돌을 피하기 위해 반영 직전에 중지하며,
# 최초 전환이 실패하면 다시 시작합니다.
if (-not $previousBackendImage -and $legacyContainerExists) {
    $previousBackendImage = Get-ContainerImage $LegacyAppName
}

if ($previousBackendImage) {
    Write-Host "직전 Backend 이미지: $previousBackendImage"
} else {
    Write-Host '실행 중인 Backend 컨테이너 없음 (최초 배포).'
}

if ($previousFrontendImage) {
    Write-Host "직전 Frontend 이미지: $previousFrontendImage"
} else {
    Write-Host '실행 중인 Frontend 컨테이너 없음 (최초 배포).'
}

# ── 3. 이미지 받기 ────────────────────────────────────────────────
Write-Step "Backend 이미지 pull: $BackendImage"
Invoke-Docker pull $BackendImage

Write-Step "Frontend 이미지 pull: $FrontendImage"
Invoke-Docker pull $FrontendImage

$env:BACKEND_IMAGE = $BackendImage
$env:FRONTEND_IMAGE = $FrontendImage
$env:APP_VERSION = $AppVersion
$env:BACKEND_PORT = '8000'
$env:FRONTEND_PORT = '80'

# ── 4. 컨테이너 교체 ──────────────────────────────────────────────
Write-Step 'Docker Compose 컨테이너 교체'
$healthy = $false
try {
    if ($legacyContainerWasRunning) {
        Invoke-Docker stop $LegacyAppName | Out-Null
        Write-Host "기존 단일 Backend 컨테이너 '$LegacyAppName' 중지됨."
    }

    Invoke-Compose up --detach --force-recreate --remove-orphans
    Write-Host "컨테이너 '$BackendContainer', '$FrontendContainer' 기동됨."

    # ── 5. 헬스체크 (실패 시 롤백) ────────────────────────────────
    Write-Step "헬스체크: $FrontendUrl / $BackendHealthUrl"
    $healthy = Test-Deployment
} catch {
    Write-Host "컨테이너 반영 중 오류: $($_.Exception.Message)" -ForegroundColor Red
}

if (-not $healthy) {
    Write-Host "`n헬스체크 실패." -ForegroundColor Red
    Write-Host "`n--- Compose 로그 (최근 50줄) ---"
    & docker compose --project-name $ProjectName --file $ComposeFile logs --tail 50

    if ($previousBackendImage -and $previousFrontendImage) {
        Write-Host "`n직전 Backend / Frontend 이미지로 롤백합니다." -ForegroundColor Yellow
        $env:BACKEND_IMAGE = $previousBackendImage
        $env:FRONTEND_IMAGE = $previousFrontendImage
        Invoke-Compose up --detach --force-recreate --remove-orphans

        if (Test-Deployment) {
            Write-Host '롤백 완료.' -ForegroundColor Yellow
        } else {
            Write-Host '롤백 후에도 헬스체크가 통과하지 못했습니다.' -ForegroundColor Red
        }
    } else {
        Write-Host '두 서비스의 직전 이미지가 없어 새 Compose 컨테이너를 내립니다.' -ForegroundColor Yellow
        & docker compose --project-name $ProjectName --file $ComposeFile down
        if ($legacyContainerWasRunning) {
            Invoke-Docker start $LegacyAppName | Out-Null
            Write-Host "기존 단일 Backend 컨테이너 '$LegacyAppName' 복구됨." -ForegroundColor Yellow
        }
    }
    throw '배포 실패: 헬스체크가 통과하지 못했습니다.'
}

Write-Host "`n배포 성공. $FrontendUrl 및 $BackendHealthUrl 응답 정상." -ForegroundColor Green

# 기존 단일 Backend 컨테이너는 새 서비스 검증이 끝난 뒤 제거합니다.
if ($legacyContainerExists) {
    Invoke-Docker rm --force $LegacyAppName | Out-Null
    Write-Host "기존 단일 Backend 컨테이너 '$LegacyAppName' 제거됨."
}

& docker ps --filter "name=^mainproject-" --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

# ── 6. 오래된 이미지 정리 ─────────────────────────────────────────
# 태그 없는 dangling 이미지만 지웁니다. 롤백용 이전 태그는 남습니다.
Write-Step '사용하지 않는 이미지 정리'
& docker image prune --force | Out-Null
