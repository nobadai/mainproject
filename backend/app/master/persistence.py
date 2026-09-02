"""
persistence.py — 마스터 실행 계획 적재 (정의서 §1.2-11)

★ 계산과 적재를 섞지 않는다.
  `flow.py` 는 DB 를 모르고, `service.py` 는 경계 변환만 한다. 여기서만 저장한다.

★ 적재 실패가 응답을 막지 않는다.
  이력이 없는 것보다 결과를 못 주는 것이 나쁘다 — `try_save_run` 이 삼킨다.

★ 마스터는 UUID 가 아니라 **업무 키**(`REQ-20260827-0001`)로 조회된다.
  사용자가 "그 요청 어떻게 됐냐"고 묻는 단위가 `request_id` 이기 때문이다.

★ **표는 `master_agent_runs` 다** (2026-09-02 이전). 옛 `orchestrator_agent_runs` 는
  오케 · Critic 과 함께 쓰던 표라 어휘의 소유가 없었다 - 조회(`STATUS`)를 이력에
  남기려 해도 남의 행의 뜻까지 건드려야 해서 지금까지 안 적어 왔다.
  Critic 은 옛 표를 그대로 쓴다.

★ `item` · `end_code` 를 컬럼으로도 넘긴다.
  payload 안에도 있지만 "배추가 며칠째 E2 인가" 를 JSONB 를 파지 않고 보기 위해서다.
  **꺼내는 것은 여기서 한다** - 저장소가 payload 모양을 알면 응답 스키마가 바뀔 때마다
  적재가 흔들린다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import date
from typing import Any

from app.master.plan import ExecutionPlan
from app.master.run_repository import try_save_run
from app.master.schemas import ProcurementRunRequest, ProcurementRunResponse
from app.master.status_flow import StatusOutcome

# 마스터의 1차 Flow 는 매입 의사결정이다. 판매(2차)가 붙으면 cycle 이 갈린다.
_CYCLE = "PROCUREMENT"

# 조회. 안을 만들지 않지만 예산을 쓰고 부서를 부르므로 이력에 남는다 (2026-09-02).
_STATUS_CYCLE = "STATUS"

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
) -> str | None:
    """실행 1건을 적재하고 **그 행의 id 를 돌려준다.** 실패해도 조용히 넘어간다.

    ★ **전에는 이 값을 버렸다** (2026-08-30 까지). `try_save_run` 이 `run_id` 를
      돌려주는데 받지 않았고, 그래서 응답에 실을 수가 없었다. 결정은 업무 키까지만
      가리켰고, 한 업무 키에 실행이 75행이면 **어느 실행을 승인한 것인지 알 수 없었다.**

    ★ 적재에 실패하면 `None` 이다 — 그때는 결정이 실행을 못 가리킨다. 그것도 사실이라
      숨기지 않는다 (`master_decisions.run_id` 가 NULL 을 허용하는 이유).

    ⚠️ 저장되는 `response_payload` 에는 이 값이 없다. 값이 **적재 후에** 나오기
      때문이다. 두 번 쓰지 않는다 — 행의 `run_id` 가 이미 그 답이다.
    """
    run_id = try_save_run(
        cycle=_CYCLE,
        as_of=response.as_of,
        request_id=response.request_id,
        item=request.item,
        end_code=response.end_code,
        runtime_status=runtime_status_of(response.end_code),
        elapsed_ms=elapsed_ms,
        plan=plan_rows(response),
        request_payload=request.model_dump(mode="json"),
        response_payload=response.model_dump(mode="json"),
    )
    return None if run_id is None else str(run_id)


def status_plan_rows(plan: ExecutionPlan) -> list[dict[str, Any]]:
    """조회의 실행 계획을 JSONB 모양으로.

    ★ 매입 쪽(`plan_rows`)은 응답 스키마(`StepOut`)를 거치는데 여기는 계획 객체를
      바로 쓴다 - 조회 응답에는 계획이 안 실리기 때문이다. **그래서 더 중요하다.**
      화면에 안 보이는 호출이라 이력이 유일한 기록이다.

    ★ 시각을 담지 않는다. `ExecutionStep` 에 시계가 없다는 것은
      `test_계획에_실행_시각이_없다` 가 잠근다.
    """
    return [asdict(step) for step in plan.steps]


def record_status(
    *,
    request_id: str,
    as_of: date,
    policy_version: str,
    intent: Mapping[str, Any],
    outcome: StatusOutcome,
    elapsed_ms: int | None = None,
) -> str | None:
    """조회 1건을 적재한다 (2026-09-02 신설).

    🔴 **왜 조회도 남기는가.**
      조회는 안을 만들지 않지만 **예산을 쓰고 부서를 부른다.** 안 남기면 그 호출이
      이력에서 사라지고, 검증 6계열의 M-16 이 막으려는 것이 정확히 "안 보이는
      호출" 이다. 조회만 계속 돌린 날과 아무것도 안 한 날이 같아 보이면 안 된다.

    ★ **전에는 표가 못 받았다.** 옛 `orchestrator_agent_runs` 의 `cycle` CHECK 에
      `STATUS` 가 없었고, 어휘를 고치려면 오케·Critic 행의 뜻까지 건드려야 했다.
      마스터가 자기 표로 나오면서(2026-09-02) 그 장애물이 없어졌다.

    ★ **`end_code` 에 S 코드가 들어간다.** 매입은 `E1`~`E5`, 조회는
      `S1_ANSWERED`~`S3_UNAVAILABLE` 이다. 컬럼을 CHECK 로 안 닫은 이유가 이것이고,
      뜻은 둘 다 "이 실행이 어떻게 끝났나" 로 같다.

    ★ **품목이 없다.** 조회는 품목 축이 아니라 부서 축이다 - 무엇을 물었는지는
      `request_payload` 의 `agents` 에 남는다. 없는 것을 지어내지 않는다.

    ⚠️ **업무 키가 매입과 겹칠 수 있다.** 둘 다 `make_request_id(as_of)` 를 쓴다.
      그래서 읽는 쪽이 `cycle` 을 밝히게 했다 (`get_run_by_request_id`) - 안 그러면
      조회가 최신 행이 되는 날 결정이 조회를 가리키고 이력 화면이 조회를 보여준다.
    """
    run_id = try_save_run(
        cycle=_STATUS_CYCLE,
        as_of=as_of,
        request_id=request_id,
        end_code=outcome.status_code,
        runtime_status=outcome.runtime_status,
        elapsed_ms=elapsed_ms,
        plan=status_plan_rows(outcome.plan),
        request_payload={
            "as_of": as_of.isoformat(),
            "policy_version": policy_version,
            "intent": dict(intent),
        },
        response_payload={
            "status_code": outcome.status_code,
            "reason": outcome.reason,
            "answers": {k: dict(v) for k, v in outcome.answers.items()},
            "unavailable": list(outcome.unavailable),
            "missing_data": {k: list(v) for k, v in outcome.missing_data.items()},
            "errors": dict(outcome.errors),
        },
    )
    return None if run_id is None else str(run_id)
