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

#: 한 답에 담을 조합 상한. 3 x 3 x 19일 = 171행이면 사람이 못 읽는다.
MAX_ITEMS = 3
MAX_KINDS = 3

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
    item: str                  # 조합이 하나면 그 값 · 여럿이면 첫 번째 (옛 이름)
    kind: str
    items: list[str]           # ★ 답할 품목 전부 (2026-09-15)
    kinds: list[str]           # ★ 답할 가격 종류 전부
    asks: list[dict[str, Any]]     # ★ 짝지어진 물음 [{item, kind, dates}]
    blocks: list[dict[str, Any]]   # 조합마다 읽어 온 것 — 표 하나가 블록 하나다
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
    given_items = _valid(req.items or ([req.item] if req.item else []), QA_ITEMS)
    given_kinds = _valid(req.kinds or ([req.kind] if req.kind else []), QA_KINDS)
    if given_items and given_kinds:
        return {"items": given_items, "kinds": given_kinds,
                "item": given_items[0], "kind": given_kinds[0]}
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
    #   ★ **짝 물음이 곧 답이다** (2026-09-15). `asks` 만 오고 `items`·`kinds` 가
    #     비어 오면 답할 수 있는 질문에 되묻게 된다 — 목록을 거기서 채운다.
    asks = chosen.get("asks") or []
    items = chosen.get("items") or [a["item"] for a in asks] or given_items
    kinds = chosen.get("kinds") or [a["kind"] for a in asks] or given_kinds
    item = items[0] if items else None
    kind = kinds[0] if kinds else None
    if items:
        picked["items"] = items
        picked["item"] = items[0]
    if kinds:
        picked["kinds"] = kinds
        picked["kind"] = kinds[0]
    if chosen.get("dates"):
        picked["asked"] = list(chosen["dates"])
    if chosen.get("asks"):
        #   ★ 짝지어진 물음이 오면 **곱하지 않는다** (2026-09-15).
        #     「5일 뒤 배추 경락가와 7일 뒤 무 도매가」를 곱하면 안 물어본
        #     배추 중도매가·무 경락가가 나가고 날짜도 뒤섞인다.
        picked["asks"] = list(chosen["asks"])
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

    items = _valid(state.get("items") or [state.get("item")], QA_ITEMS)
    kinds = _valid(state.get("kinds") or [state.get("kind")], QA_KINDS)
    if not items:
        bad = state.get("item")
        return {"status": "OUT_OF_SCOPE", "message": OUT_OF_SCOPE_ITEM.format(item=bad)}
    if not kinds:
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
    used_default = not (req.dates or state.get("asked")
                        or any(a.get("dates") for a in state.get("asks") or []))
    fallback = list(req.dates or state.get("asked") or [base_dt])
    last = base_dt + timedelta(days=QA_MAX_OFFSET)

    #   ★ **묶음이 있으면 그것만 본다.** 없으면 품목 x 가격을 곱한다.
    raw_asks = state.get("asks") or [
        {"item": item, "kind": kind} for item in items[:MAX_ITEMS] for kind in kinds[:MAX_KINDS]
    ]
    asks: list[dict[str, Any]] = []
    out_of_range: set[date] = set()
    for entry in raw_asks:
        if entry.get("item") not in QA_ITEMS or entry.get("kind") not in QA_KINDS:
            continue
        wanted = list(entry.get("dates") or fallback)
        asks.append({
            "item": entry["item"],
            "kind": entry["kind"],
            "targets": sorted({d for d in wanted if base_dt < d <= last}),
            "wants_today": any(d == base_dt for d in wanted),
        })
        out_of_range |= {d for d in wanted if d < base_dt or d > last}
    if not asks:
        return {"status": "OUT_OF_SCOPE", "message": OUT_OF_SCOPE_KIND}

    return {
        "base_dt": base_dt,
        "items": [a["item"] for a in asks],
        "kinds": [a["kind"] for a in asks],
        "item": asks[0]["item"],
        "kind": asks[0]["kind"],
        "asks": asks,
        #   옛 이름 — 첫 묶음 기준. 한 묶음짜리 검사·분기가 아직 쓴다.
        "wants_today": any(a["wants_today"] for a in asks),
        "targets": sorted({d for a in asks for d in a["targets"]}),
        "out_of_range": sorted(out_of_range),
        "used_default": used_default,
    }


def _valid(raw: Any, allowed: tuple[str, ...]) -> list[str]:
    """우리가 아는 값만 순서대로. 중복·빈 값은 버린다."""
    out: list[str] = []
    for value in raw or []:
        text = (value or "").strip() if isinstance(value, str) else ""
        if text in allowed and text not in out:
            out.append(text)
    return out


