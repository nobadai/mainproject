"""FastAPI 앱.

/health 는 배포 스크립트와 컨테이너 HEALTHCHECK 가 호출하는 엔드포인트입니다.
앱을 확장하더라도 이 경로는 유지하세요 — 배포 성공 판정의 기준입니다.
"""

import os

from fastapi import FastAPI

from app.critic.router import router as critic_router
from app.finance.router import router as finance_router
from app.logistics.router import router as logistics_router
from app.master.adapters.finance import finance_port
from app.master.adapters.logistics import logistics_port
from app.master.router import router as master_router
from app.master.wiring import register as register_agent
from app.orchestrator.router import router as orchestrator_router
from app.purchase_agent.adapter import purchase_port
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
register_agent("purchase", purchase_port)
app.include_router(orchestrator_router)
app.include_router(critic_router)
app.include_router(sales_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return {
        "message": "mainproject is running",
        "version": os.getenv("APP_VERSION", "dev"),
    }
