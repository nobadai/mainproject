<#
.SYNOPSIS
    대상 Windows PC 에서 실행되는 배포 스크립트.

.DESCRIPTION
    GitHub Actions self-hosted 러너가 체크아웃한 소스를 배포 경로로 동기화하고,
    가상환경을 갱신한 뒤 앱을 스케줄드 태스크로 재시작합니다.

    앱을 "스케줄드 태스크"로 띄우는 이유:
    러너가 Start-Process 로 직접 띄운 프로세스는 워크플로 잡이 끝날 때 함께
    정리될 수 있습니다. 스케줄드 태스크는 Task Scheduler 서비스가 실행 주체이므로
    잡이 끝나도 살아남고, PC 재부팅 시 자동 시작도 함께 얻습니다.

.NOTES
    환경변수 DEPLOY_DIR / APP_NAME / APP_PORT 는 워크플로에서 주입됩니다.
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Windows PowerShell 5.1 의 기본 출력 인코딩은 시스템 코드페이지(한국어=CP949)라
# Actions 로그에서 한글이 깨집니다. UTF-8 로 맞춰 로그를 읽을 수 있게 합니다.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ── 설정 ──────────────────────────────────────────────────────────
$DeployDir  = if ($env:DEPLOY_DIR) { $env:DEPLOY_DIR } else { 'C:\apps\mainproject' }
$AppName    = if ($env:APP_NAME)   { $env:APP_NAME }   else { 'mainproject' }
$AppPort    = if ($env:APP_PORT)   { $env:APP_PORT }   else { '8000' }

$SourceDir  = $PWD.Path                       # 러너가 체크아웃한 작업 디렉터리
$ReleaseDir = Join-Path $DeployDir 'current'  # 실제 서비스되는 코드
$BackupDir  = Join-Path $DeployDir 'previous' # 직전 릴리스 (롤백용)
$VenvDir    = Join-Path $DeployDir 'venv'     # 가상환경 (릴리스 간 재사용)
$LogDir     = Join-Path $DeployDir 'logs'
$TaskName   = "app-$AppName"
$HealthUrl  = "http://127.0.0.1:$AppPort/health"

function Write-Step($Message) {
    Write-Host "`n=== $Message ===" -ForegroundColor Cyan
}

# ── 1. 디렉터리 준비 ──────────────────────────────────────────────
Write-Step "디렉터리 준비: $DeployDir"
foreach ($dir in @($DeployDir, $LogDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}

# ── 2. 기존 앱 중지 ───────────────────────────────────────────────
Write-Step "기존 앱 중지"
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    # 포트가 실제로 풀릴 때까지 대기 (파일 잠금/포트 충돌 방지)
    for ($i = 0; $i -lt 15; $i++) {
        $inUse = Get-NetTCPConnection -LocalPort $AppPort -State Listen -ErrorAction SilentlyContinue
        if (-not $inUse) { break }
        Start-Sleep -Seconds 1
    }
    Write-Host "기존 태스크 중지 완료."
}
else {
    Write-Host "실행 중인 태스크 없음 (최초 배포)."
}

