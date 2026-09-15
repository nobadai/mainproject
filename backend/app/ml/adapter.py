"""마스터 포트 — `AgentRequest` → 질의응답 → `(AgentReply, ExecutionMetadata)`.

## 무엇인가

**마스터가 우리를 부르는 유일한 문**이다. 다른 파트와 같은 모양으로 맞췄다
(`logistics/adapter.py::logistics_port` · `purchase_agent/adapter.py::purchase_port`).

```text
AgentPort = (AgentRequest) -> (AgentReply, ExecutionMetadata)
```

★ **HTTP 를 안 쓴다.** `app/ml/router.py` 의 `/ml/qa` 는 **시험용 입구**이고 연결에
  쓰지 않는다. 같은 프로세스 안에서 함수로 부른다 — 다른 파트가 전부 그렇고,
  그래야 마스터의 호출 예산·이력·봉투 검증이 한 줄기로 이어진다.

## 두 갈래인 이유 — 지금 마스터는 질문을 안 실어 보낸다

`master/status_flow.py:110` 이 `runner.call(agent, "STATUS_QUERY")` 만 부른다.
**`payload` 가 비어서 온다.** 그래서 두 경우를 다 받는다.

```text
payload 에 question 이 있다    질문을 해석해 답한다 (우리 Q&A 그대로)
payload 가 비어 있다           오늘 예측 요약을 답한다 — 되묻지 않는다
```

🔴 **빈 요청에 되묻지 않는다.** 마스터가 사람 말을 그대로 넘겨주게 되기 전까지는
  «무엇을 물었는지» 를 알 길이 없는데, 그 상태에서 `NEEDS_CLARIFICATION` 을 내면
  **조회할 때마다 되묻는 부서**가 된다. 오늘 값을 요약해 주는 편이 답이다.

## 마스터가 「ml」을 알아야 부를 수 있다 — 저쪽 3줄

우리 쪽은 이 파일로 끝이고, 마스터 저장소에 세 자리가 필요하다. 우리는
`app/ml/` 밖을 고치지 않으므로 **문서로 넘긴다** (`app/ml/README.md`).

```python
# app/master/envelope.py:58
AgentName = Literal["finance", "inventory", "purchase", "sales", "ml"]

# app/master/envelope.py:194  _AGENT_MODES
"ml": frozenset({"STATUS_QUERY"}),

# app/master/bootstrap.py
from app.ml.wiring import register_ml_agent
register_ml_agent()
```

★ **모드를 새로 만들지 않았다.** `STATUS_QUERY` 는 *"묻기만 하는 요청"* 이고
  우리가 하는 일이 정확히 그것이다. 새 모드를 요구하면 저쪽이 고칠 자리가 는다.

🔴 **`suggested_adjustments` 를 절대 싣지 않는다.** 봉투가 `_AGENT_DEPT` 에 없는
  에이전트의 조정안을 `ContractViolation` 으로 막는다. 우리는 조언자가 아니라
  **답하는 쪽**이다 — 밴드에 기여하지 않는다.

## payload 를 어떻게 짜나 — 마스터가 한 줄씩 펼쳐 쓴다

`master/answer.py::facts_from_status` 가 payload 의 **키마다 한 줄**을 만든다.
아는 키는 라벨을 붙이고, 모르는 키는 **이름 그대로** 나간다.

그래서 마크다운 한 덩어리를 payload 에 그냥 넣으면 **표가 한 줄로 뭉개진다.**
둘로 나눠 싣는다.

```text
answer_markdown   사람에게 그대로 보여줄 글. 마스터가 통째로 실으면 된다
나머지 칸          기계가 읽는 값 — 품목·가격종류·기준일·대상일·예측값·오차율
```

⚠️ **마스터가 `answer_markdown` 을 그대로 실을지는 저쪽 결정이다.** 지금 규칙대로면
  한 줄로 펼쳐진다. 그래서 `app/ml/README.md` 에 *"이 칸은 펼치지 말고
  그대로 붙여 달라"* 를 적었고, 그때까지도 **나머지 칸만으로 답이 성립**하도록
  값을 같이 싣는다.

## Evidence 등급을 ASSUMED 로 두는 이유

`contracts/core.py` 의 `HARD_ALLOWED_GRADES` 는 `OFFICIAL · VENDOR · SIM_FIXED` 다.
**예측은 관측이 아니다.** 우리 값으로 하드 제약을 세우면 «모델이 틀리면 제약도
틀리는» 제약이 된다. 그래서 `ASSUMED` 로 내보내 하드 제약에 못 쓰게 한다.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from app.contracts.core import Evidence
from app.master.envelope import (
    AgentReply,
    AgentRequest,
    ExecutionMetadata,
)
from app.ml import qa_llm, qa_tools
from app.ml.qa_graph import answer as qa_answer
from app.ml.qa_schemas import QaRequest

#: 마스터가 우리를 부르는 이름. **저쪽 `AgentName` 에 같은 값이 있어야 한다.**
AGENT_NAME = "ml"

#: 우리가 받는 모드. 늘리려면 저쪽 `_AGENT_MODES` 도 같이 늘어야 한다.
SUPPORTED_MODES: tuple[str, ...] = ("STATUS_QUERY",)

#: 쓴 도구 이름. 실제로 부른 것만 적는다 — 안 부른 것을 적으면 실행 계획이 거짓이 된다.
_T_QA = "ml.qa_graph.answer"
_T_LATEST = "ml.qa_tools.latest_base_date"
_T_ROWS = "ml.qa_tools.forecast_rows"

#: 질문이 실려 오는 칸. **하나뿐이다** (마스터 확정 · 2026-09-15).
#:
#: 처음에는 `question` · `utterance` · `q` 셋을 다 받았다. 어느 이름으로 올지 몰라서였다.
#: 마스터가 `question` 으로 정했으므로 **나머지를 닫는다** — 열어 두면 나중에 어느 것이
#: 정본인지 아무도 못 정하고, 두 이름으로 다른 값이 오는 날 조용히 한쪽만 읽힌다.
_QUESTION_KEYS = ("question",)


def _run_id(request: AgentRequest) -> str:
    """회신과 메타데이터가 **같은 값**을 써야 한다 (E-BIND-RUN-ID)."""
    return f"{request.context.request_id}:{AGENT_NAME}:{request.call_seq}"


def _question(request: AgentRequest) -> str | None:
    """마스터가 질문을 실어 보냈나. 없으면 `None` — 지어내지 않는다."""
    for key in _QUESTION_KEYS:
        value = request.payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _item(request: AgentRequest) -> str | None:
    """마스터 `Intent.item` 이 payload 로 내려오면 쓴다. 어휘가 우리와 같다."""
    value = request.payload.get("item")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _metadata(
    request: AgentRequest,
    *,
    tools: tuple[str, ...],
    elapsed_ms: int,
    llm_called: bool,
) -> ExecutionMetadata:
    """실행 흔적. **업무 결과와 섞지 않는다** (M-1 §6).

    `llm_status` 는 네 값의 뜻이 서로 다르다 (`master/llm/schemas.py`).

    ```text
    DISABLED          설정이 꺼져 있다 (`ML_LLM_ENABLED=0` 또는 키 없음)
    SKIPPED_TEMPLATE  켜져 있는데 이번 실행에서는 안 불렀다
    SUCCESS           불렀고 답을 받았다
    ```

    🔴 «안 켰다» 와 «켰는데 이번엔 안 썼다» 를 한 값으로 적으면 사람이 없는 문제를
      찾는다. 매입이 같은 자리에서 같은 이유로 갈라 적는다.
    """
    if not qa_llm.enabled():
        llm_status = "DISABLED"
    else:
        llm_status = "SUCCESS" if llm_called else "SKIPPED_TEMPLATE"
    return ExecutionMetadata(
        run_id=_run_id(request),
        request_id=request.context.request_id,
        agent=AGENT_NAME,  # type: ignore[arg-type]
        used_tools=tools,
        tool_order=tuple(range(1, len(tools) + 1)),
        llm_status=llm_status,  # type: ignore[arg-type]
        llm_model=qa_llm._model() if llm_called else "",
        elapsed_ms=elapsed_ms,
    )


def _reply(
    request: AgentRequest,
    *,
    runtime_status: str,
    business_status: str,
    payload: dict[str, Any] | None = None,
    evidences: tuple[Evidence, ...] = (),
    reasoning: str = "",
    missing_data: tuple[str, ...] = (),
    observed_at: date | None = None,
) -> AgentReply:
    """봉투 4종(request_id·as_of·agent·mode)을 **한 곳에서** 채운다.

    호출부마다 적으면 한 경로만 어긋나도 검증이 잡는데 원인은 흩어진다.

    🔴 `observed_at` 기본값은 `None` 이고 그것이 **「안 쟀다」** 다. 예측을 실제로
      읽은 경로만 값을 넘긴다 — `as_of` 로 메우면 안 잰 호출이 잰 호출로 세어진다.
    """
    return AgentReply(
        request_id=request.context.request_id,
        as_of=request.context.as_of,
        agent=AGENT_NAME,  # type: ignore[arg-type]
        mode=request.mode,
        run_id=_run_id(request),
        runtime_status=runtime_status,  # type: ignore[arg-type]
        business_status=business_status,  # type: ignore[arg-type]
        payload=dict(payload or {}),
        evidences=evidences,
        reasoning=reasoning,
        missing_data=missing_data,
        observed_at=observed_at,
    )


#: 근거 하나가 가리키는 값. `claim` 은 **payload 의 주소와 글자까지 같아야** 한다
#: (`envelope.canonical_claim`). 다르면 «무엇을 뒷받침하는지 불명» 으로 고아가 된다.
_EVIDENCE_FIELDS = ("predicted", "lower", "upper")


def _evidence(
    index: int,
    row: dict[str, Any],
    field_name: str,
    item: str,
    kind: str,
    base_dt: date,
) -> Evidence:
    """예측 한 칸의 근거. **ref 를 지어내지 않는다** — 읽은 표와 키를 그대로 적는다.

    등급은 `ASSUMED` 다. 예측은 관측이 아니라서 하드 제약을 세울 자격이 없다
    (`HARD_ALLOWED_GRADES` 에 없다).

    🔴 `claim` 을 사람 말로 적으면 안 된다. 처음에 *"배추 AUC 2026-09-16 예측"* 으로
      적었더니 payload 어디와도 안 맞아 **고아 근거**가 됐다 (2026-09-15 실측).
      주소는 `forecasts[0].predicted` 처럼 **payload 를 그대로 가리키는 경로**다.
    """
    return Evidence(
        claim=f"forecasts[{index}].{field_name}",
        source="tool_calc",
        ref_ids=(
            (
                f"ml_price_forecasts:base_dt={base_dt.isoformat()}"
                f",item={item},kind={kind},target_dt={row['target_dt']}"
                f",column={field_name}"
            ),
        ),
        value=float(row[field_name]),
        unit=str(row.get("unit") or "원/kg"),
        evidence_grade="ASSUMED",
        evidence_detail="예측값 — 관측이 아니다. 하드 제약의 근거로 쓰지 말 것",
    )


def evidences_for(rows: list[dict[str, Any]], item: str, kind: str, base_dt: date) -> tuple:
    """`forecasts[]` 의 숫자 칸마다 근거 하나. **행 하나에 셋이다.**

    봉투가 배열 항목 안의 **숫자**에 근거를 요구한다 (`required_claims`). 라벨은
    면제라 `target_dt` · `kind` · `is_filled` 는 안 단다.
    """
    return tuple(
        _evidence(index, row, field_name, item, kind, base_dt)
        for index, row in enumerate(rows)
        for field_name in _EVIDENCE_FIELDS
        if row.get(field_name) is not None
    )


def _forecast_rows(out: Any) -> list[dict[str, Any]]:
    """예측 행을 payload 모양으로. **날짜는 문자열로** 내보낸다 (JSON 으로 나간다)."""
    kind = out.meta.kind
    return [
        {
            "target_dt": str(row["target_dt"]),
            "predicted": row["predicted"],
            "lower": row["lower"],
            "upper": row["upper"],
            "kind": kind,
            #   ★ 복사값이라는 것은 **답의 일부**다. 감추면 그날 조사가 있었던 것으로
            #     읽힌다. 다만 «시장이 쉬었다» 는 뜻은 아니다 — 우리 조사 축이다.
            "is_filled": bool(row.get("is_filled")),
        }
        for row in out.rows_for_evidence
    ]


def _answer_payload(out: Any) -> dict[str, Any]:
    """Q&A 결과를 payload 로. **마크다운과 기계용 값을 나눠 싣는다.**

    마스터가 `answer_markdown` 을 그대로 쓰지 못하더라도 나머지 칸만으로 답이
    성립해야 한다 — 한쪽이 안 되면 답이 통째로 비는 모양을 만들지 않는다.

    ★ **모양을 봉투 규칙에 맞춘다** (2026-09-15 실측으로 고침).

      ```text
      최상위 숫자·대문자 라벨      근거 필요 — 없으면 E-EVIDENCE-MISSING
      배열 항목 안의 숫자          근거 필요
      배열 항목 안의 라벨          면제
      as_of 등 봉투 어휘           면제
      ```

      그래서 `qa_status`(`"OK"`) · `target_kind`(`"AUC"`) 같은 **근거를 달 수 없는
      대문자 라벨**을 최상위에 두지 않는다. 억지로 근거를 만들면 *"세어 본 것"* 을
      근거라고 적게 된다 — 봉투 주석이 그것을 결함이라고 부른다.

      같은 사실은 `forecasts[]` 안에 들어간다. 사람에게는 `answer_markdown` 이,
      기계에는 `forecasts[]` 가 답이다.
    """
    meta = out.meta
    payload: dict[str, Any] = {
        "answer_markdown": out.markdown,
        "as_of": meta.base_dt.isoformat() if meta.base_dt else None,
        #   ★ 소문자라 «판정 라벨» 로 안 걸린다. 상태를 감추지 않으면서 근거도
        #     요구되지 않는 모양이다.
        "answer_status": meta.status.lower(),
    }
    if meta.item:
        payload["item"] = meta.item
    if meta.model_version:
        payload["model_version"] = meta.model_version
    if meta.source:
        payload["forecast_source"] = meta.source
    if meta.use_recommended is not None:
        payload["use_recommended"] = meta.use_recommended
    rows = _forecast_rows(out)
    if rows:
        payload["forecasts"] = rows
    if meta.out_of_range:
        #   스칼라 배열은 통째로 근거 하나를 요구한다 — 그래서 문장으로 싣는다.
        #   "못 답한 날" 은 세어 본 것이지 근거를 댈 수치가 아니다.
        payload["out_of_range_note"] = (
            "예측 범위 밖: " + ", ".join(d.isoformat() for d in meta.out_of_range)
        )
    return payload


#: 답할 수 있는 범위. 질문이 안 왔을 때 **무엇을 물으면 되는지**까지 적는다.
_CAPABILITY_NOTE = (
    "배추·무·양파의 경락가(AUC)·중도매가(WHSL)·소매가(RTL)를 "
    "오늘부터 18일 뒤까지 답합니다. 질문 문장을 payload.question 으로 보내주세요."
)


def _no_question(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """질문이 안 왔을 때. **되묻지 않고 오늘 예측을 요약해 답한다.**

    🔴 여기서 `RUNTIME_NOT_READY` 를 내면 안 된다. 마스터가 아직 사람 말을 안 넘겨
      주는 구조라(`status_flow.py:110`) **매번** 그 답이 나가고, 그러면 화면에
      *"ML 이 답하지 못했다"* 가 상시로 뜬다 — 진짜 고장과 구분이 안 된다.
    """
    started = time.perf_counter()
    tools = (_T_LATEST,)
    try:
        base_dt = qa_tools.latest_base_date(request.context.as_of)
    except Exception:                                        # noqa: BLE001  DB 미연결
        return _error(request, tools, started, "예측표를 읽지 못했다")
    if base_dt is None:
        elapsed = int((time.perf_counter() - started) * 1000)
        return (
            _reply(
                request,
                runtime_status="RUNTIME_NOT_READY",
                business_status="skipped",
                missing_data=("ml_price_forecasts",),
                reasoning="전달표에 예측이 아직 없다",
            ),
            _metadata(request, tools=tools, elapsed_ms=elapsed, llm_called=False),
        )

    elapsed = int((time.perf_counter() - started) * 1000)
    return (
        _reply(
            request,
            runtime_status="READY",
            business_status="ok",
            payload={
                "as_of": base_dt.isoformat(),
                "forecast_available": True,
                "answerable": _CAPABILITY_NOTE,
            },
            reasoning="질문 문장이 오지 않아 예측 보유 상태만 답했다",
            observed_at=base_dt,
        ),
        _metadata(request, tools=tools, elapsed_ms=elapsed, llm_called=False),
    )


def _error(
    request: AgentRequest,
    tools: tuple[str, ...],
    started: float,
    reason: str,
) -> tuple[AgentReply, ExecutionMetadata]:
    """터진 호출. **예외를 위로 던지지 않는다** — 하나의 실패가 사이클을 죽인다.

    `ERROR` 만 재시도 가치가 있다 (`AgentReply.worth_retry`). 값이 없어서 못 낸
    답(`RUNTIME_NOT_READY`)과 갈라 적어야 마스터가 다시 부를지 정할 수 있다.
    """
    elapsed = int((time.perf_counter() - started) * 1000)
    return (
        _reply(
            request,
            runtime_status="ERROR",
            business_status="skipped",
            reasoning=reason,
        ),
        _metadata(request, tools=tools, elapsed_ms=elapsed, llm_called=False),
    )


#: 답은 나갔지만 **일부만** 된 상태. 업무 판정을 `conditional` 로 적는다.
_PARTIAL_STATUSES = frozenset({"PARTIAL", "NEED_CLARIFY"})

#: 답을 못 낸 상태. 무엇이 없어서인지 이름을 같이 낸다.
_NOT_READY_MISSING: dict[str, tuple[str, ...]] = {
    "SOURCE_UNAVAILABLE": ("ml_price_forecasts",),
    "NO_DATA": ("ml_price_forecasts",),
    "LLM_UNAVAILABLE": ("ml_question_interpretation",),
}


def ml_port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
    """마스터가 부르는 유일한 접점.

    ★ 실패를 **값으로** 다룬다 (`ports.py` §7.1). 어떤 경로로도 예외가 위로
      올라가지 않는다 — 우리 하나가 터져서 마스터 사이클이 죽으면 안 된다.
    """
    if request.mode not in SUPPORTED_MODES:
        started = time.perf_counter()
        return _error(request, (), started, f"{request.mode} 는 받지 않는다")

    question = _question(request)
    if question is None:
        return _no_question(request)

    started = time.perf_counter()
    tools = (_T_QA, _T_LATEST, _T_ROWS)
    try:
        out = qa_answer(
            QaRequest(
                question=question,
                item=_item(request),
                as_of=request.context.as_of,
            )
        )
    except Exception:                                        # noqa: BLE001
        #   ★ 오류 문구를 그대로 올리지 않는다. 접속 정보가 오류에 실려 나온 적이 있다.
        return _error(request, tools, started, "질의응답 처리 중 오류가 났다")

    elapsed = int((time.perf_counter() - started) * 1000)
    status = out.meta.status
    llm_called = qa_llm.enabled()

    if status in _NOT_READY_MISSING:
        return (
            _reply(
                request,
                runtime_status="RUNTIME_NOT_READY",
                business_status="skipped",
                missing_data=_NOT_READY_MISSING[status],
                reasoning=out.markdown,
            ),
            _metadata(request, tools=tools, elapsed_ms=elapsed, llm_called=llm_called),
        )

    if status == "OUT_OF_SCOPE":
        #   ★ 소관이 아닌 것은 **고장이 아니다.** READY 로 답하고 그렇게 말한다 —
        #     RUNTIME_NOT_READY 로 내면 «ML 이 못 답했다» 로 이력에 남는다.
        return (
            _reply(
                request,
                runtime_status="READY",
                business_status="skipped",
                payload=_answer_payload(out),
                reasoning="우리 예측 대상이 아니다",
            ),
            _metadata(request, tools=tools, elapsed_ms=elapsed, llm_called=llm_called),
        )

    base_dt = out.meta.base_dt
    evidences: tuple[Evidence, ...] = ()
    if out.rows_for_evidence and base_dt and out.meta.item and out.meta.kind:
        evidences = evidences_for(
            out.rows_for_evidence, out.meta.item, out.meta.kind, base_dt
        )

    return (
        _reply(
            request,
            runtime_status="READY",
            business_status="conditional" if status in _PARTIAL_STATUSES else "ok",
            payload=_answer_payload(out),
            evidences=evidences,
            reasoning=f"질문을 해석해 {out.meta.item or '?'} {out.meta.kind or '?'} 예측을 읽었다",
            #   ★ 예측을 실제로 읽었을 때만 관측 시점을 적는다.
            observed_at=base_dt if evidences else None,
        ),
        _metadata(request, tools=tools, elapsed_ms=elapsed, llm_called=llm_called),
    )
