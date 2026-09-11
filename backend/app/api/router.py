"""화면용 API — `/api` 아래 전부.

★ **기존 부서 라우터와 이름이 겹칩니다. 주소로 가릅니다.**

    /finance/agent        에이전트를 돌린다      (기존 · 이 폴더가 아님)
    /api/finance          화면에 값을 준다       (여기)

  `/api` 로 시작하면 화면용입니다. 규칙은 이 하나뿐입니다.

★ **`/api` 아래는 GET 만 둡니다.** 화면은 읽기만 합니다. 쓰기는 기존
  경로가 합니다 (`POST /master/…/decision` 처럼). 그래서 이 폴더의 코드가
  실수로 데이터를 건드릴 길이 아예 없습니다.

★ **왜 따로 있나.** 기존 라우터 36개 중 GET 은 12개인데 전부
  `runs/{run_id}` 계열입니다. 화면은 `run_id` 를 모릅니다 — **날짜** 하나만
  압니다. "1월 6일 현금 잔액 얼마?" 를 물을 자리가 없어서 새로 만듭니다.

폴더 하나 = 부서 하나입니다. 각자 자기 폴더의 `query.py` 만 고칩니다.
"""

from fastapi import APIRouter

from app.api.dashboard.routes import router as dashboard_router
from app.api.finance.routes import router as finance_router
from app.api.finance.console_routes import router as finance_console_router
from app.api.forecast.routes import router as forecast_router
from app.api.logistics.routes import router as logistics_router
from app.api.purchase.routes import router as purchase_router
from app.api.sales.routes import router as sales_router
from app.api.sales.console_routes import router as sales_console_router

router = APIRouter(prefix="/api")

for _child in (
    dashboard_router,
    forecast_router,
    purchase_router,
    finance_router,
    logistics_router,
    sales_router,
    finance_console_router,
    sales_console_router,
):
    router.include_router(_child)
