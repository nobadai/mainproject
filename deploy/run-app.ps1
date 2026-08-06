<#
    스케줄드 태스크가 실행하는 앱 실행 스크립트.
    deploy.ps1 이 남긴 runtime.json 에서 설정을 읽어 앱을 띄우고 로그를 남깁니다.
#>

$ErrorActionPreference = 'Stop'

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$appRoot = Split-Path -Parent $here

$cfg    = Get-Content (Join-Path $here 'runtime.json') -Raw | ConvertFrom-Json
$python = $cfg.VENV_PYTHON
$logDir = $cfg.LOG_DIR

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$stamp  = Get-Date -Format 'yyyyMMdd'
$outLog = Join-Path $logDir "app-$stamp.log"
$errLog = Join-Path $logDir "app-$stamp.err.log"

$env:APP_PORT   = $cfg.APP_PORT
$env:PYTHONPATH = $appRoot
# 로그가 버퍼에 갇히지 않고 즉시 파일에 쓰이도록 합니다.
$env:PYTHONUNBUFFERED = '1'

Set-Location $appRoot
& $python -m app.main *>&1 | Tee-Object -FilePath $outLog -Append