# Task Scheduler 의 중지는 최상위 프로세스(powershell.exe)만 끝내므로
# 그 자식인 python.exe 가 살아남아 포트를 계속 점유할 수 있습니다.
# 남아있다면 강제 종료하되, 우리 배포 경로에서 실행된 프로세스만 건드립니다.
$stragglers = Get-NetTCPConnection -LocalPort $AppPort -State Listen -ErrorAction SilentlyContinue
foreach ($conn in $stragglers) {
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    if (-not $proc) { continue }

    if ($proc.Path -and $proc.Path.StartsWith($DeployDir, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "포트 $AppPort 를 점유 중인 잔존 프로세스 종료: $($proc.ProcessName) (PID $($proc.Id))"
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
    else {
        # 우리 앱이 아닌 다른 프로그램이 포트를 쓰고 있습니다. 남의 프로세스를
        # 죽이는 대신 명확히 실패시켜, 포트 설정을 바로잡도록 합니다.
        throw "포트 $AppPort 를 배포 대상이 아닌 프로세스가 사용 중입니다: " +
              "$($proc.ProcessName) (PID $($proc.Id), 경로: $($proc.Path)). " +
              "APP_PORT 를 변경하거나 해당 프로그램을 종료하세요."
    }
}

# 종료 후 포트가 실제로 해제됐는지 최종 확인
for ($i = 0; $i -lt 10; $i++) {
    if (-not (Get-NetTCPConnection -LocalPort $AppPort -State Listen -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Seconds 1
    if ($i -eq 9) { throw "포트 $AppPort 가 해제되지 않았습니다." }
}

# ── 3. 릴리스 교체 (직전 버전은 백업) ─────────────────────────────
Write-Step "릴리스 파일 동기화"
if (Test-Path $ReleaseDir) {
    if (Test-Path $BackupDir) { Remove-Item $BackupDir -Recurse -Force }
    Move-Item $ReleaseDir $BackupDir
    Write-Host "직전 릴리스를 previous/ 로 백업했습니다."
}
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null

# robocopy: /MIR 미러링, /XD 로 불필요한 디렉터리 제외
# robocopy 는 정상 동작 시에도 exit code 1~7 을 반환하므로 8 이상만 실패로 처리합니다.
robocopy $SourceDir $ReleaseDir /MIR /NFL /NDL /NJH /NJS /NP `
    /XD .git .github .venv venv __pycache__ .pytest_cache node_modules | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy 실패 (exit code $LASTEXITCODE)" }
$global:LASTEXITCODE = 0

# ── 4. 가상환경 및 의존성 ─────────────────────────────────────────
Write-Step "가상환경 및 의존성 설치"
$Python = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path $Python)) {
    Write-Host "가상환경 생성 중..."
    py -3 -m venv $VenvDir
}
& $Python -m pip install --upgrade pip --quiet
& $Python -m pip install -r (Join-Path $ReleaseDir 'requirements.txt') --quiet
if ($LASTEXITCODE -ne 0) { throw "의존성 설치 실패" }

# ── 5. 스케줄드 태스크 등록/갱신 ──────────────────────────────────
Write-Step "앱 태스크 등록"
$runScript = Join-Path $ReleaseDir 'deploy\run-app.ps1'
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runScript`"" `
    -WorkingDirectory $ReleaseDir

# 부팅 시 자동 시작 트리거
$trigger = New-ScheduledTaskTrigger -AtStartup

# SYSTEM 계정으로 로그인 없이 백그라운드 실행
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

# 실행 시간 제한 없음 + 실패 시 재시작
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

# 환경변수는 태스크에 직접 넘길 수 없으므로 파일로 전달합니다 (run-app.ps1 이 읽음).
@{ APP_PORT = $AppPort; LOG_DIR = $LogDir; VENV_PYTHON = $Python } |
    ConvertTo-Json | Set-Content -Path (Join-Path $ReleaseDir 'deploy\runtime.json') -Encoding utf8

Start-ScheduledTask -TaskName $TaskName
Write-Host "태스크 '$TaskName' 시작됨."

# ── 6. 헬스체크 (실패 시 롤백) ────────────────────────────────────
Write-Step "헬스체크: $HealthUrl"
$healthy = $false
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $res = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
        if ($res.StatusCode -eq 200) { $healthy = $true; break }
    } catch {
        Write-Host "  대기 중... ($i/30)"
    }
}

if (-not $healthy) {
    Write-Host "`n헬스체크 실패. 직전 릴리스로 롤백합니다." -ForegroundColor Red
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (Test-Path $BackupDir) {
        Remove-Item $ReleaseDir -Recurse -Force
        Move-Item $BackupDir $ReleaseDir
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "롤백 완료 (previous → current)." -ForegroundColor Yellow
    } else {
        Write-Host "백업이 없어 롤백할 수 없습니다 (최초 배포)." -ForegroundColor Yellow
    }
    Write-Host "`n--- 최근 로그 ---"
    Get-ChildItem $LogDir -Filter '*.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
        Get-Content -Tail 50
    throw "배포 실패: 헬스체크가 통과하지 못했습니다."
}

Write-Host "`n배포 성공. $HealthUrl 응답 정상." -ForegroundColor Green