def after_gate(state: QaState) -> str:
    """읽을 것이 하나라도 있으면 조회하고, 없으면 곧장 답을 만든다."""
    if state.get("status"):
        return "compose"
    return "fetch" if (state.get("targets") or state.get("wants_today")) else "compose"


def fetch(state: QaState) -> QaState:
    """표를 읽는다. **조합마다 한 블록** (품목 x 가격 종류).

    ★ 「배추 경락가랑 도매가」처럼 여럿을 물으면 조합이 여럿이다. 전에는 한 조합만
      읽어 **중도매가만 답하고 경락가를 조용히 버렸다** (2026-09-15 실측).

    🔴 조합 수를 막아 둔다. 3품목 x 3가격 x 19일 = 171행이면 화면이 덮인다.
    """
    base_dt = state["base_dt"]
    asks = state.get("asks") or [
        {"item": state["item"], "kind": state["kind"],
         "targets": list(state.get("targets") or []),
         "wants_today": bool(state.get("wants_today"))}
    ]
    try:
        blocks: list[dict[str, Any]] = []
        fell_back = False
        for ask in asks[:MAX_ITEMS * MAX_KINDS]:
            item, kind = ask["item"], ask["kind"]
            rows = qa_tools.forecast_rows(item, kind, base_dt, ask["targets"])
            today = qa_tools.today_row(item, kind) if ask["wants_today"] else None
            if state.get("used_default") and today is None and not rows:
                #   ★ 오늘 값이 없는 아침도 있다. **빈 답을 주지 말고 내일로 물러선다.**
                fell_back = True
                rows = qa_tools.forecast_rows(
                    item, kind, base_dt, [base_dt + timedelta(days=1)]
                )
            blocks.append({
                "item": item,
                "kind": kind,
                "targets": ask["targets"],
                "rows": rows,
                "today": today,
                "accuracy": qa_tools.accuracy(item, kind),
                "usability": qa_tools.usability(item, kind),
            })
        first = blocks[0] if blocks else {}
        return {
            "blocks": blocks,
            "default_fell_back": fell_back,
            #   옛 이름 — 첫 조합을 가리킨다. 한 조합짜리 검사·호출이 아직 쓴다.
            "rows": first.get("rows") or [],
            "today": first.get("today"),
            "accuracy": first.get("accuracy"),
            "usability": first.get("usability"),
        }
    except Exception:                                        # noqa: BLE001
        return {"status": "SOURCE_UNAVAILABLE", "message": SOURCE_DOWN}


