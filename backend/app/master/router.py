"""마스터 에이전트 API 라우터.

정의서 v2.2 §3.1 — 진입점은 **사용자 요청**과 **ML 완료 Trigger** 둘이다.

★ 마스터는 도메인 DB 를 읽지 않는다 (§3.2.5). 각 에이전트가 자기 Tool 로 조회하고,
  마스터는 요청 본문과 실행 이력만 다룬다.
"""

from datetime import date

from fastapi import APIRouter, HTTPException, status

from app.contracts.core import ContractViolation
from app.master.ask_schemas import AskExecuteRequest, AskRequest, AskResponse
from app.master.ask_service import ask as run_ask
from app.master.ask_service import execute as run_ask_execute
from app.master.closing import ClosingOut
from app.master.closing import close_day as run_close_day
from app.master.collection import CollectionOut
from app.master.collection import collect_receipts as run_collect_receipts
from app.master.day_open import DayOpenOut
from app.master.day_open import open_day as run_open_day
from app.master.decision import CommitmentOut, DecisionIn, DecisionOut, DecisionRejected
from app.master.decision_service import current_commitment, get_decisions, record_decision
from app.master.holiday_calendar import get_calendar
from app.master.inbound import InboundOut
from app.master.inbound import receive_arrivals as run_receive_arrivals
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.receivable import ReceivableOut
from app.master.receivable import issue_receivables as run_issue_receivables
from app.master.schemas import (
    BurnInOut,
    ProcurementRunRequest,
    ProcurementRunResponse,
    ReportOut,
    RunHistoryOut,
    SalesRunRequest,
    SalesRunResponse,
    TriggerAck,
)
from app.master.service import (
    get_burn_in_history,
    get_run_history,
    get_run_report,
    run_procurement,
    run_sales,
)
from app.master.walk_report import WalkReport
from app.master.walk_report import walk_report as build_walk_report

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
    "/sales/run",
    response_model=SalesRunResponse,
    summary="판매 의사결정 Flow 실행",
)
def master_sales_run(request: SalesRunRequest) -> SalesRunResponse:
    """물류 초기 컨텍스트 → 판매 후보 → 후보별 검증 라우팅 → 사용자 선택지.

    **실패도 200 이다** — `/master/request` 와 같은 태도다. 후보 없음·전부 탈락·
    미시작·예산 소진은 오류가 아니라 **그날의 결과**이며 `SL1`~`SL5` 로 구분된다 (§5.3).

    🔴 **주말에도 돈다.** 개장 관문은 지나지만 실행일 관문은 **매입만** 지난다 —
      파는 데는 ML 예측이 필요 없다 (설계 §1).

    🔴 **`/master/sales/trigger`(비동기 ack)를 두지 않는다.** 매입의 그것은 ML 완료
      이벤트를 받는 스케줄러용이고, **판매는 사람이 눌러서 시작한다.** 부를 사람이
      없는 진입점을 만들면 어휘만 늘고 아무도 안 쓴다.

    ★ `trigger` 는 판매도 `USER_REQUEST` 가 기본이다. `ML_COMPLETE` 는 판매 경로에
      의미가 없지만 **어휘를 새로 만들지 않는다** — 쓰지 않는 값이 있는 것과 어휘가
      갈리는 것 중 뒤가 더 비싸다.
    """
    try:
        return run_sales(request)
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
      `generated_at` 을 `as_of` 와 대조한다 (`envelope.forecast_is_clean`).

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


