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

등록 도중 나오는 질문들의 답은 아래 "서비스로 등록"을 참고하세요.

### 4. 실행 방식 — 임시 실행 vs 서비스

**임시 실행 (디버깅용).** 배포 로그가 창에 실시간으로 찍혀서 첫 설정이나
문제 추적에 유용합니다. 창을 닫거나 로그아웃하면 러너가 죽습니다.

```powershell
.\run.cmd
```

**서비스로 등록 (상시 운영).** 로그인하지 않아도, 재부팅 후에도 동작합니다.
Windows 러너는 별도 `svc` 스크립트가 아니라 `config.cmd` 등록 과정에서
서비스 여부를 정합니다. 이미 비서비스로 등록했다면 지우고 다시 등록하세요.

```powershell
.\config.cmd remove
.\config.cmd --url https://github.com/<사용자명>/mainproject --token <새토큰>
```

질문에 이렇게 답합니다.

| 질문 | 답 |
| --- | --- |
| runner group / name / work folder | Enter (기본값) |
| `Run the runner as service?` | **`Y`** |
| `User account to use for the service` | `<컴퓨터명>\<관리자계정>` |
| `Password` | 해당 계정 비밀번호 |

> **서비스 계정은 반드시 관리자여야 합니다.** 기본값인
> `NT AUTHORITY\NETWORK SERVICE`는 관리자가 아니라서 `deploy.ps1`의
> 스케줄드 태스크 등록(`Register-ScheduledTask`)이 권한 오류로 실패합니다.
> 계정명은 `whoami`, 관리자 여부는 `Get-LocalGroupMember -Group Administrators`로 확인하세요.

정상 등록되면 `Get-Service actions.runner.*`가 `Running`이고,
GitHub의 Runners 목록에 **Idle(초록색)** 로 표시됩니다.

### 5. 대상 PC 사전 요구사항

배포 스크립트가 쓰는 도구들입니다. 미리 설치돼 있어야 합니다.

```powershell
winget install --id Python.Python.3.12 -e --scope machine
winget install --id Git.Git -e --scope machine
```

`--scope machine`이 중요합니다. 사용자 단위로 설치하면 서비스 계정의 PATH에서
파이썬이 보이지 않아 배포 중 `py: 명령을 찾을 수 없음`으로 실패합니다.

설치 후 `py -3 --version`, `git --version`이 동작하는지 확인하세요.
러너는 **시작 시점의 PATH를 물고 있으므로**, 파이썬을 러너보다 나중에 깔았다면
러너를 재시작해야 새 PATH가 반영됩니다.

**설치할 필요 없는 것:**

- **PowerShell 7 (`pwsh`)** — 워크플로가 Windows 기본 탑재인 PowerShell 5.1만 씁니다
- **실행 정책 변경** — 배포 단계를 cmd를 거쳐 호출하고 `-ExecutionPolicy Bypass`를
  넘기므로, 대상 PC가 기본값 `Restricted`여도 그대로 동작합니다.
  시스템 보안 설정을 바꿀 필요가 없습니다

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
| 배포 job이 계속 대기 중 | 러너가 **Offline**이 아닌지. `run.cmd` 창이 닫혔거나 서비스가 멈추면 job은 에러 없이 최대 24시간 큐에 머뭅니다 |
| `pwsh: command not found` | 워크플로가 `shell: pwsh`를 쓰고 있는 것. 대상 PC에 PowerShell 7이 없으므로 cmd 경유 방식을 써야 합니다 |
| `running scripts is disabled on this system` | 실행 정책 문제. 배포 단계가 cmd를 거쳐 `-ExecutionPolicy Bypass`로 호출되는지 확인 |
| `py: 명령을 찾을 수 없음` | 파이썬 설치 후 러너를 재시작했는지, `--scope machine`으로 설치했는지 |
| `Register-ScheduledTask` 액세스 거부 | 러너가 관리자 권한으로 실행 중인지 (서비스면 계정이 Administrators 그룹인지) |
| 헬스체크 실패 | `C:\apps\mainproject\logs\` 의 최신 로그 확인 |
| 앱은 뜨는데 외부 접속 불가 | 방화벽 인바운드 규칙, 앱이 `0.0.0.0`에 바인딩되는지 |
| 스케줄드 태스크 상태 확인 | `Get-ScheduledTask -TaskName app-mainproject \| Get-ScheduledTaskInfo` |

## 로컬 개발

```bash
py -3 -m venv .venv && .venv\Scripts\pip install -r requirements.txt pytest ruff
```
