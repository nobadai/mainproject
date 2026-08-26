"""FastAPI 앱.

/health 는 배포 스크립트와 컨테이너 HEALTHCHECK 가 호출하는 엔드포인트입니다.
앱을 확장하더라도 이 경로는 유지하세요 — 배포 성공 판정의 기준입니다.
"""

import os

from fastapi import FastAPI

from app.critic.router import router as critic_router
from app.finance.router import router as finance_router
from app.logistics.router import router as logistics_router
from app.master.router import router as master_router
from app.orchestrator.router import router as orchestrator_router
from app.sales.router import router as sales_router

app = FastAPI(title="mainproject")
app.include_router(finance_router)
app.include_router(logistics_router)
app.include_router(master_router)
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
