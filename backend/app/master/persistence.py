"""
persistence.py — 마스터 실행 계획 적재 (정의서 §1.2-11)

★ 계산과 적재를 섞지 않는다.
  `flow.py` 는 DB 를 모르고, `service.py` 는 경계 변환만 한다. 여기서만 저장한다.

★ 적재 실패가 응답을 막지 않는다.
  이력이 없는 것보다 결과를 못 주는 것이 나쁘다 — `try_save_run` 이 삼킨다.

★ 마스터는 UUID 가 아니라 **업무 키**(`REQ-20260827-0001`)로 조회된다.
  사용자가 "그 요청 어떻게 됐냐"고 묻는 단위가 `request_id` 이기 때문이다.
"""

from __future__ import annotations

from typing import Any

from app.master.schemas import ProcurementRunRequest, ProcurementRunResponse
from app.orchestrator.run_repository import try_save_run

# 마스터의 1차 Flow 는 매입 의사결정이다. 판매(2차)가 붙으면 cycle 이 갈린다.
_CYCLE = "PROCUREMENT"

# 종료 코드 → 런타임 상태. 표의 CHECK 어휘가 3값이라 여기서 접는다.
_RUNTIME_BY_END_CODE = {
    "E4_NOT_STARTED": "RUNTIME_NOT_READY",
}


def runtime_status_of(end_code: str) -> str:
    """`E4` 만 미가동이다.

    `E2`(보류)·`E3`(반려)·`E5`(계획 없음)는 **돌긴 돈** 날이다 — 회사 상태이지
    실행 환경 문제가 아니다. 이 구분이 무너지면 "부서가 죽은 날"과 "부서가 반대한 날"이
    이력에서 같아 보인다.
    """
    return _RUNTIME_BY_END_CODE.get(end_code, "READY")


def plan_rows(response: ProcurementRunResponse) -> list[dict[str, Any]]:
    """실행 계획을 JSONB 로 저장할 모양으로.

    ★ 시각을 담지 않는다. 계획은 **같은 입력에 같은 값**이어야 한다 (§1.2-11).
      언제 돌았는지는 행의 `created_at` 이 답한다.
    """
    return [step.model_dump(mode="json") for step in response.plan]


def record(
    request: ProcurementRunRequest,
    response: ProcurementRunResponse,
    *,
    elapsed_ms: int | None = None,
) -> None:
    """실행 1건을 적재한다. 실패해도 조용히 넘어간다."""
    try_save_run(
        agent="master",
        cycle=_CYCLE,
        as_of=response.as_of,
        request_id=response.request_id,
        runtime_status=runtime_status_of(response.end_code),
        elapsed_ms=elapsed_ms,
        plan=plan_rows(response),
        request_payload=request.model_dump(mode="json"),
        response_payload=response.model_dump(mode="json"),
    )
