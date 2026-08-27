"""마스터 에이전트 API 라우터.

정의서 v2.2 §3.1 — 진입점은 **사용자 요청**과 **ML 완료 Trigger** 둘이다.

★ 마스터는 도메인 DB 를 읽지 않는다 (§3.2.5). 각 에이전트가 자기 Tool 로 조회하고,
  마스터는 요청 본문과 실행 이력만 다룬다.
"""

from fastapi import APIRouter, HTTPException, status

from app.master.schemas import (
    ProcurementRunRequest,
    ProcurementRunResponse,
    RunHistoryOut,
    TriggerAck,
)
from app.master.service import get_run_history, run_procurement
from app.orchestrator.contracts_core import ContractViolation

router = APIRouter(prefix="/master", tags=["master"])


@router.post(
    "/request",
    response_model=ProcurementRunResponse,
    summary="매입 의사결정 Flow 실행",
)
def master_request(request: ProcurementRunRequest) -> ProcurementRunResponse:
    """재무·물류 경계 수집 → 매입 시나리오 → 재검증 → 사용자 선택지.

    **실패도 200 으로 돌려준다.** 부서 미가동·보류·반려는 오류가 아니라 **그날의 결과**이며
    종료 코드로 구분된다 (§5.3). 400/422 는 요청 자체가 계약을 어긴 경우다.
    """
    try:
        return run_procurement(request)
    except ContractViolation as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error


@router.post(
    "/trigger",
    response_model=TriggerAck,
    summary="ML 예측 완료 이벤트 수신",
)
def master_trigger(request: ProcurementRunRequest) -> TriggerAck:
    """ML 파이프라인이 "오늘 예측·적재가 끝났다"를 알린다.

    ★ 마스터는 **ML 을 호출하지 않는다.** 완료 신호만 받는다 (§3.1).

    ★ **예측값은 마스터가 실어 준다** (§3.2.5 예외 · 매입 파트 지적으로 뒤집음).
      ML 은 매입의 도메인이 아니라 매입이 직접 읽으면 §1.2-9 를 어기고, ML 은 호출
      구조 밖이라 §4.1 의 "해당 에이전트에게 요청"도 성립하지 않는다. 대신 마스터가
      `generated_at` 을 `as_of` 와 대조한다 (`ProcurementFlow._forecast_is_clean`).

    ⚠️ **지금은 동기로 즉시 실행한다.** 회의 3.1 이 요구한 Queue·비동기는 별도 이슈다.
      그래서 `note` 가 항상 `executed` 이며, 큐가 붙으면 `queued` 가 나온다.
    """
    payload = request.model_copy(update={"trigger": "ML_COMPLETE"})
    result = run_procurement(payload)
    return TriggerAck(
        accepted=True,
        request_id=result.request_id,
        as_of=result.as_of,
        note="executed",
    )


@router.get(
    "/runs/{request_id}",
    response_model=RunHistoryOut,
    summary="실행 이력 조회 — 그 요청이 어떻게 됐나",
)
def master_run_history(request_id: str) -> RunHistoryOut:
    """업무 키(`REQ-20260827-0001`)로 실행 계획과 요청·응답 원문을 돌려준다.

    ★ **검증 Tool 의 ④ 실행 계획 온전성 검사(M-16)가 읽는 경로**이기도 하다 (§3.7.4).
      `plan` 은 응답 원문 안이 아니라 별도 컬럼에서 오므로, 응답 스키마가 바뀌어도
      검증이 따라 흔들리지 않는다.
    """
    try:
        return get_run_history(request_id)
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
