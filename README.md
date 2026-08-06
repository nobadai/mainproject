# mainproject

GitHub Actions로 CI를 돌리고, 사내/집 Windows PC에 self-hosted 러너로 배포하는 프로젝트입니다.

## 구조

| 경로 | 역할 |
| --- | --- |
| `.github/workflows/ci-cd.yml` | CI(클라우드 러너) → CD(self-hosted 러너) 파이프라인 |
| `deploy/deploy.ps1` | 대상 PC에서 실행되는 배포 스크립트 (동기화 → 의존성 → 재시작 → 헬스체크 → 롤백) |
| `deploy/run-app.ps1` | 스케줄드 태스크가 앱을 띄울 때 쓰는 실행 스크립트 |
| `app/main.py` | 앱 본체. `/health`는 헬스체크용이므로 유지하세요 |

배포 대상 PC의 디렉터리 레이아웃:

```
C:\apps\mainproject\
  current\    # 현재 서비스 중인 코드
  previous\   # 직전 릴리스 (헬스체크 실패 시 자동 롤백)
  venv\       # 파이썬 가상환경
  logs\       # 앱 로그
```

## 대상 PC에 GitHub Actions 러너 설치하기

러너가 GitHub 쪽으로 아웃바운드 연결을 맺어 작업을 당겨오는 구조입니다.
따라서 **공인 IP도, 포트포워딩도, 방화벽 인바운드 허용도 필요 없습니다.**

### 1. 등록 토큰 발급

GitHub 저장소 → **Settings** → **Actions** → **Runners** → **New self-hosted runner**
→ OS는 **Windows**, 아키텍처는 **x64** 선택.

화면에 나오는 토큰(`A...` 형태)은 **1시간 후 만료**되므로 바로 다음 단계를 진행하세요.

### 2. 대상 PC에서 러너 내려받기

대상 Windows PC에서 PowerShell을 **관리자 권한으로** 실행합니다.

```powershell
mkdir C:\actions-runner; cd C:\actions-runner
```

이어서 **GitHub 설정 화면의 Download 섹션에 있는 명령을 그대로 복사해 붙여넣으세요.**
러너 버전은 계속 올라가므로 화면에 표시된 URL을 쓰는 것이 가장 정확합니다. 형태는 아래와 같습니다.

```powershell
Invoke-WebRequest -Uri https://github.com/actions/runner/releases/download/v<버전>/actions-runner-win-x64-<버전>.zip -OutFile actions-runner.zip
Expand-Archive -Path actions-runner.zip -DestinationPath .
```

### 3. 러너 등록

`<TOKEN>` 자리에 1단계에서 받은 토큰을, URL은 본인 저장소 주소로 바꿉니다.

```powershell
.\config.cmd --url https://github.com/<사용자명>/mainproject --token <TOKEN> --labels self-hosted,windows --unattended
```

`--labels`의 `self-hosted,windows`는 워크플로의 `runs-on: [self-hosted, windows]`와
반드시 일치해야 합니다. 안 맞으면 배포 job이 러너를 못 찾고 무한 대기합니다.

### 4. 서비스로 등록 (PC 재부팅 후 자동 시작)

러너를 Windows 서비스로 등록하면 로그인하지 않아도, 재부팅 후에도 자동으로 동작합니다.

```powershell
.\svc.cmd install
.\svc.cmd start
```

정상 등록되면 `Get-Service actions.runner.*` 로 상태를 확인할 수 있고,
GitHub의 Runners 목록에 **Idle(초록색)** 로 표시됩니다.

> **중요 — 서비스 계정 권한.** `svc.cmd install`은 기본적으로 서비스를
> `NT AUTHORITY\NETWORK SERVICE`로 등록하는데, 이 계정은 관리자가 아니라서
> `deploy.ps1`의 스케줄드 태스크 등록(`Register-ScheduledTask`)이 **권한 오류로 실패**합니다.
> 관리자 계정으로 설치하세요.
>
> ```powershell
> .\svc.cmd install <컴퓨터명>\<관리자계정명>
> ```
>
> 이미 설치했다면 `services.msc` → `Actions Runner (...)` → 속성 → 로그온 탭에서
> 계정을 바꾼 뒤 서비스를 재시작해도 됩니다.

### 5. 대상 PC 사전 요구사항

배포 스크립트가 쓰는 도구들입니다. 미리 설치돼 있어야 합니다.

```powershell
winget install --id Python.Python.3.12 -e --scope machine
winget install --id Git.Git -e --scope machine
```

`--scope machine`이 중요합니다. 사용자 단위로 설치하면 서비스 계정의 PATH에서
파이썬이 보이지 않아 배포 중 `py: 명령을 찾을 수 없음`으로 실패합니다.

설치 후 `py -3 --version`, `git --version`이 동작하는지 확인하세요.
러너 서비스는 시작 시점의 PATH를 물고 있으므로, 파이썬을 러너보다 나중에 깔았다면
`.\svc.cmd stop; .\svc.cmd start`로 재시작해야 새 PATH가 반영됩니다.

### 6. 방화벽

앱 포트(기본 8000)를 다른 기기에서 접속하려면 인바운드 허용이 필요합니다.
같은 PC에서만 쓸 거라면 생략하세요.

```powershell
New-NetFirewallRule -DisplayName "mainproject 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

## 배포 흐름

1. `main` 브랜치에 push
2. GitHub 클라우드 러너에서 lint + test 실행
3. 통과하면 대상 PC의 self-hosted 러너가 `deploy/deploy.ps1` 실행
4. `current\`를 새 코드로 교체하고 기존 릴리스는 `previous\`로 백업
5. 스케줄드 태스크 `app-mainproject` 재시작
6. `http://127.0.0.1:8000/health` 를 최대 60초간 폴링
7. 실패하면 `previous\`로 자동 롤백하고 최근 로그 50줄 출력

## 설정 바꾸기

배포 경로·포트·태스크 이름은 워크플로의 `env:` 블록에서 조정합니다.

```yaml
env:
  DEPLOY_DIR: C:\apps\mainproject
  APP_NAME: mainproject
  APP_PORT: "8000"
```

## 문제 해결

| 증상 | 확인할 것 |
| --- | --- |
| 배포 job이 계속 대기 중 | 러너 라벨(`self-hosted`, `windows`)이 워크플로와 일치하는지, 러너가 Idle인지 |
| `py: 명령을 찾을 수 없음` | 파이썬 설치 후 러너 서비스를 재시작했는지 |
| 헬스체크 실패 | `C:\apps\mainproject\logs\` 의 최신 로그 확인 |
| 앱은 뜨는데 외부 접속 불가 | 방화벽 인바운드 규칙, 앱이 `0.0.0.0`에 바인딩되는지 |
| 스케줄드 태스크 상태 확인 | `Get-ScheduledTask -TaskName app-mainproject \| Get-ScheduledTaskInfo` |

## 로컬 개발

```bash
py -3 -m venv .venv && .venv\Scripts\pip install -r requirements.txt pytest ruff
```
