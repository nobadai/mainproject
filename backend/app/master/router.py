"""마스터 에이전트 API 라우터.

정의서 v2.2 §3.1 — 진입점은 **사용자 요청**과 **ML 완료 Trigger** 둘이다.

★ 마스터는 도메인 DB 를 읽지 않는다 (§3.2.5). 각 에이전트가 자기 Tool 로 조회하고,
  마스터는 요청 본문과 실행 이력만 다룬다.
"""

from fastapi import APIRouter, HTTPException, status

from app.master.ask_schemas import AskExecuteRequest, AskRequest, AskResponse
from app.master.ask_service import ask as run_ask
from app.master.ask_service import execute as run_ask_execute
from app.master.decision import DecisionIn, DecisionOut, DecisionRejected
from app.master.decision_service import get_decisions, record_decision
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


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="발화문 입구 — 분류하고, 확인이 필요 없으면 조회까지",
)
def master_ask(request: AskRequest) -> AskResponse:
    """사용자의 말을 알아듣고 **무엇을 할지 정한다.** 마스터 역할 ①(요청 해석).

    ★ **분류와 실행이 다르다.** 확인이 필요하면 `CLASSIFIED_ONLY` 로 되묻고
      **아무것도 돌리지 않는다.** 200 으로 나가는 정상 경로다.

    ★ **바로 도는 것은 조회뿐이다.** 오분류 비용이 비대칭이라 그렇다 — 조회를 잘못
      고르면 다시 물으면 그만이지만, 매입은 호출 예산 12회와 매입 LLM 을 태운다.

    ★ **LLM 이 죽어도 200 이다.** 키·서버가 없으면 `llm_status=FALLBACK` 에
      `NEEDS_CLARIFICATION` 으로 되묻는다 — 브랜치만 받은 팀원 환경에서 깨지지 않는다.

    | outcome | 뜻 |
    |---|---|
    | `STATUS_ANSWERED` | 조회를 돌려 답을 담았다 |
    | `CLASSIFIED_ONLY` | 알아들었지만 확인이 필요해 실행하지 않았다 |
    | `NEEDS_CLARIFICATION` | 못 알아들었다 — 되묻는다 |
    """
    return run_ask(request)


@router.post(
    "/ask/execute",
    summary="확인한 의도를 실행 — 발화문을 다시 분류하지 않는다",
)
def master_ask_execute(request: AskExecuteRequest) -> AskResponse | ProcurementRunResponse:
    """`/ask` 가 돌려준 `intent` 를 **그대로** 보내 실행한다.

    ★ **재분류하지 않는다.** 다시 분류하면 사용자가 확인한 것과 다른 것이 돌 수 있고,
      그 순간 확인의 뜻이 사라진다. **본 것을 실행한다.**

    ★ 매입 실행은 기존 `/master/request` 와 **같은 Flow** 를 탄다 — 발화문 경로라고
      다른 조립을 두면 두 경로가 조용히 갈라진다 (구 백로그 B1-3 이 그 문제였다).

    ★ **`SELECT_SCENARIO` 는 `/runs/{id}/decision` 과 같은 규칙을 탄다.** 발화문
      경로라고 승인 기준을 따로 두면 화면에서 누른 승인과 말로 한 승인이 갈라진다.
      그래서 상태 코드도 같다 — 404 · 409 · 422.

      🔴 다만 **말에 없는 둘을 본문에 실어야 한다** — `target_request_id`(어느 실행의
      안인가)와 `decided_by`(누가 승인하는가). 없으면 422 다. 서버가 "가장 최근 실행"
      으로 추측하면 엉뚱한 날의 안을 승인할 수 있다.

    아직 배선되지 않은 종류는 501 이다 — `RERUN_WITH_CONDITION` 은 조건을 반영한
    `/master/request` 를 쓴다.
    """
    try:
        return run_ask_execute(request)
    except NotImplementedError as error:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(error),
        ) from error
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except DecisionRejected as error:
        raise HTTPException(
            status_code=(
                status.HTTP_409_CONFLICT if error.conflict else status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=str(error),
        ) from error


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


@router.post(
    "/runs/{request_id}/decision",
    response_model=DecisionOut,
    status_code=status.HTTP_201_CREATED,
    summary="사용자 결정 기록 — 승인 · 전체 거절 · 조건부 재요청",
)
def master_decide(request_id: str, body: DecisionIn) -> DecisionOut:
    """마스터가 제시한 안에 대한 **사람의 결정**을 적는다 (회의 미결정 12번).

    ★ **마스터 Flow 가 부를 수 없는 경로다.** 승인 게이트가 툴 목록 안에 있으면
      마스터가 스스로 통과시킬 수 있다 — 8/26 회의가 "툴 바깥에 두어 우회 불가하게"로
      정한 이유다. `flow.py` 는 이 모듈을 임포트하지 않는다.

    ★ **적재 실패를 삼키지 않는다** — `/request` 와 반대다. 실행 이력은 없어도 결과를
      줄 수 있지만, 결정이 안 남았는데 201 을 돌려주면 승인 없이 실행된 것과 같아진다.

    | 상태 | 언제 |
    |---|---|
    | 404 | 그 업무 키의 실행이 없다 |
    | 409 | 지금 상태에서 받을 수 없다 — `E4` 에 결정 · `E1` 아닌 날 승인 · 같은 안 재승인 |
    | 422 | 요청이 틀렸다 — 제시되지 않은 안 · 라벨/조건 누락 |
    """
    try:
        return record_decision(request_id, body)
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except DecisionRejected as error:
        raise HTTPException(
            status_code=(
                status.HTTP_409_CONFLICT if error.conflict else status.HTTP_422_UNPROCESSABLE_ENTITY
            ),
            detail=str(error),
        ) from error


@router.get(
    "/runs/{request_id}/decisions",
    response_model=list[DecisionOut],
    summary="결정 이력 — 번복도 지우지 않고 남는다",
)
def master_decision_history(request_id: str) -> list[DecisionOut]:
    """한 요청에 붙은 결정 전부. 오래된 것부터이며 최신 하나가 `is_current` 다.

    ★ 실행이 없어도 **빈 목록**을 돌려준다. 결정이 없는 것과 요청이 없는 것을 여기서는
      구분하지 않는다 — 그 구분은 `GET /master/runs/{request_id}` 가 404 로 답한다.
    """
    return get_decisions(request_id)
