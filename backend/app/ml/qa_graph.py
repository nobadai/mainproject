"""예측 질의응답 — LangGraph 그래프 (노드 넷 · 분기 둘 · 출구 하나).

```text
START → supervise → gate ─(정상)→ fetch → compose → END
                      └─(되묻기 · 범위 밖 · 값 없음)→ compose → END
```

★ **LLM 이 고르고, 규칙이 확인한다.**

    supervise   질문을 해석해 «어느 도구 · 어떤 인자» 를 고른다        ← LLM 자리
    gate        고른 값이 범위 안인지 확인한다                          ← 규칙
    fetch       표를 읽는다 (여러 날짜를 한 번에)                       ← 규칙
    compose     마크다운을 만든다. **유일한 출구다**                    ← 틀 (+ 나중에 LLM)

★ **부르는 길이 둘이다.** `question` 만 주면 LLM 이 해석하고, `item·kind·dates` 를
  직접 주면 해석을 건너뛴다. 둘째 길은 LLM 없이도 값과 서식을 확인하려고 남긴다.

🔴 **LLM 이 고른 값을 그대로 쓰지 않는다.** `gate` 가 다시 검사한다. 키가 없거나
   호출이 실패하면 «해석하지 못했습니다» 로 답한다 — 지어내지 않는다.

★ **체크포인트를 쓰지 않는다.** 대화 상태를 들고 있지 않다 (`compile()` 맨몸).
  이어지는 질문(「5일 뒤엔?」)은 마스터가 직전 맥락을 같이 넘겨 주는 것으로 푼다.
  저장소 전체에 체크포인트를 쓰는 곳이 없다 — 매입·판매 그래프도 같다.
"""

from __future__ import annotations

from datetime import date, timedelta, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.ml import qa_llm, qa_tools
from app.ml.qa_schemas import (
    QA_ITEMS,
    QA_KINDS,
    QA_MAX_OFFSET,
    QaAnswer,
    QaMeta,
    QaRequest,
)

KIND_LABEL = {"AUC": "경락가", "WHSL": "중도매가", "RTL": "소매가"}

#: 전달표의 예측 시각은 기준일 06:00 KST 로 찍힌다. UTC 로 보여 주면 «왜 전날이냐» 가 된다.
KST = timezone(timedelta(hours=9))


def _kst(value) -> str:
    """예측 시각을 한국 시간으로. 시간대를 모르는 값이면 그대로 적는다."""
    try:
        return value.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    except (AttributeError, ValueError, TypeError):
        return str(value)

#: 되묻기·거절 문구는 **틀에 박는다.** 문장이 짧아질 때 먼저 잘리면 안 되는 자리다.
OUT_OF_SCOPE_ITEM = (
    "**{item}** 은 우리 예측 대상이 아닙니다. 배추 · 무 · 양파만 답할 수 있습니다."
)
#: 품목 이름을 못 받았을 때. LLM 이 out_of_scope 만 고르고 item 을 비워 보내는 일이 있다.
OUT_OF_SCOPE_UNKNOWN = (
    "우리가 답할 수 있는 품목이 아닙니다. 배추 · 무 · 양파만 답할 수 있습니다."
)
OUT_OF_SCOPE_KIND = (
    "가격 종류를 알 수 없습니다. **경락가(AUC) · 중도매가(WHSL) · 소매가(RTL)** 중에서 "
    "골라 다시 물어봐 주세요."
)
NEED_CLARIFY_LLM = (
    "지금 질문을 해석하지 못했습니다. 품목과 가격 종류를 넣어 다시 물어봐 주세요.\n\n"
    "- `배추 경락가 내일 얼마야?`\n"
    "- `무 소매가 5일 뒤 얼마나 바뀌어?`"
)
SOURCE_DOWN = "지금 예측 창고를 읽지 못했습니다. 값을 지어내지 않기 위해 답을 비웁니다."


class QaState(TypedDict, total=False):
    """그래프가 들고 다니는 것. 판정 결과만 담고 원본은 표에 둔다."""

    request: QaRequest
    item: str
    kind: str
    base_dt: date
    wants_today: bool
    asked: list[date]          # LLM 이 고른 날짜 (요청이 직접 주면 그쪽이 이긴다)
    targets: list[date]        # 전달표에서 읽을 날 (D+1 ~ D+18)
    out_of_range: list[date]
    rows: list[dict[str, Any]]
    today: dict[str, Any] | None
    accuracy: dict[str, Any] | None
    usability: dict[str, Any] | None
    status: str
    message: str               # 되묻기·거절일 때 쓸 본문
    markdown: str
    meta: QaMeta


