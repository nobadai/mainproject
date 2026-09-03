"""FastAPI 앱.

/health 는 배포 스크립트와 컨테이너 HEALTHCHECK 가 호출하는 엔드포인트입니다.
앱을 확장하더라도 이 경로는 유지하세요 — 배포 성공 판정의 기준입니다.
"""

import os
from functools import partial

from fastapi import FastAPI

from app.critic.router import router as critic_router
from app.finance.adapter import finance_port
from app.finance.router import router as finance_router
from app.logistics.adapter import logistics_port
from app.logistics.router import router as logistics_router
from app.master.router import router as master_router
from app.master.wiring import register as register_agent
from app.purchase_agent.adapter import purchase_port
from app.purchase_agent.quotes import auction_quote_source
from app.sales.router import router as sales_router

app = FastAPI(title="mainproject")
app.include_router(finance_router)
app.include_router(logistics_router)
app.include_router(master_router)

# 마스터가 부를 수 있는 대상은 **런타임에 등록된 것뿐**이다 (wiring).
#
# ★ 미등록은 오류가 아니라 "오늘 그 부서가 돌지 않는다"와 같은 상태다 —
#   마스터가 E4_NOT_STARTED + missing_adapters 를 낸다 (정의서 §5.3).
#
# ★ 세 파트가 다 등록됐다 (2026-08-27). 이제 미등록으로 멈추지 않고, 각 부서가
#   **무엇이 없어서 못 도는지**를 자기 missing_data 로 답한다. 둘은 다르다 —
#   앞은 배선 문제이고 뒤는 그날의 사실이다.
register_agent("finance", finance_port)
register_agent("inventory", logistics_port)
# 🔴 **실 경락가를 꽂는다** (2026-09-03). 기본값 mock 으로 두면 매입이 안을 못 낸다.
#
#   mock 단가는 실 ML 예측에서 나온 상한을 못 넘는다 — 두 값의 출처가 달라서다.
#
#       max_price          실 ML 예측 q90       배추 992 · 무 795
#       grade_unit_price   mock                 배추 1,650 · 무 1,100
#
#   그래서 self_check 가 전부 컷하고 `no_proposal_reason` 만 남았다. 실측으로
#   같은 payload 를 두 시세로 돌려 확인했다.
#
#       mock      배추 0안 · 무 0안   business=skipped
#       실 경락가  배추 2안 · 무 2안   business=ok
#
# ★ **mock 이 틀린 값이라서가 아니라 섞이면 안 되는 값이라서다.** 예측은 실데이터인데
#   단가만 연습값이면 그 둘을 비교한 판정은 아무 뜻이 없다.
#
# ⚠️ ML `current_price` 와 매입 물량가중 시리즈가 일치하지 않는 것은 여전히 미결이다
#   (2026-08-31 실측 · 배추 812 vs 933). 다만 그것은 **두 값을 어떻게 병기해 보여줄지**의
#   문제이고, 매입단가로 무엇을 쓸지는 아니다 — 매입단가는 실제로 살 때 낼 돈이다.
register_agent("purchase", partial(purchase_port, quotes=auction_quote_source()))
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
#   🔴 **`app/orchestrator/` 폴더도 남는다.** `contracts_core.py` 는 재무·물류·
#   매입·Critic·마스터가 전부 쓰는 **공용 계약**이다 (`Evidence`·`EndCode`·
#   `ContractViolation`). 폴더 이름만 오케일 뿐 내용은 공용이라, 중립 위치로
#   옮기는 것은 저장소 전체의 import 를 건드리는 별도 작업이다
#   (`docs/260830_오케_Critic_정리안.md`).


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return {
        "message": "mainproject is running",
        "version": os.getenv("APP_VERSION", "dev"),
    }
