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
    used_default: bool         # 날짜를 안 말해 **오늘**을 기본값으로 썼다
    default_fell_back: bool    # 그 오늘 값마저 없어 내일로 물러섰다
    rows: list[dict[str, Any]]
    today: dict[str, Any] | None
    accuracy: dict[str, Any] | None
    usability: dict[str, Any] | None
    status: str
    message: str               # 되묻기·거절일 때 쓸 본문
    note: str                  # 답 맨 앞에 붙일 한 줄 (무엇을 무시했는지)
    markdown: str
    meta: QaMeta


# ───────────────────────────────────────────────────────────── 노드
def supervise(state: QaState) -> QaState:
    """질문 → 어느 도구·어떤 인자. **고르기만 한다.**"""
    req = state["request"]
    #   ★ 직접 준 값이 **우리 품목일 때만** 해석을 건너뛴다.
    #     Swagger 기본 본문이 item 에 "string" 을 넣어 주는데, 그것을 품목으로 읽고
    #     거절하면 질문 문장을 쳐다보지도 않는다 (2026-09-14 실측).
    if req.item in QA_ITEMS and req.kind in QA_KINDS:
        return {"item": req.item, "kind": req.kind}
    if not req.question:
        if req.item and req.item not in QA_ITEMS:
            return {"status": "OUT_OF_SCOPE",
                    "message": OUT_OF_SCOPE_ITEM.format(item=req.item)}
        return {"status": "NEED_CLARIFY", "message": OUT_OF_SCOPE_KIND}

    #   질문이 같이 왔으면 질문으로 답하고, 무엇을 무시했는지 밝힌다.
    ignored = req.item if (req.item and req.item not in QA_ITEMS) else None

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
    if ignored:
        picked["note"] = (
            f"> `item` 에 준 «{ignored}» 는 우리 품목이 아니어서 질문 문장으로 답했습니다."
        )
    #   ★ 질문에서 못 고른 칸은 **요청이 직접 준 유효한 값**으로 메운다.
    #     item 하나가 엉터리라고 해서 제대로 준 kind 까지 버리면, 답할 수 있는
    #     질문에 되묻게 된다 (2026-09-14 실측: item="string" · kind="AUC").
    item = chosen.get("item") or (req.item if req.item in QA_ITEMS else None)
    kind = chosen.get("kind") or (req.kind if req.kind in QA_KINDS else None)
    if item:
        picked["item"] = item
    if kind:
        picked["kind"] = kind
    if chosen.get("dates"):
        picked["asked"] = list(chosen["dates"])
    if not item or not kind:
        #   ★ **빠진 것만 묻는다** (2026-09-15 · 사용자 지적으로 고침).
        #     전에는 품목이 없어도 «가격 종류를 알 수 없습니다» 만 적었다. 그러면
        #     「경락가요」라고 답해도 또 되묻게 된다 — 한 번에 알려줬어야 할 것을
        #     두 번에 나눠 묻는 셈이다.
        #
        #   ★ **알아들은 것은 문장에 적는다.** 날짜를 제대로 골라 놓고 되묻기로
        #     빠지면 그 값이 조용히 사라져, 사람이 「오늘부터 8일」을 또 적게 된다.
        picked["status"] = "NEED_CLARIFY"
        picked["message"] = _ask_again(item, kind, picked.get("asked"))
    return picked


