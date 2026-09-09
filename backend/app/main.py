"""FastAPI 앱.

/health 는 배포 스크립트와 컨테이너 HEALTHCHECK 가 호출하는 엔드포인트입니다.
앱을 확장하더라도 이 경로는 유지하세요 — 배포 성공 판정의 기준입니다.
"""

import os

from fastapi import FastAPI

from app.api.router import router as screen_router
from app.finance.router import router as finance_router
from app.logistics.router import router as logistics_router
from app.master.bootstrap import wire_registries
from app.master.critic.router import router as critic_router
from app.master.router import router as master_router
from app.ml.console_proxy import router as ml_console_router
from app.ml.router import router as ml_router
from app.sales.router import router as sales_router

app = FastAPI(title="mainproject")
# 화면용 API (`/api/…`). **부서 라우터와 주소로 가른다** — `/finance/agent` 는
# 에이전트를 돌리고, `/api/finance` 는 화면에 값을 준다. `/api` 아래는 GET 뿐이다.
app.include_router(screen_router)
# ML 예측 API. **여태 안 붙어 있어서 `/ml/forecast` 가 404 였다** (2026-09-08 발견).
# 매입은 `app.ml.service` 를 파이썬으로 직접 불러 써서 아무도 모르고 있었다.
app.include_router(ml_router)
# ML 운영 콘솔(`/ml/console/…`). **우리 ML 백엔드로 넘기는 프록시다** —
# 재학습·에이전트는 학습 꾸러미가 있는 곳에서만 돌 수 있다.
app.include_router(ml_console_router)
app.include_router(finance_router)
app.include_router(logistics_router)
app.include_router(master_router)

# ── 등록소를 채운다 ────────────────────────────────────────────────────
#
# 🔴 **등록 줄은 여기 없다. `app/master/bootstrap.py` 하나에 있다** (2026-09-09).
#
#   전에는 이 자리에 register_agent · register_transition · register_day_opening ·
#   register_cancellation · register_inbound · register_collection 이 모듈 수준으로
#   늘어서 있었다. 그러면 **이 모듈을 임포트한 사람만** 등록된 세상을 본다.
#
#   ```text
#   FastAPI      app/main.py 를 임포트한다        → 등록소가 찬다
#   CLI 걷기      app/master/backtest_runner.py    → 안 거친다 → 전부 빈 채로 걸었다
#   ```
#
#   ⚠️ 실측으로 5일을 걸으니 5일 다 사고였다 — *"하루 넘김 미등록: finance,
#     logistics"*. 러너는 미등록을 이름까지 정확히 말했고, 고칠 곳은 조립 자리였다.
#
# ★ **임포트 시점에 부르는 것은 그대로다.** 옮긴 것은 자리뿐이고 시점도 대상도
#   안 바꿨다. 무엇을 왜 등록하는지는 그 파일의 각 줄 위에 그대로 있다.
wire_registries()


app.include_router(critic_router)
app.include_router(sales_router)

# ★ **오케스트레이터 라우터는 2026-08-30 에 걷어냈다.**
#
#   `/orchestrator/{procurement,sales,day,runs,runs/{id}}` 5개. 오케스트레이터가
#   마스터 에이전트가 되면서 대체됐고, 저장소 전체에서 **부르는 곳이 없었다** —
#   프론트도 테스트도 다른 파트도 안 썼다 (실측 2026-08-30).
#
#   🔴 **표는 그대로다.** `orchestrator_agent_runs` 는 오케·Critic·마스터가 함께
#   쓰는 실행이력이고, `agent` 축이 셋을 구분한다. 과거 행(agent='orchestrator'
#   21건)은 그때 실제로 있었던 일이라 지우거나 옮기지 않는다.
#
#   🟢 **`app/orchestrator/` 폴더는 2026-09-07 에 없어졌다** (지시).
#
#   전에는 이 자리에 *"폴더도 남는다 — `contracts_core.py` 가 다섯 파트의 공용
#   계약이라 중립 위치로 옮기는 것은 저장소 전체의 import 를 건드리는 별도 작업"*
#   이라고 적어 뒀다. 그 별도 작업이 두 판에 걸쳐 끝났다.
#
#   ```text
#   2026-09-03  공용 계약 → app/contracts/core.py   (재수출 shim 을 남겨 파트별 이전)
#   2026-09-07  나머지 전부 → app/master/           (shim 을 닫고 폴더를 지웠다)
#   ```
#
#   ⚠️ **표는 그대로다** — 위 문단의 `orchestrator_agent_runs` 는 안 건드렸다.
#   코드 경로만 옮겼다. `tests/master/test_orchestrator_is_gone.py` 가 폴더가
#   돌아오지 못하게 잠근다.


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return {
        "message": "mainproject is running",
        "version": os.getenv("APP_VERSION", "dev"),
    }