# ───────────────────────────────────────────────────────────── 노드
def supervise(state: QaState) -> QaState:
    """질문 → 어느 도구·어떤 인자. **고르기만 한다.**"""
    req = state["request"]
    if req.item and req.kind:
        return {"item": req.item, "kind": req.kind}
    if not req.question:
        return {"status": "NEED_CLARIFY", "message": OUT_OF_SCOPE_KIND}

    #   ★ 기준일을 먼저 잡는다 — 「내일」이 며칠인지는 기준일이 있어야 정해진다.
    try:
        base_dt = qa_tools.latest_base_date(req.as_of)
    except Exception:                                    # noqa: BLE001
        return {"status": "SOURCE_UNAVAILABLE", "message": SOURCE_DOWN}
    if base_dt is None:
        return {"status": "NO_DATA", "message": "전달표에 예측이 아직 없습니다."}

    chosen = qa_llm.interpret(req.question, base_dt)
    if chosen is None:
        return {"status": "LLM_UNAVAILABLE", "message": NEED_CLARIFY_LLM}
    if chosen["route"] == "out_of_scope":
        item = chosen.get("item")
        message = OUT_OF_SCOPE_ITEM.format(item=item) if item else OUT_OF_SCOPE_UNKNOWN
        return {"status": "OUT_OF_SCOPE", "message": message}

    picked: QaState = {"base_dt": base_dt}
    if chosen.get("item"):
        picked["item"] = chosen["item"]
    if chosen.get("kind"):
        picked["kind"] = chosen["kind"]
    if chosen.get("dates"):
        picked["asked"] = list(chosen["dates"])
    if not picked.get("kind"):
        #   가격 종류를 못 고르면 되묻는다. 되묻는 문장에 다시 물을 예시를 심는다.
        picked["status"] = "NEED_CLARIFY"
        picked["message"] = OUT_OF_SCOPE_KIND
    return picked


def gate(state: QaState) -> QaState:
    """고른 값이 범위 안인가. **LLM 이 고른 값을 그대로 쿼리에 넣지 않는다.**"""
    if state.get("status"):
        return {}

    item, kind = state.get("item"), state.get("kind")
    if item not in QA_ITEMS:
        return {"status": "OUT_OF_SCOPE", "message": OUT_OF_SCOPE_ITEM.format(item=item)}
    if kind not in QA_KINDS:
        return {"status": "OUT_OF_SCOPE", "message": OUT_OF_SCOPE_KIND}

    req = state["request"]
    base_dt = state.get("base_dt")
    if base_dt is None:
        try:
            base_dt = qa_tools.latest_base_date(req.as_of)
        except Exception:                                    # noqa: BLE001  DB 미연결·표 없음
            return {"status": "SOURCE_UNAVAILABLE", "message": SOURCE_DOWN}
    if base_dt is None:
        return {"status": "NO_DATA", "message": "전달표에 예측이 아직 없습니다."}

    #   ★ 우선순위 — 요청이 직접 준 날짜 > LLM 이 고른 날짜 > 기본값(내일)
    asked = list(req.dates or state.get("asked") or [base_dt + timedelta(days=1)])
    wants_today = any(d == base_dt for d in asked)
    last = base_dt + timedelta(days=QA_MAX_OFFSET)
    targets = sorted({d for d in asked if base_dt < d <= last})
    out_of_range = sorted({d for d in asked if d < base_dt or d > last})
    return {
        "base_dt": base_dt,
        "wants_today": wants_today,
        "targets": targets,
        "out_of_range": out_of_range,
    }


def after_gate(state: QaState) -> str:
    """읽을 것이 하나라도 있으면 조회하고, 없으면 곧장 답을 만든다."""
    if state.get("status"):
        return "compose"
    return "fetch" if (state.get("targets") or state.get("wants_today")) else "compose"


def fetch(state: QaState) -> QaState:
    """표를 읽는다. 날짜 여러 개를 한 번에."""
    item, kind = state["item"], state["kind"]
    try:
        rows = qa_tools.forecast_rows(item, kind, state["base_dt"], state.get("targets") or [])
        today = qa_tools.today_row(item, kind) if state.get("wants_today") else None
        return {
            "rows": rows,
            "today": today,
            "accuracy": qa_tools.accuracy(item, kind),
            "usability": qa_tools.usability(item, kind),
        }
    except Exception:                                        # noqa: BLE001
        return {"status": "SOURCE_UNAVAILABLE", "message": SOURCE_DOWN}


def compose(state: QaState) -> QaState:
    """유일한 출구. 정상 답·되묻기·거절이 **같은 자리에서** 나간다."""
    if state.get("status") and not state.get("rows") and not state.get("today"):
        meta = QaMeta(
            status=state["status"],
            item=state.get("item"),
            kind=state.get("kind"),
            base_dt=state.get("base_dt"),
            out_of_range=state.get("out_of_range") or [],
        )
        return {"markdown": state.get("message", ""), "meta": meta}
    return _answer_markdown(state)


def _line(label: str, row: dict[str, Any], unit: str, note: str = "") -> str:
    if row.get("is_filled"):
        note = (note + " · " if note else "") + "⚠ 복사값 — 그날 조사가 없어 앞 장날 값"
    if row.get("is_gated"):
        note = (note + " · " if note else "") + "모델 대신 출발점을 그대로 씀"
    return (
        f"| {label} | **{int(row['predicted']):,}{unit}** | "
        f"{int(row['lower']):,} ~ {int(row['upper']):,} | {note} |"
    )