def _ask_again(item: str | None, kind: str | None, dates: list[date] | None) -> str:
    """되묻는 문장. **빠진 것만 묻고, 알아들은 것은 밝힌다.**"""
    known: list[str] = []
    if item:
        known.append(f"품목 **{item}**")
    if dates:
        known.append(
            f"날짜 **{dates[0]}**" if len(dates) == 1
            else f"날짜 **{dates[0]} ~ {dates[-1]}** ({len(dates)}일)"
        )

    if not item and not kind:
        need = "어느 품목의 어느 가격인지"
        example = "배추 경락가"
    elif not item:
        need = "어느 품목인지"
        example = f"배추 {KIND_LABEL.get(kind, kind)}"
    else:
        need = "어느 가격인지"
        example = f"{item} 경락가"

    lines = [f"{need} 알 수 없습니다."]
    if known:
        lines.append(f"알아들은 것 — {' · '.join(known)}. 이건 다시 안 적으셔도 됩니다.")
    if not item:
        lines.append("품목: **배추 · 무 · 양파**")
    if not kind:
        lines.append("가격: **경락가(AUC) · 중도매가(WHSL) · 소매가(RTL)**")
    lines.append(f"예: `{example}` 라고 덧붙여 다시 물어봐 주세요.")
    return "\n\n".join(lines)


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
    #   ★ **날짜를 안 말했으면 오늘을 보여준다** (2026-09-15 · 사용자 지시로 바꿈).
    #     전에는 말없이 «내일 하루» 였다. 값은 맞지만 왜 하루뿐인지 안 밝혀서,
    #     사람이 «원래 하루치만 있나 보다» 하고 넘어간다 — 조용한 축소다.
    #     기본값을 썼다는 것을 `used_default` 로 들고 가 답에 한 줄로 적는다.
    used_default = not (req.dates or state.get("asked"))
    asked = list(req.dates or state.get("asked") or [base_dt])
    wants_today = any(d == base_dt for d in asked)
    last = base_dt + timedelta(days=QA_MAX_OFFSET)
    targets = sorted({d for d in asked if base_dt < d <= last})
    out_of_range = sorted({d for d in asked if d < base_dt or d > last})
    return {
        "base_dt": base_dt,
        "wants_today": wants_today,
        "targets": targets,
        "out_of_range": out_of_range,
        "used_default": used_default,
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
        base_dt = state["base_dt"]
        targets = list(state.get("targets") or [])
        rows = qa_tools.forecast_rows(item, kind, base_dt, targets)
        today = qa_tools.today_row(item, kind) if state.get("wants_today") else None
        fell_back = False
        if state.get("used_default") and today is None and not rows:
            #   ★ 오늘 값이 없는 날도 있다 (원본 창고에 리드 0 이 안 들어온 아침).
            #     그때는 **빈 답을 주지 말고 내일로 물러선다.** 물러섰다는 것도 적는다.
            fell_back = True
            rows = qa_tools.forecast_rows(item, kind, base_dt, [base_dt + timedelta(days=1)])
        return {
            "rows": rows,
            "today": today,
            "default_fell_back": fell_back,
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
            #   ★ 어느 창고에서 읽었는지는 **사람에게 알 바가 아니다** (2026-09-15).
            #     기계가 볼 것은 `meta.source` 로 나간다 — 답 문장은 값만 말한다.
            _line(f"오늘 {today['target_dt']}", today, unit)
        )
    for row in rows:
        body.append(_line(f"{row['target_dt']} (D+{row['offset_days']})", row, unit))

    missing = sorted(set(state.get("targets") or []) - {r["target_dt"] for r in rows})
    tail: list[str] = [""]
    if state.get("used_default"):
        #   ★ **기본값을 썼다는 것을 밝힌다.** 안 밝히면 「하루치만 있나 보다」로 읽힌다.
        tail.append(
            "> 날짜를 따로 말씀하지 않으셔서 "
            + ("오늘 값이 아직 없어 **내일** 값을 보여드립니다."
               if state.get("default_fell_back") else "**오늘** 값을 보여드립니다.")
            + " 「전부」라고 하시면 오늘부터 18일 뒤까지 다 보여드립니다."
        )
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
        #   ★ **코드 이름을 문장에 넣지 않는다** (마스터 요청 · 2026-09-15).
        #     화면이 «실제 서비스» 전제로 정리돼 영문 키가 보이면 안 된다.
        #     `ops_auc` 같은 이름은 `meta.model_version` 으로만 나간다 — 기계가 읽는 칸이다.
        tail.append(f"\n*{_kst(rows[0]['generated_at'])} 에 계산한 값입니다*")

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
    #   ★ 무엇을 무시했는지는 답 맨 앞에 적는다. 조용히 무시하면 나중에
    #     «왜 다른 걸 답했지» 가 된다.
    note = [state["note"], ""] if state.get("note") else []
    return {
        "markdown": "\n".join(note + head + body + tail),
        "meta": meta,
        "status": status,
    }


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
    """바깥에 드러내는 것은 이 함수 하나다. 그래프는 안쪽 사정이다.

    ★ **읽은 행을 같이 돌려준다** (2026-09-15). 마스터 어댑터가 회신에 붙일
      `Evidence` 를 그 행에서 만든다 — 답 문장에서 숫자를 다시 뜯어내면
      **같은 사실에 두 경로가 생긴다.** 값은 표에서 온 것 하나여야 한다.
    """
    final = build_graph().invoke({"request": request})
    rows = list(final.get("rows") or [])
    today = final.get("today")
    if today:
        rows = [*rows, today]
    return QaAnswer(
        markdown=final["markdown"],
        meta=final["meta"],
        rows_for_evidence=rows,
    )