def compose(state: QaState) -> QaState:
    """유일한 출구. 정상 답·되묻기·거절이 **같은 자리에서** 나간다."""
    if state.get("status") and not state.get("blocks"):
        meta = QaMeta(
            status=state["status"],
            item=state.get("item"),
            kind=state.get("kind"),
            items=state.get("items") or [],
            kinds=state.get("kinds") or [],
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


def _block_table(block: dict[str, Any], state: QaState, many: bool) -> tuple[list[str], str]:
    """조합 하나를 표로. 돌려주는 것은 (줄 목록, 단위)."""
    item, kind = block["item"], block["kind"]
    rows, today = block["rows"], block["today"]
    base_dt = state["base_dt"]
    unit = (rows[0]["unit"] if rows else (today or {}).get("unit")) or "원/kg"

    out = [
        f"**{item} · {KIND_LABEL.get(kind, kind)} · 기준일 {base_dt}**",
        "",
        "| 날짜 | 예측 | 예상 구간 | 비고 |",
        "|---|---|---|---|",
    ]
    if today:
        out.append(_line(f"오늘 {today['target_dt']}", today, unit))
    for row in rows:
        out.append(_line(f"{row['target_dt']} (D+{row['offset_days']})", row, unit))

    #   ★ **설명 줄을 문장에 안 적는다** (2026-09-15 · 화면을 깨끗이 하라는 지시).
    #     출발점 · 평균 오차 · 값의 정체는 `meta` 로 옮겼다 — 없앤 것이 아니다.
    #
    #   🔴 하나만 문장에 남긴다: **쓰지 말라는 조합** 경고다. 그건 설명이 아니라
    #     «이 값으로 판단하지 마세요» 라는 판정이고, 못 보면 그대로 쓰게 된다.
    usab = block.get("usability") or {}
    if usab.get("use_recommended") is False:
        out.insert(
            1,
            "> 🔴 이 조합은 **판단에 쓰지 마세요.** 우리 모델보다 «어제 가격 그대로» 가 낫습니다."
            + (f" ({usab.get('quality_note')})" if usab.get("quality_note") else ""),
        )
    if many:
        out.append("")
    return out, unit


def _first_of(block: dict[str, Any], key: str) -> Any:
    """블록의 첫 행에서 한 칸. 행이 없으면 당일 값에서, 그것도 없으면 `None`."""
    rows = block.get("rows") or []
    if rows and rows[0].get(key) is not None:
        return rows[0][key]
    today = block.get("today") or {}
    return today.get(key)


def _answer_markdown(state: QaState) -> QaState:
    """조합마다 표 하나. **공통 안내는 한 번만** 적는다."""
    base_dt = state["base_dt"]
    blocks = state.get("blocks") or []
    many = len(blocks) > 1

    all_rows = [r for b in blocks for r in b["rows"]]
    todays = [b["today"] for b in blocks if b["today"]]
    missing = sorted({
        d
        for block in blocks
        for d in (set(block.get("targets") or []) - {r["target_dt"] for r in block["rows"]})
    })

    head: list[str] = []
    if many:
        #   ★ 여러 조합이면 무엇을 답했는지 맨 앞에 밝힌다 — 표가 길어 눈에 안 들어온다.
        names = " · ".join(
            f"{b['item']} {KIND_LABEL.get(b['kind'], b['kind'])}" for b in blocks
        )
        head = [f"**{names}** 를 모두 보여드립니다.", ""]

    body: list[str] = []
    for block in blocks:
        lines, _unit = _block_table(block, state, many)
        body += lines

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
    if all_rows:
        #   ★ **코드 이름을 문장에 넣지 않는다** (마스터 요청 · 2026-09-15).
        tail.append("\n*" + _kst(all_rows[0]["generated_at"]) + " 에 계산한 값입니다*")

    status = "OK"
    if state.get("out_of_range") or missing:
        status = "PARTIAL"
    if not all_rows and not todays:
        status = "NO_DATA"

    first = blocks[0] if blocks else {}
    meta = QaMeta(
        status=status,
        item=first.get("item"),
        kind=first.get("kind"),
        items=[b["item"] for b in blocks],
        kinds=[b["kind"] for b in blocks],
        base_dt=base_dt,
        targets=([r["target_dt"] for r in (first.get("rows") or [])]
                 + ([first["today"]["target_dt"]] if first.get("today") else [])),
        missing=missing,
        out_of_range=state.get("out_of_range") or [],
        model_version=(all_rows[0]["model_version"] if all_rows
                       else (todays[0] if todays else {}).get("model_version")),
        generated_at=_kst(all_rows[0]["generated_at"]) if all_rows else None,
        source=("ml_price_forecasts · prediction_log" if (all_rows and todays)
                else "prediction_log" if todays else "ml_price_forecasts"),
        is_filled=[bool(r.get("is_filled")) for r in (first.get("rows") or [])],
        band_method=(all_rows[0].get("band_method") if all_rows
                     else (todays[0] if todays else {}).get("band_method")),
        use_recommended=(first.get("usability") or {}).get("use_recommended"),
        #   ★ 문장에서 뺀 값들 — 여기로 옮겼다. 없앤 것이 아니다.
        current_price=_first_of(first, "current_price"),
        accuracy_pct=(first.get("accuracy") or {}).get("pct"),
        accuracy_note=qa_tools.SEALED_SOURCE if first.get("accuracy") else None,
        market_name=_first_of(first, "market_name"),
        grade_name=_first_of(first, "grade_name"),
        spec_desc=_first_of(first, "spec_desc"),
    )
    #   ★ 무엇을 무시했는지는 답 맨 앞에 적는다.
    note = [state["note"], ""] if state.get("note") else []
    return {
        "markdown": "\n".join(note + head + body + tail).rstrip(),
        "meta": meta,
        "status": status,
    }


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
    #   ★ 조합이 여럿이면 **행마다 어느 조합인지** 붙인다 (2026-09-15).
    #     안 붙이면 어댑터가 근거를 만들 때 배추 경락가와 배추 중도매가를 못 가린다.
    rows: list[dict] = []
    for block in final.get("blocks") or []:
        for row in [*block["rows"], *([block["today"]] if block["today"] else [])]:
            rows.append({**row, "item": block["item"], "kind": block["kind"]})
    return QaAnswer(
        markdown=final["markdown"],
        meta=final["meta"],
        rows_for_evidence=rows,
    )
