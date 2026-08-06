# mainproject

FastAPI 앱을 컨테이너로 빌드해, 사내/집 Windows PC에 self-hosted 러너로 배포합니다.

## 구조

| 경로 | 역할 |
| --- | --- |
| `.github/workflows/ci-cd.yml` | CI → 이미지 빌드 → 배포 파이프라인 |
| `Dockerfile` | 앱 이미지 정의 (python:3.12-slim + uvicorn) |
| `deploy/deploy.ps1` | 대상 PC에서 컨테이너를 교체하는 스크립트 |
| `app/main.py` | FastAPI 앱. `/health`는 배포 판정 기준이므로 유지하세요 |
| `requirements.txt` | 런타임 의존성 — 이미지에 설치됨 |
| `requirements-dev.txt` | 개발/CI 도구 — 이미지에 들어가지 않음 |

## 배포 흐름

```
main 에 push
  ├─ ci     (ubuntu)  ruff + pytest
  ├─ build  (ubuntu)  이미지 빌드 → ghcr.io/<owner>/mainproject:<sha> push
  └─ deploy (대상 PC) pull → 컨테이너 교체 → /health 폴링 → 실패 시 롤백
```

이미지는 **GitHub 클라우드에서 빌드**되므로 대상 PC에는 빌드 도구가 필요 없습니다.
대상 PC는 완성된 이미지를 받아 실행만 합니다.

배포된 컨테이너는 `--restart unless-stopped` 로 뜨므로, Docker Desktop이 살아 있는 한
재부팅 후에도 자동으로 복구됩니다.

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

| 필요 | 용도 |
| --- | --- |
| **Docker Desktop** (실행 중) | 컨테이너 실행 |
| **Git** | `actions/checkout` |

```powershell
winget install --id Git.Git -e --scope machine
```

Docker Desktop은 [docker.com](https://www.docker.com/products/docker-desktop/)에서 설치하고,
설정에서 **Start Docker Desktop when you log in**을 켜두세요.

> **러너는 시작 시점의 PATH를 물고 있습니다.** Docker Desktop을 러너보다 나중에
> 설치했다면 러너를 재시작해야 `docker` 명령을 찾습니다.

**설치할 필요 없는 것:**

- **Python** — 앱이 컨테이너 안에서 돌므로 호스트에는 필요 없습니다
- **PowerShell 7 (`pwsh`)** — 워크플로가 Windows 기본 탑재인 PowerShell 5.1만 씁니다
- **실행 정책 변경** — 배포 단계를 cmd를 거쳐 호출하고 `-ExecutionPolicy Bypass`를
  넘기므로, 대상 PC가 기본값 `Restricted`여도 그대로 동작합니다

> **Docker Desktop은 서비스가 아니라 사용자 애플리케이션입니다.** 로그인한 사용자
> 세션에서 실행 중이어야 데몬에 접근할 수 있습니다. 아무도 로그인하지 않은 상태로
> 무인 운영하려면 러너를 WSL2 안에 두거나 대상 PC를 Linux로 바꾸는 편이 낫습니다.

### 6. 방화벽

앱 포트(기본 8000)를 다른 기기에서 접속하려면 인바운드 허용이 필요합니다.
같은 PC에서만 쓸 거라면 생략하세요.

```powershell
New-NetFirewallRule -DisplayName "mainproject 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

## 설정 바꾸기

컨테이너 이름과 포트는 워크플로 `deploy` job의 `env:` 블록에서 조정합니다.

```yaml
env:
  APP_NAME: mainproject
  APP_PORT: "8000"
```

## 운영 명령

대상 PC에서 쓰는 명령들입니다.

```powershell
docker ps --filter name=mainproject          # 상태 확인
docker logs -f mainproject                   # 로그 실시간 확인
docker restart mainproject                   # 재시작
```

수동 롤백은 이전 커밋 SHA 태그로 다시 띄우면 됩니다.

```powershell
docker rm --force mainproject
docker run -d --name mainproject --restart unless-stopped -p 8000:8000 ghcr.io/<owner>/mainproject:<이전SHA>
```

## 문제 해결

| 증상 | 확인할 것 |
| --- | --- |
| 배포 job이 계속 대기 중 | 러너가 **Offline**이 아닌지. `run.cmd` 창이 닫혔거나 서비스가 멈추면 job은 에러 없이 최대 24시간 큐에 머뭅니다 |
| `docker: command not found` | Docker Desktop 설치 후 러너를 재시작했는지 |
| `error during connect` / 데몬 연결 실패 | 대상 PC에 로그인된 상태로 Docker Desktop이 실행 중인지 |
| `denied` / `unauthorized` (pull 실패) | 워크플로 `deploy` job에 `packages: read` 권한과 GHCR 로그인 단계가 있는지 |
| `pwsh: command not found` | 워크플로가 `shell: pwsh`를 쓰고 있는 것. 대상 PC에 PowerShell 7이 없으므로 cmd 경유 방식을 써야 합니다 |
| `running scripts is disabled on this system` | 실행 정책 문제. 배포 단계가 cmd를 거쳐 `-ExecutionPolicy Bypass`로 호출되는지 확인 |
| 헬스체크 실패 | `docker logs mainproject` 확인. 실패 시 배포 로그에도 마지막 50줄이 출력됩니다 |
| 포트 충돌 | `Get-NetTCPConnection -LocalPort 8000 -State Listen` 로 점유 프로세스 확인 |
| 앱은 뜨는데 외부 접속 불가 | 방화벽 인바운드 규칙 |

## 로컬 개발

```powershell
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\ruff check .
.venv\Scripts\pytest -q
```

개발 서버 실행:

```powershell
.venv\Scripts\uvicorn app.main:app --reload
```