@router.get(
    "/runs/{request_id}/report",
    response_model=ReportOut,
    summary="매입안 보고서 — 들고 나갈 수 있는 Markdown",
)
def master_run_report(request_id: str) -> ReportOut:
    """안마다 분할·조달·지급 일정과 근거를 편 문서.

    ★ **못 한 것을 같이 싣는다.** 지적·확인 필요·못 돈 검사·입력 출처가 안 옆에
      있어야 들고 나간 사람이 그 숫자를 어떻게 읽어야 하는지 안다.
      **결론만 담은 문서가 가장 위험하다.**
    """
    try:
        return get_run_report(request_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.get(
    "/burn-in",
    response_model=BurnInOut,
    summary="번인 구간 — 에이전트가 판단하기 전에 회사가 어떻게 왔나",
)
def master_burn_in() -> BurnInOut:
    """`sim_runs` 의 `BURN_IN` 한 건과 일별 마감 30일.

    🔴 **결론 옆에 경로를 두기 위한 것이다.** 에이전트가 12-31 에 *"살 안이 없다"*
      고 답하는데 그 앞 30일을 안 보면 시스템이 고장 난 것처럼 읽힌다.

    ★ 읽기 전용이다 — 하루를 진행시키는 것은 승인이 발주로 흘러가야 성립하고,
      그건 각 파트의 상태 전이 로직이다 (아직 없다 · 별도 이슈).
    """
    try:
        return get_burn_in_history()
    except LookupError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error


@router.get(
    "/walks/{sim_run_id}/report",
    response_model=WalkReport,
    summary="걷기 성적표 — 안 돈 날을 처음으로 셀 수 있다",
)
def master_walk_report(sim_run_id: str, start: date, end: date) -> WalkReport:
    """한 걷기가 범위 안에서 어떻게 갔나. **날마다 한 줄이고 빈 날도 한 줄이다.**

    🔴 **「행이 없다」를 두 가지로 가른다.** 표는 없는 것을 말할 수 없어서, 범위를
       받아 빈 날을 계산하고 실행일 여부로 그 뜻을 가른다.

    | 판정 | 뜻 |
    |---|---|
    | `WALKED` | 🟢 돌았다 |
    | `SKIPPED_OFF_DAY` | 🟢 안 도는 날이다 — 주말·공휴일 |
    | `NO_ROW_ON_EXECUTION_DAY` | 🔴 **실행일인데 행이 없다** |
    | `ROW_ON_OFF_DAY` | 🟡 안 도는 날인데 행이 있다 — 사고가 아니다 |
    | `UNKNOWN_CALENDAR` | ⚠️ 달력이 그 날을 안 덮어 모른다 |

    ★ **판정을 `end_code` 와 섞지 않았다.** `end_code` 는 *"그 판단이 어떻게
      끝났나"* 이고 판정은 *"그 날이 어떻게 됐나"* 다 — 축이 다르다.

    🔴 **`start` · `end` 가 필수다.** 기본값으로 "전체" 를 만들지 않는다 — 범위가
       없으면 빈 날을 계산할 수 없고, 그러면 `NO_ROW_ON_EXECUTION_DAY` 가 성립하지
       않는다.

    ★ **공휴일 달력을 여기서 붙인다.** 안 붙이면 설·추석이 실행일로 읽혀
      `NO_ROW_ON_EXECUTION_DAY` 가 되고, **없는 공백**이 화면에 뜬다.

    | 상태 | 언제 |
    |---|---|
    | 200 | 걷기가 있었다 · **행이 하나도 없었다** — 둘 다 사실이다 |
    | 400 | `end` 가 `start` 보다 앞이다 |

    🔴 **행이 0건이어도 404 가 아니다.** 404 로 내면 *"안 걸었다"* 와 *"그런 걷기가
       없다"* 가 같아진다 — 이 성적표가 가르려는 것이 정확히 그런 종류의 접힘이다.

    ⚠️ **한계 — 축이 NULL 인 행은 어느 성적표에도 안 나온다.** 실측(2026-09-09)으로
      이 표 1,322행 중 1,206행이 그것이고, 손으로 부른 것과 옛 실험이다. 감추는
      것이 아니라 **어느 걷기 것인지 표가 모른다.**
    """
    try:
        return build_walk_report(
            sim_run_id=sim_run_id,
            start=start,
            end=end,
            calendar=get_calendar(),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
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

    🔴 **승인은 이제 부서를 부른다** (M-4 · 2026-09-07). `APPROVE` 면 **그때 선택된
      1안만** 오늘 `as_of` 로 다시 검증하고, 그 결과가 응답의 `revalidation_outcome`
      에 실린다 (`revalidation.py`). 그래서 승인 응답이 **다른 결정보다 느리다** —
      개장 관문 한 번과 부서 호출 두어 번이 그 안에 있다.

      ```text
      PASSED       통과했다               CONDITIONAL  새 조건이 붙었다 — 화면이 다시 받는다
      FAILED       재검증에서 막혔다        ERROR        재검증 자체를 못 돌렸다
      ```

      ⚠️ **네 값 전부 201 이다.** 재검증이 막힌 것은 요청이 틀린 것도, 지금 상태에서
        결정을 못 받는 것도 아니다 — **사용자가 승인을 눌렀다는 사실**은 그대로 적힌다
        (설계 §0). 막혔다고 행을 안 쓰면 *"승인하려다 막혔다"* 가 사라진다.

      ⚠️ **`PASSED` 여도 아직 도메인 Write 가 안 흐른다** (M-5).

    | 상태 | 언제 |
    |---|---|
    | 404 | 그 업무 키의 실행이 없다 |
    | 409 | 지금 상태에서 받을 수 없다 — `E4` 에 결정 · `E1` 아닌 날 승인 · 같은 안 재승인 |
    | 422 | 요청이 틀렸다 — 제시되지 않은 안 · 라벨/조건 누락 |
    """
    try:
        # 🔴 **여기서 날짜를 안 정한다** (2026-09-09). 재검증이 설 날은 **그 실행의
        #    날**이고, 그것은 실행 이력 행이 들고 있다 — 진입점이 정하면 화면이 언제
        #    누르느냐에 따라 재검증이 딴 날로 돈다.
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
    "/runs/{request_id}/commitment",
    response_model=CommitmentOut,
    summary="현재 승인이 만든 확정 입고 약정 (H1)",
)
def master_commitment(request_id: str) -> CommitmentOut:
    """물류 H1 미래 점유의 입력이 되는 약정. 전달 방식 ⓐ — 물류 회신 2026-09-01 합의.

    | 상태 | 언제 |
    |---|---|
    | 200 | 현재 결정이 승인이다 — 못 만든 약정도 `buildable=false` 로 **사실대로** 나간다 |
    | 404 | 승인이 없다 — 결정이 없거나, 현재 결정이 거절·조건부 재요청이다 |

    ★ `buildable=false` 를 404 로 접지 않는다. *"승인이 없다"* 와 *"승인했는데 약정을
      못 만들었다"* 는 다른 사실이고, 부르는 쪽이 다르게 행동해야 한다 — 앞은 물류를
      부르지 않으면 되고, 뒤는 사람이 봐야 한다.
    """
    commitment = current_commitment(request_id)
    if commitment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"업무 키 {request_id} 에 유효한 승인이 없다 — 약정은 승인에서만 나온다.",
        )
    return commitment


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


@router.post(
    "/days/{as_of}/open",
    response_model=DayOpenOut,
    summary="하루를 연다 — 그날 상태 행을 파트마다 보장한다",
)
def master_open_day(as_of: date) -> DayOpenOut:
    """`as_of` 날 상태 행이 없으면 전날에서 물려받아 만든다.

    🔴 **명시적 호출이다. 실행의 부작용이 아니다.** `run_procurement` 이 시작할 때
       자동으로 열지 않는다 — 판단 한 번이 장부를 바꾸면 *"같은 as_of 로 백번 돌려도
       같은 답"* 이 깨진다. 하루가 넘어가는 것은 **사건**이고, 사건에는 자기 자리가 있다.

    ★ **멱등이다.** 같은 날을 두 번 열면 두 번째는 아무것도 안 한다 — 파트마다
      `opened` 가 빈 목록으로 나가는 것이 *"이미 열려 있었다"* 다.

    | 상태 | 언제 |
    |---|---|
    | 200 | 열었다 · 이미 열려 있었다 · 막혔다 · 미등록이다 — 전부 **그날의 사실**이다 |

    ★ **실패도 200 이다** (`/master/request` 와 같은 태도). 미등록·상한 초과는 오류가
      아니라 상태이고, 적재 실패는 롤백되어 어제 그대로다 — 사유가 본문에 실린다.
    """
    return run_open_day(as_of)


@router.post(
    "/days/{as_of}/receive",
    response_model=InboundOut,
    summary="그날 도착분을 받는다 — 개장 다음이고 판단과는 별개다",
)
def master_receive_arrivals(as_of: date) -> InboundOut:
    """`as_of` 에 도착 예정인 것을 파트마다 받는다.

    🔴 **왜 자기 엔드포인트인가** (물류 물음 2026-09-07).

      바로 위 `master_open_day` 가 적어 둔 원칙 그대로다 — *"명시적 호출이다. 실행의
      부작용이 아니다. 사건에는 자기 자리가 있다."* **입고도 사건이다.** 도착분을
      받으면 Receipt · Lot · Inventory Move 가 생기고 그것은 장부가 바뀌는 것이다.

    ★ **`run_procurement` 안으로 넣지 않는다.** 넣으면 판단 한 번이 재고를 늘리고,
      *"같은 `as_of` 로 백번 돌려도 같은 답"* 이 깨진다. 개장을 판단 밖에 둔 이유와
      같다.

    ⚠️ **상위 `run_day` 하나로 묶지도 않는다.** 물류가 그 안을 주셨는데(`B`), 묶으면
      **실패 조합을 한 응답으로 못 낸다.**

      ```text
      개장 성공 · 입고 BLOCKED · 판단 성공     ← 이 상태를 한 status 로 어떻게 적나
      ```

      `#316` 에서 `BLOCKED` 를 `NOTHING_DUE` 로 접었다가 물류가 잡아 준 것과 같은
      병이다. **사건 셋은 상태 셋이고, 순서는 부르는 쪽이 지킨다.**

    ★ **순서는 문장이 아니라 Gate 가 지킨다.** 안 열린 날 부르면 `NOT_OPENED` 로
      돌아서고 `next_action` 이 `OPEN_DAY_REQUIRED` 를 준다 — 전에는 docstring 에
      *"`open_day` 다음이다"* 라고만 적혀 있어 코드가 아무것도 안 봤다.

    ⚠️ **달력일이다.** 창고는 토요일에도 받는다. 그래서 이 호출은 실행일 판정을 안
      본다 — 토요일에 `open_day` · `receive` 는 돌고 `run_procurement` 만 안 돈다.

    | 상태 | 언제 |
    |---|---|
    | 200 | 받았다 · 받을 게 없었다 · 막혔다 · 안 열렸다 — 전부 **그날의 사실**이다 |

    ★ **실패도 200 이다** (`/days/{as_of}/open` 과 같은 태도). `FAILED` 는 롤백되어
      아무것도 안 바뀐 상태이고, 사유가 본문에 실린다.
    """
    return run_receive_arrivals(as_of)


@router.post(
    "/days/{as_of}/collect",
    response_model=CollectionOut,
    summary="그날 수금 사건을 반영한다 — 개장 다음이고 판단과는 별개다",
)
def master_collect_receipts(as_of: date) -> CollectionOut:
    """`as_of` 의 수금 사건을 파트마다 반영한다.

    🔴 **왜 자기 엔드포인트인가.**

      바로 위 두 형제가 적어 둔 원칙 그대로다 — *"명시적 호출이다. 실행의 부작용이
      아니다. 사건에는 자기 자리가 있다."* **수금도 사건이다.** 채권이 돈으로 들어오면
      `receivables` 와 `finance_states` 가 바뀌고 그것은 장부가 바뀌는 것이다.

    ★ **`run_procurement` 안으로 넣지 않는다.** 넣으면 **판단 한 번이 현금을 움직이고**
      *"같은 `as_of` 로 백번 돌려도 같은 답"* 이 깨진다. 개장·입고를 판단 밖에 둔
      이유와 같고, 현금이라 더 그렇다.

    🔴 **마스터는 수금 대상이나 수금액을 스스로 정하지 않는다** (재무 결정 2026-09-07).

      ```text
      Finance   실제 수금 사건의 정본 의미 — receivable_id · collection_date
                · cumulative target_received_total_krw · (sim_run_id, financing_mode)
      Master    날짜에 해당하는 사건을 **운반하고 호출**한다
      Sales     계약 결제조건·채권 발생의 상업적 원천
      ```

      ⚠️ **`due_date` 경과는 수금이 아니다.** 기일이 지난 것과 돈이 들어온 것을 접으면
        **없는 현금으로 매입 판단이 돈다.**

    ★ **순서는 문장이 아니라 Gate 가 지킨다.** 안 열린 날 부르면 `NOT_OPENED` 로
      돌아서고 `next_action` 이 `OPEN_DAY_REQUIRED` 를 준다 — `/days/{as_of}/receive`
      와 같은 모양이다.

    ⚠️ **달력일이다.** 입금은 토요일에도 찍힌다. 그래서 이 호출은 실행일 판정을 안
      본다 — 토요일에 `open_day` · `receive` · `collect` 는 돌고 `run_procurement` 만
      안 돈다.

    | 상태 | 언제 |
    |---|---|
    | 200 | 수금했다 · 들어올 게 없었다 · 막혔다 · 안 열렸다 — 전부 **그날의 사실**이다 |

    ★ **실패도 200 이다** (`/days/{as_of}/receive` 와 같은 태도). `FAILED` 는 롤백되어
      아무것도 안 바뀐 상태이고, 사유가 본문에 실린다.
    """
    return run_collect_receipts(as_of)


@router.post(
    "/days/{as_of}/issue-receivables",
    response_model=ReceivableOut,
    summary="그날 확정된 판매를 채권으로 세운다 — 수금보다 앞이고 판단과는 별개다",
)
def master_issue_receivables(as_of: date) -> ReceivableOut:
    """`as_of` 가 `sale_date` 인 확정 판매를 파트마다 채권으로 세운다.

    🔴 **왜 자기 엔드포인트인가.**

      바로 위 세 형제가 적어 둔 원칙 그대로다 — *"명시적 호출이다. 실행의 부작용이
      아니다. 사건에는 자기 자리가 있다."* **채권 발행도 사건이다.** 판매가 확정되면
      `receivables` 가 한 줄 늘고 `finance_states.receivables_krw` 가 올라가며,
      그것은 장부가 바뀌는 것이다.

    ★ **`run_procurement` 안으로 넣지 않는다.** 넣으면 판단 한 번이 채권 잔액을
      움직이고 *"같은 `as_of` 로 백번 돌려도 같은 답"* 이 깨진다. `receivables_krw` 는
      매입 cap 이 보는 값이라 더 그렇다.

    🔴 **`run_scheduled_day` 가 부르는 함수와 사람이 부르는 함수가 같다.** 둘이
      갈리면 손으로 부른 결과와 걷기 결과가 다른 코드를 지난다.

    🔴 **마스터가 채권 금액도 기일도 스스로 정하지 않는다** (판매·재무 결정 2026-09-09).

      ```text
      Sales     확정된 판매의 정본 — sale_date · collection_due_date · total_amount_krw
      Finance   채권 원장의 정본 — receivable_id · (sim_run_id, financing_mode) 축
      Master    그날 확정분을 **운반하고 호출**한다
      ```

      ⚠️ **`collection_due_date` 가 비어 있으면 지어내지 않고 막는다.** 기일을 마스터가
        발명하면 그 값으로 수금 판정이 돌고, 틀려도 에러가 안 난다.

    ★ **멱등이다.** 같은 날을 두 번 걸어도 채권은 하나다 — 두 번째는 `ISSUED` 인데
      새로 만든 건수가 0 이고, 그것이 정상이다.

    ★ **순서는 문장이 아니라 Gate 가 지킨다.** 안 열린 날 부르면 `NOT_OPENED` 로
      돌아서고 `next_action` 이 `OPEN_DAY_REQUIRED` 를 준다 — `/days/{as_of}/collect`
      와 같은 모양이다.

    ⚠️ **달력일이다.** `sales.sale_date` 가 정본이고 실행일 달력으로 밀지 않는다.

    | 상태 | 언제 |
    |---|---|
    | 200 | 세웠다 · 세울 게 없었다 · 막혔다 · 안 열렸다 — 전부 **그날의 사실**이다 |

    ★ **실패도 200 이다** (`/days/{as_of}/collect` 와 같은 태도). `FAILED` 는 롤백되어
      아무것도 안 바뀐 상태이고, 사유가 본문에 실린다.
    """
    return run_issue_receivables(as_of)


@router.post(
    "/days/{as_of}/close",
    response_model=ClosingOut,
    summary="그날을 닫는다 — 하루의 맨 끝이다. 숫자는 재무가 낸다",
)
def master_close_day(as_of: date) -> ClosingOut:
    """`as_of` 를 파트마다 닫는다. **출고 뒤이고 하루의 맨 끝이다.**

    🔴 **왜 자기 엔드포인트인가.**

      바로 위 네 형제가 적어 둔 원칙 그대로다 — *"명시적 호출이다. 실행의 부작용이
      아니다. 사건에는 자기 자리가 있다."* **마감도 사건이다.** 그날이 닫히면
      `daily_closings` 에 한 줄이 서고, 그것이 손익 곡선의 한 점이 된다.

    🔴 **마스터가 숫자를 계산하지 않는다.**

      ```text
      마스터   "그날을 닫아 달라" 고 부르고, 답을 어휘로 받아 적는다
      재무     그날 숫자와 daily_closings 적재
      ```

      ⚠️ **마감은 재무 원장 계산이다.** 마스터가 계산하면 조정자가 부서를 겸한다 —
        등록소를 일곱 개나 나눈 이유가 그것이다.

    🔴 **오늘은 이 경로가 매일 `NOTHING_DUE` + `missing=["finance"]` 를 낸다.**
      재무 마감 어댑터가 아직 없어서다. **그것이 맞는 상태다** — *"오늘 마감이 안
      돈다"* 를 정직하게 보이게 한다. 붙는 날 `bootstrap` 한 줄이면 된다.

    🔴 **`run_scheduled_day` 가 부르는 함수와 사람이 부르는 함수가 같다.** 둘이
      갈리면 손으로 부른 결과와 걷기 결과가 다른 코드를 지난다.

    ★ **`sim_run_id` 는 마스터가 정한다.** `daily_closings` 의 PK 가
      `(sim_run_id, close_date)` 라 **어느 실행의 장부인가**가 없으면 행이 어디에
      앉을지 정해지지 않는다. 값의 주인은 `ledger_repository.BURN_IN_SIM_RUN_ID`
      하나이고, 같은 날 판단 행·관문 행이 싣는 값과 같다.

    ★ **순서는 문장이 아니라 Gate 가 지킨다.** 안 열린 날 부르면 `NOT_OPENED` 로
      돌아서고 `next_action` 이 `OPEN_DAY_REQUIRED` 를 준다 —
      `/days/{as_of}/issue-receivables` 와 같은 모양이다.

    ⚠️ **장부 관문은 이 경로에 없다.** 손으로 부르는 이 자리는 관문 사유를 안 받고,
      막힌 날 `BLOCKED` 를 내는 것은 걷기(`run_scheduled_day`)가 하는 일이다 —
      관문의 답을 아는 쪽이 거기이기 때문이다.

    | 상태 | 언제 |
    |---|---|
    | 200 | 닫았다 · 닫을 게 없었다 · 막혔다 · 안 열렸다 — 전부 **그날의 사실**이다 |

    ★ **실패도 200 이다** (`/days/{as_of}/issue-receivables` 와 같은 태도). `FAILED` 는
      롤백되어 아무것도 안 바뀐 상태이고, 사유가 본문에 실린다.
    """
    return run_close_day(as_of, sim_run_id=BURN_IN_SIM_RUN_ID)
