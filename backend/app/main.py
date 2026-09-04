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
from app.finance.transition import FinanceTransitionAdapter
from app.logistics.adapter import logistics_port
from app.logistics.router import router as logistics_router
from app.logistics.transition import LogisticsTransitionAdapter
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.router import router as master_router
from app.master.transition import register_transition
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

# ── 승인 → 장부 상태전이 (C 형태 ⑦) ────────────────────────────────────
#
# 🔴 **호출이 0건이었다.** 어댑터는 재무·물류 양쪽에 다 섰는데 등록하는 줄이 없어
#    `apply_approval` 이 매 승인마다 *"상태전이 미등록"* 으로 돌아섰다 — 사람이
#    승인해도 장부가 안 바뀌었다. 배선은 마스터 몫이고 그 자리가 여기다.
#
# ★ **위의 `register_agent` 와 다른 등록소다.** 저쪽은 **부를 대상**(에이전트)을,
#   이쪽은 **장부를 바꿀 방법**을 담는다. 한 사전에 섞으면 "어댑터가 없다" 와
#   "전이가 없다" 가 같은 문장으로 나가는데, 둘은 다른 사실이다.
#
# 🔴 **`sim_run_id` 는 마스터가 정한다.** `persist_inventory` 의 WHERE 가 그 값을
#    쓰지만 *"어느 실행의 장부인가"* 는 물류 사실이 아니다. 물류 모듈에 상수로
#    박으면 실행이 둘이 되는 날 물류 코드를 고쳐야 하므로 여기서 눈에 보이게 준다.
#    값의 주인은 `ledger_repository.BURN_IN_SIM_RUN_ID` 하나이고, 매입 원장
#    (`ledger.sim_run_id_for`)도 같은 상수를 가리킨다 — 새로 만들지 않는다.
register_transition("finance", FinanceTransitionAdapter())
register_transition("logistics", LogisticsTransitionAdapter(sim_run_id=BURN_IN_SIM_RUN_ID))

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