def _answer_markdown(state: QaState) -> QaState:
    item, kind = state["item"], state["kind"]
    rows = state.get("rows") or []
    today = state.get("today")
    base_dt = state["base_dt"]

    unit = (rows[0]["unit"] if rows else (today or {}).get("unit")) or "원/kg"
    head = [
        f"**{item} · {KIND_LABEL.get(kind, kind)} · 기준일 {base_dt}**",
        "",
        "| 날짜 | 예측 | 예상 구간 | 비고 |",
        "|---|---|---|---|",
    ]
    body: list[str] = []
    if today:
        body.append(
            _line(f"오늘 {today['target_dt']}", today, unit, "당일 값은 우리 내부 기록에서")
        )
    for row in rows:
        body.append(_line(f"{row['target_dt']} (D+{row['offset_days']})", row, unit))

    missing = sorted(set(state.get("targets") or []) - {r["target_dt"] for r in rows})
    tail: list[str] = [""]
    if state.get("out_of_range"):
        last = base_dt + timedelta(days=QA_MAX_OFFSET)
        days = " · ".join(str(d) for d in state["out_of_range"])
        tail.append(
            f"> ⚠ {days} 은 예측 범위 밖입니다. 오늘 기준 {base_dt + timedelta(days=1)} ~ {last} "
            "까지 답할 수 있습니다."
        )
    if missing:
        tail.append(f"> ⚠ {' · '.join(str(d) for d in missing)} 은 그 기준일에 예측이 없습니다.")

    anchor = (rows[0] if rows else today or {}).get("current_price")
    if anchor:
        tail.append(
            f"> 출발점 {int(anchor):,}{unit}"
            " — 실제 거래가가 아니라 모델이 출발한 값입니다."
        )

    acc = state.get("accuracy")
    if acc:
        #   ★ 화면에 적힌 것과 **같은 값**이다. prediction_log 로 다시 재지 않는다.
        tail.append(
            f"> 이 조합의 평균 오차는 **{acc['pct']}%** 입니다 "
            f"(평균 실제가 {acc['avg']} · 평균 오차 {acc['err']}). "
            f"{qa_tools.SEALED_SOURCE}."
        )
    usab = state.get("usability") or {}
    if usab.get("use_recommended") is False:
        tail.insert(
            1,
            "> 🔴 이 조합은 **판단에 쓰지 마세요.** 우리 모델보다 «어제 가격 그대로» 가 낫습니다."
            + (f" ({usab.get('quality_note')})" if usab.get("quality_note") else ""),
        )

    spec = rows[0] if rows else None
    if spec and spec.get("spec_desc"):
        tail.append(
            f"> 값의 정체: {spec.get('market_name')} · {spec.get('grade_name')}등급"
            f" · {spec['spec_desc']}"
        )
    if rows:
        tail.append(
            f"\n*기준일 {base_dt} · 모델 {rows[0]['model_version']}"
            f" · 예측 시각 {_kst(rows[0]['generated_at'])}*"
        )

    status = "OK"
    if state.get("out_of_range") or missing:
        status = "PARTIAL"
    if not rows and not today:
        status = "NO_DATA"

    meta = QaMeta(
        status=status,
        item=item,
        kind=kind,
        base_dt=base_dt,
        targets=[r["target_dt"] for r in rows] + ([today["target_dt"]] if today else []),
        missing=missing,
        out_of_range=state.get("out_of_range") or [],
        model_version=(rows[0]["model_version"] if rows else (today or {}).get("model_version")),
        generated_at=_kst(rows[0]["generated_at"]) if rows else None,
        source=("ml_price_forecasts · prediction_log" if (rows and today)
                else "prediction_log" if today else "ml_price_forecasts"),
        is_filled=[bool(r.get("is_filled")) for r in rows],
        band_method=(rows[0].get("band_method") if rows else (today or {}).get("band_method")),
        use_recommended=usab.get("use_recommended"),
    )
    return {"markdown": "\n".join(head + body + tail), "meta": meta, "status": status}


# ───────────────────────────────────────────────────────────── 그래프
def build_graph():
    """노드 넷 · 분기 둘. **체크포인트 없이** 맨몸으로 컴파일한다."""
    graph = StateGraph(QaState)
    graph.add_node("supervise", supervise)
    graph.add_node("gate", gate)
    graph.add_node("fetch", fetch)
    graph.add_node("compose", compose)

    graph.add_edge(START, "supervise")
    graph.add_edge("supervise", "gate")
    graph.add_conditional_edges("gate", after_gate, ["fetch", "compose"])
    graph.add_edge("fetch", "compose")
    graph.add_edge("compose", END)
    return graph.compile()


def answer(request: QaRequest) -> QaAnswer:
    """바깥에 드러내는 것은 이 함수 하나다. 그래프는 안쪽 사정이다."""
    final = build_graph().invoke({"request": request})
    return QaAnswer(markdown=final["markdown"], meta=final["meta"])
