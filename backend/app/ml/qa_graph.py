"""예측 질의응답 — LangGraph 그래프 (노드 여섯 · 분기 하나 · 출구 하나).

```text
START → supervise → gate ─(읽을 날이 있다)→ fetch ─┐
                      └─(되묻기 · 범위 밖 · 값 없음)─┴→ batch → perf → compose → END
```

★ **갈래가 셋이다** (2026-09-16). 한 질문이 여럿을 물을 수 있다.

    forecast  가격이 얼마인가 · 얼마나 맞나 · 써도 되나
    batch     그날 배치가 어떻게 됐나 + 그날 AI 점검 보고서 (**그대로** 붙인다)
    perf      봉인 개봉 성능표 + 그날 재학습 후보 비교

🔴 **해석기를 하나 더 두지 않았다.** LLM 호출은 여전히 **한 번**이고, 그 한 번이
   `routes` 목록을 돌려준다. **갈래를 나눠 처리하는 것은 코드가 한다** — 노드마다
   따로 읽고 compose 가 부른 순서대로 이어 붙인다.

🔴 **한 갈래가 터져도 나머지는 나간다.** 배치를 못 읽었다고 성능 답까지 버리면,
   사람은 무엇이 고장인지 모른 채 빈 화면을 본다. 못 읽은 갈래만 한 줄로 말한다.

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

from app.master.clock import today_in_seoul
from app.ml import qa_llm, qa_tools
from app.ml.qa_schemas import (
    QA_ITEMS,
    QA_KINDS,
    QA_MAX_OFFSET,
    QA_ROUTES,
    QaAnswer,
    QaMeta,
    QaRequest,
)
from app.ml.schemas import SPEC

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


def _hhmm(value) -> str:
    """시각을 «09:00» 으로. **배치 기록은 UTC 라 한국 시간으로 돌린다.**

    🔴 한국 09:00 이 UTC 자정이다 (CLAUDE.md §9). 안 돌리면 09:00 배치가
      «00:00» 으로 보이고, 사람이 «한밤중에 돌았나» 로 읽는다.
    """
    if value is None:
        return "—"
    try:
        return value.astimezone(KST).strftime("%H:%M")
    except (AttributeError, ValueError, TypeError):
        return str(value)


def _stamp(value) -> str:
    """보고서 시각. **이미 한국 시간이라 돌리지 않는다** (`agent_report.ran_at`)."""
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError, TypeError):
        return str(value)


def _cell(value) -> str:
    """표 한 칸. 줄바꿈과 막대기는 표를 깨뜨리므로 눕힌다. **글자는 안 지운다.**"""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " · ").strip()

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

#: 날짜를 안 물어봐 하루만 답했을 때, **더 볼 수 있다고 알려주는 뒷문장.**
#:
#: 🔴 **「전부」라고 권하지 않는다** (2026-09-16 · 사용자 지시). 그 말은 우리
#:   해석기만 아는 말이고(`qa_llm.py` 의 지시문이 「전부」를 19일로 편다),
#:   **마스터를 거쳐 오면 못 알아듣는다.** 시키는 대로 «전부» 라고 말한 사람이
#:   답을 못 받는 안내였다.
#:
#: ★ 그래서 **되는 말을 예로 보인다.** 「일주일치」·「3일 뒤」는 날짜를 고르는
#:   보통 말이라 어느 길로 들어와도 읽힌다.
ASK_DATE_HINT = (
    "원하는 날짜나 기간을 말씀해주시면 해당 기간에 대한 가격을 보여드립니다. "
    "예) 일주일치의 가격 / 3일 뒤 가격"
)


class QaState(TypedDict, total=False):
    """그래프가 들고 다니는 것. 판정 결과만 담고 원본은 표에 둔다."""

    request: QaRequest
    routes: list[str]          # ★ 무엇을 물었나 — forecast · batch · perf (2026-09-16)
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

    #   ── 배치 갈래가 읽어 온 것 ──────────────────────────────────────
    #   🔴 «못 읽었다»(error) 와 «없다»(empty) 를 **한 칸에 담지 않는다.**
    #     둘을 뭉치면 창고가 죽은 날과 아직 안 돈 날이 같은 문장으로 나간다.
    batch_on: date
    batch_row: dict[str, Any] | None
    batch_fails: list[dict[str, Any]]
    batch_read: str            # ok · empty · error
    check_row: dict[str, Any] | None
    check_read: str
    #   ── 성능 갈래 ────────────────────────────────────────────────
    retrain_rows: list[dict[str, Any]]
    perf_read: str
    #   ★ 지금 무엇이 도나 (2026-09-16). 이름은 교체해도 그대로라 만든 날·학습 끝이
    #     같이 있어야 가려진다.
    models: list[dict[str, Any]]
    models_read: str           # ok · error
    cutover_read: str          # ok · absent · error
    #   ★ 바꿀 수 있는 후보가 있나 (2026-09-16). 이것만 DB 가 아니라 ML 콘솔에
    #     서버가 직접 물어 온다 — 「아직 안 눌렀나」는 그래프만 안다
    #     (`qa_tools.retrain_pending`).
    pending: list[dict[str, Any]]
    pending_read: str          # ok · error
    #   ★ 질문에 없어서 **전부로 채운** 자리 (2026-09-16 · 사용자 지시).
    filled_defaults: list[str]


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
        #   ★ 값을 직접 준 길은 **늘 가격 갈래다.** 배치·성능은 질문 문장으로만 온다.
        return {"routes": ["forecast"],
                "items": given_items, "kinds": given_kinds,
                "item": given_items[0], "kind": given_kinds[0]}
    if not req.question:
        if req.item and req.item not in QA_ITEMS:
            #   🔴 **범위 밖은 전부로 안 바꾼다.** 「대파」를 물었는데 배추가 나가면
            #     물은 것과 다른 답이다.
            return {"routes": ["forecast"], "status": "OUT_OF_SCOPE",
                    "message": OUT_OF_SCOPE_ITEM.format(item=req.item)}
        #   ★ **채팅 입구와 같은 규칙으로 채운다** (2026-09-16 · 사용자 결정 ④).
        #     전에는 여기서 «가격 종류를 알 수 없습니다» 로 되물었다. 그런데 이 입구는
        #     **부르는 것이 프로그램**이라 되물어도 다시 답할 수가 없다 — 되묻기가
        #     사람 입구보다 더 나쁘게 걸리는 자리였다.
        return _fill_defaults({"routes": ["forecast"]}, given_items, given_kinds)

    #   질문이 같이 왔으면 질문으로 답하고, 무엇을 무시했는지 밝힌다.
    ignored = req.item if (req.item and req.item not in QA_ITEMS) else None

    #   ★ 기준일을 먼저 잡는다 — 「내일」이 며칠인지는 기준일이 있어야 정해진다.
    #
    #   🔴 **여기서 바로 포기하지 않는다** (2026-09-16). 예전에는 예측표를 못 읽으면
    #     그 자리에서 «못 읽었습니다» 로 끝냈다. 그런데 배치·성능은 **다른 표**를
    #     읽는다 — 예측표가 죽었다고 «오늘 배치 잘 됐어?» 에 답을 못 할 이유가 없다.
    #     무엇을 물었는지 안 뒤에 판단한다.
    source_down = False
    try:
        base_dt = qa_tools.latest_base_date(req.as_of)
    except Exception:                                    # noqa: BLE001
        base_dt, source_down = None, True

    #   ★ **「오늘」은 화면의 기준일이다** (2026-09-15 · 사용자 지시).
    #     화면(3000)은 날짜를 걸으며 채팅마다 그 날을 `as_of` 로 싣는다. 전에는
    #     «as_of 이하 최신 예측일» 을 오늘로 셌다 — 그날 예측이 없으면(휴일 등)
    #     하루 이틀 전 날이 「오늘」이 됐다. 해석기는 as_of 로 날을 센다.
    #   ★ 벽시계는 `master/clock.py` 하나로만 읽는다 — 서버가 UTC 면 하루 밀린다.
    today = req.as_of or base_dt or today_in_seoul()
    chosen = qa_llm.interpret(req.question, today)
    if chosen is None:
        return {"routes": ["forecast"], "status": "LLM_UNAVAILABLE",
                "message": NEED_CLARIFY_LLM}

    routes, out_of_scope = _pick_routes(chosen)
    if out_of_scope and not routes:
        item = chosen.get("item")
        message = OUT_OF_SCOPE_ITEM.format(item=item) if item else OUT_OF_SCOPE_UNKNOWN
        return {"routes": ["forecast"], "status": "OUT_OF_SCOPE", "message": message}
    if not routes:
        #   ★ **아무 갈래도 못 정했다** — 되묻는 자리는 이제 여기 하나뿐이다
        #     (2026-09-16). 품목·가격 종류가 빠진 것은 되묻지 않고 전부로 채운다.
        return {"routes": ["forecast"], "status": "NEED_CLARIFY",
                "message": _ask_again(None, None, chosen.get("dates"))}

    picked: QaState = {"routes": routes}
    if base_dt is not None:
        picked["base_dt"] = base_dt
    if out_of_scope:
        #   ★ 「대파 값이랑 배치 상태」처럼 **반만 우리 것**인 질문이 있다.
        #     소관 밖이라고 배치 답까지 버리지 않는다 — 소관 밖이라는 말만 얹는다.
        item = chosen.get("item")
        picked["note"] = (
            OUT_OF_SCOPE_ITEM.format(item=item) if item else OUT_OF_SCOPE_UNKNOWN
        )
    if "forecast" not in routes:
        #   가격을 안 물었으면 예측표 사정은 답을 막지 않는다.
        return picked
    if source_down:
        return {**picked, "status": "SOURCE_UNAVAILABLE", "message": SOURCE_DOWN}
    if base_dt is None:
        return {**picked, "status": "NO_DATA", "message": "전달표에 예측이 아직 없습니다."}

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

    #   🔴 짝 물음이 있으면 안 채운다 — 짝에 이미 품목·가격이 들어 있어서,
    #     거기에 전부를 끼얹으면 **안 물어본 조합이 나간다.**
    #   🔴 범위 밖 품목(대파 등)은 위에서 이미 갈라져 나갔다. 여기로 안 온다.
    if asks:
        picked["filled_defaults"] = []
    else:
        _fill_defaults(picked, items, kinds)
        items = picked.get("items") or items
        kinds = picked.get("kinds") or kinds

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
        #   ★ 여기까지 왔는데 비어 있으면 **짝 물음이 엉터리인 경우**뿐이다
        #     (위에서 짝이 없을 때는 전부로 채웠다). 그때는 되묻는다.
        picked["status"] = "NEED_CLARIFY"
        picked["message"] = _ask_again(item, kind, picked.get("asked"))
    return picked


def _pick_routes(chosen: dict[str, Any]) -> tuple[list[str], bool]:
    """고른 갈래를 우리 어휘로. 돌려주는 것은 (갈래 목록, 소관 밖인가).

    ★ **옛 이름을 끊지 않는다.** `routes` 가 없으면 한 칸짜리 `route` 를 읽는다 —
      그 이름으로 부르는 코드와 검사가 아직 있다.

    ★ 모르는 값(`accuracy` · `usability`)은 **가격 갈래로 접는다.** 예전부터 그
      자리로 갔고, 되묻는 판단은 규칙이 한다.

    ★ **`clarify` 만 다르다** (2026-09-16). 그건 «못 알아들었다» 는 말이지 가격
      질문이 아니다. 가격으로 접어 버리면 품목·가격 종류를 전부로 채워
      **아무 말도 안 한 사람에게 아홉 개 표가 나간다.** 빈 목록을 돌려주고
      부르는 쪽이 되묻게 한다.
    """
    raw = chosen.get("routes")
    if not isinstance(raw, list) or not raw:
        raw = [chosen.get("route")]
    out_of_scope = False
    clarify = False
    out: list[str] = []
    for value in raw:
        text = str(value or "").strip()
        if text == "out_of_scope":
            out_of_scope = True
            continue
        if text in _CLARIFY_WORDS:
            clarify = True
            continue
        name = text if text in QA_ROUTES else "forecast"
        if name not in out:
            out.append(name)
    if out_of_scope:
        #   소관 밖이라고 한 갈래는 **가격 갈래다.** 배치·성능은 품목과 무관하다.
        out = [r for r in out if r != "forecast"]
        return out, True
    if not out and clarify:
        return [], False
    return out or ["forecast"], False


#: «못 알아들었다» 는 답. 이 말만 왔으면 갈래를 못 정한 것이다.
_CLARIFY_WORDS = frozenset({"clarify", "need_clarify"})


#: 되물을 때 붙이는 한 줄. **가격만 답하는 줄 알면 다음에도 가격만 묻는다.**
CAN_ALSO_ANSWER = (
    "가격 말고 **오늘 배치 결과**와 **모델 성능**도 물어보실 수 있습니다."
)


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
    if not item and not kind:
        #   ★ 아무것도 못 알아들었으면 **답할 수 있는 것을 다 알려준다** (2026-09-16).
        #     품목만 되물으면 «가격만 답하는 곳» 으로 읽혀 배치·성능을 영영 안 묻는다.
        lines.append(CAN_ALSO_ANSWER)
    return "\n\n".join(lines)


def _fill_defaults(picked: QaState, items: list[str], kinds: list[str]) -> QaState:
    """빠진 자리를 **전부로 채운다.** 무엇을 채웠는지 `filled_defaults` 에 남긴다.

    ★ **입구가 둘인데 규칙은 하나다** (2026-09-16 · 사용자 결정 ④).
      질문 문장으로 오든(`question`) 값으로 오든(`item`·`kind`), 빠진 쪽은 같은
      식으로 채운다. 입구가 다르다고 답이 달라지면 어느 쪽이 맞는지 알 수 없다.

    🔴 **되묻지 않는다.** 되물으면 사람은 한 번 더 쳐야 하고, 값으로 부르는
      프로그램은 **아예 다시 답할 수가 없다.** 대신 무엇을 채웠는지 답 첫 줄에
      한 줄로 밝힌다 (`_filled_line`).
    """
    filled: list[str] = []
    if not items:
        items = list(QA_ITEMS)
        filled.append("품목")
    if not kinds:
        kinds = list(QA_KINDS)
        filled.append("가격 종류")
    picked["items"] = list(items)
    picked["item"] = items[0]
    picked["kinds"] = list(kinds)
    picked["kind"] = kinds[0]
    picked["filled_defaults"] = filled
    return picked


def gate(state: QaState) -> QaState:
    """고른 값이 범위 안인가. **LLM 이 고른 값을 그대로 쿼리에 넣지 않는다.**"""
    if state.get("status"):
        return {}
    if "forecast" not in (state.get("routes") or ["forecast"]):
        #   가격을 안 물었으면 검사할 품목·날짜가 없다. 배치·성능은 여기를 안 지난다.
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
    fallback = list(req.dates or state.get("asked") or [req.as_of or base_dt])
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

    #   ★ **범위 안 날이 하나도 없으면 오늘 값을 주지 않는다** (2026-09-15 · 화면).
    #     「30일 뒤」만 물었는데 오늘 값이 나가면, 물은 것과 다른 날을 답한 것이다.
    #     범위 밖 안내만 준다.
    if out_of_range and not any(a["targets"] or a["wants_today"] for a in asks):
        today = req.as_of or base_dt
        days = " · ".join(
            f"{d} ({(d - today).days:+d}일)" for d in sorted(out_of_range)
        )
        return {
            "status": "OUT_OF_SCOPE",
            "base_dt": base_dt,
            "out_of_range": sorted(out_of_range),
            "items": [a["item"] for a in asks],
            "kinds": [a["kind"] for a in asks],
            "message": (
                f"{days} 은 예측 범위 밖입니다. "
                f"{today} 부터 {today + timedelta(days=QA_MAX_OFFSET)} 까지 답할 수 있습니다."
            ),
        }

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
    """읽을 예측이 있으면 조회하고, 없으면 건너뛴다.

    ★ 건너뛰어도 **출구로 바로 가지 않는다** (2026-09-16). 배치·성능 노드가 뒤에
      있어서, 가격을 안 물었거나 되묻기로 빠진 질문도 그 갈래는 답해야 한다.
    """
    if state.get("status"):
        return "batch"
    return "fetch" if (state.get("targets") or state.get("wants_today")) else "batch"


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
            today = (qa_tools.today_row(item, kind, base_dt)
                     if ask["wants_today"] else None)
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


def _asked_on(state: QaState) -> date:
    """어느 날을 묻는가. **화면 기준일이고, 없으면 오늘이다.**

    ★ 예측 기준일(`base_dt`)을 쓰지 않는다. 배치·보고서는 **예측표와 다른 표**라
      예측이 없는 날에도 배치는 돌았을 수 있다.

    🔴 **`date.today()` 를 안 쓴다** (`master/clock.py`). 서버가 UTC 면 한국 09:00 이
      UTC 자정이라 **날짜가 하루 밀린다** — 09:00 배치를 물었는데 어제 것을 읽는다.
      CLAUDE.md §9 에 같은 사고가 적혀 있다.
    """
    return state["request"].as_of or today_in_seoul()


def batch_node(state: QaState) -> QaState:
    """그날 배치 한 행 + 실패한 단계 + 그날 AI 점검 보고서. **셋 다 읽기만 한다.**

    🔴 **둘을 따로 감싼다.** 배치 행을 못 읽어도 보고서는 나갈 수 있고 그 반대도 된다.
      하나로 묶으면 한 번의 실패가 둘을 다 지운다.
    """
    if "batch" not in (state.get("routes") or []):
        return {}
    on = _asked_on(state)
    out: QaState = {"batch_on": on}

    try:
        row = qa_tools.batch_run(on)
        out["batch_row"] = row
        out["batch_fails"] = qa_tools.failed_stages(row["run_id"]) if row else []
        out["batch_read"] = "ok" if row else "empty"
    except Exception:                                        # noqa: BLE001
        out["batch_read"] = "error"

    try:
        report = qa_tools.agent_report(qa_tools.CHECK_REPORT, on)
        out["check_row"] = report
        out["check_read"] = "ok" if report else "empty"
    except Exception:                                        # noqa: BLE001
        out["check_read"] = "error"
    return out


def perf_node(state: QaState) -> QaState:
    """봉인 개봉 성능표는 **상수**라 못 읽을 일이 없다. 읽는 것은 둘이다.

    ```text
    현재 모델      prediction_log + model_cutover
    재학습 보고서   그날 것 **전부** (판정 3건 · 검증 2건인 날이 있다)
    ```

    🔴 **따로 감싼다.** 현재 모델을 못 읽어도 재학습 이야기는 나갈 수 있고,
      그 반대도 된다. 하나로 묶으면 한 번의 실패가 둘을 다 지운다.
    """
    if "perf" not in (state.get("routes") or []):
        return {}
    on = _asked_on(state)
    out: QaState = {}

    try:
        found = qa_tools.current_models()
        out["models"] = list(found.get("models") or [])
        out["cutover_read"] = str(found.get("cutover_read") or "ok")
        out["models_read"] = "ok"
    except Exception:                                        # noqa: BLE001
        out["models"] = []
        out["models_read"] = "error"
        out["cutover_read"] = "error"

    rows: list[dict[str, Any]] = []
    read = "ok"
    try:
        for name in qa_tools.RETRAIN_REPORTS:
            #   ★ **그날 것을 전부** 읽는다 (2026-09-16). 마지막 하나만 읽었더니
            #     경락가 검증의 «후보가 나쁩니다» 가 답에서 통째로 빠졌다.
            rows += qa_tools.agent_reports(name, on)
    except Exception:                                        # noqa: BLE001
        read = "error"
    out["retrain_rows"] = rows
    out["perf_read"] = read

    #   ★ **또 따로 감싼다.** ML 콘솔이 안 떠 있어도 위의 성능표와 보고서는
    #     그대로 나가야 한다. 못 읽으면 답에 그렇게 한 줄 적는다.
    try:
        out["pending"] = qa_tools.retrain_pending()
        out["pending_read"] = "ok"
    except Exception:                                        # noqa: BLE001
        out["pending"] = []
        out["pending_read"] = "error"
    return out


#: 가격 갈래가 답을 못 냈을 때, 그것이 «못 읽음» 인가 «없음» 인가.
#: 되묻기·소관 밖은 **답을 한 것**이다 — 실패로 세지 않는다.
_FORECAST_GRADE = {
    "SOURCE_UNAVAILABLE": "error",
    "LLM_UNAVAILABLE": "error",
    "NO_DATA": "empty",
}


def _grade(grades: list[str]) -> str:
    """갈래 하나의 성적. **하나라도 나갔으면 ok** — 나머지는 한 줄로 말한다."""
    if "ok" in grades:
        return "ok"
    return "error" if "error" in grades else "empty"


def _batch_section(state: QaState) -> tuple[str, str]:
    """배치 갈래의 글 한 덩어리. 돌려주는 것은 (글, 성적)."""
    on = state.get("batch_on") or _asked_on(state)
    lines = [f"**배치 — {on}**", ""]
    grades: list[str] = []

    row = state.get("batch_row")
    if state.get("batch_read") == "error":
        lines.append("배치 기록을 읽지 못했습니다.")
        grades.append("error")
    elif not row:
        lines.append(f"{on} 배치 기록이 없습니다.")
        grades.append("empty")
    else:
        status = qa_tools.BATCH_STATUS_LABEL.get(str(row.get("status")), str(row.get("status")))
        lines += [
            "| 항목 | 값 |",
            "|---|---|",
            f"| 상태 | {status} |",
            f"| 시작 | {_hhmm(row.get('started_at'))} |",
            f"| 끝 | {_hhmm(row.get('finished_at'))} |",
            f"| 끝난 단계 | {row.get('n_ok')} |",
            f"| 실패한 단계 | {row.get('n_fail')} |",
        ]
        fails = state.get("batch_fails") or []
        if fails:
            #   ★ 실패는 **숨기지 않는다.** 단계 이름은 사람 말로 바꿔 적되,
            #     모르는 단계면 이름을 그대로 적는다 — 안 적는 것보다 낫다.
            lines += ["", "| 실패한 단계 | 남긴 말 |", "|---|---|"]
            for fail in fails:
                stage = str(fail.get("stage") or "")
                lines.append(
                    f"| {qa_tools.STAGE_LABEL.get(stage, stage)} "
                    f"| {_cell(fail.get('message'))} |"
                )
        grades.append("ok")

    report = state.get("check_row")
    if state.get("check_read") == "error":
        lines += ["", "점검 보고서를 읽지 못했습니다."]
        grades.append("error")
    elif not report or not report.get("body"):
        lines += ["", f"{on} 점검 보고서가 아직 없습니다."]
        grades.append("empty")
    else:
        #   🔴 **요약하지 않는다.** 이 글은 이미 사람이 읽으라고 쓰인 것이고,
        #     다시 줄이면 우리가 고른 것만 남는다.
        lines += ["", f"**AI 점검 보고서 — {_stamp(report.get('ran_at'))}**", "",
                  str(report["body"]).strip()]
        grades.append("ok")

    return "\n".join(lines), _grade(grades)


#: 보고서가 제목 맨 앞에 쓰는 가격 종류 코드. **답 문장에서는 사람 말로 바꾼다.**
_KIND_CODE = {"auc": "경락가", "whsl": "중도매가", "rtl": "소매가"}


def _kindly(title: Any) -> str:
    """«rtl 무 — …» 를 «소매가 무 — …» 로. **맨 앞 한 낱말만** 바꾼다.

    🔴 판정 문구는 손대지 않는다 (CLAUDE.md §12 · 지시 4). 바꾸는 것은 가격 종류
      코드 하나뿐이고, 그건 뜻이 아니라 이름이다 — 문장 속 다른 곳은 건드리지 않는다.
    """
    text = str(title or "")
    head, sep, rest = text.partition(" ")
    if head.lower() in _KIND_CODE:
        return _KIND_CODE[head.lower()] + sep + rest
    return text


#: 가격 종류를 못 가렸을 때. **지어내지 않는다** — 검증 보고서가 실제로 이렇다.
KIND_UNKNOWN = "(가격 종류 미상)"


def _report_kind(report: dict[str, Any]) -> str:
    """보고서 하나가 어느 가격 종류인가. **있는 것만 본다, 순서대로.**

    ```text
    ① payload.kind        다른 일꾼이 붙이는 중 — 붙으면 이게 가장 정확하다
    ② 제목 맨 앞 낱말      auc · whsl · rtl        (판정 보고서에만 있다)
    ③ 없으면 «(가격 종류 미상)»
    ```

    🔴 ③ 을 추측으로 메우지 않는다. 2026-09-16 검증 보고서 둘은 제목이
      «견주는 창» · «배추 — …» 로 시작해 가격 종류가 **어디에도 안 적혀 있다.**
      학습 끝 날짜로 되짚으면 맞힐 수야 있지만, 그건 우리가 지어낸 것이다.
    """
    payload = report.get("payload") or {}
    code = str(payload.get("kind") or "").strip().lower()
    if code in _KIND_CODE:
        return _KIND_CODE[code]
    for finding in payload.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        head = str(finding.get("title") or "").partition(" ")[0].lower()
        if head in _KIND_CODE:
            return _KIND_CODE[head]
    return KIND_UNKNOWN


def _numbers_of(report: dict[str, Any]) -> list[dict[str, str]]:
    """보고서 안의 수치 묶음을 `{이름: 값}` 으로. 짝이 아닌 것은 버린다."""
    out: list[dict[str, str]] = []
    for finding in (report.get("payload") or {}).get("findings") or []:
        if not isinstance(finding, dict):
            continue
        out.append({
            str(pair[0]): str(pair[1])
            for pair in finding.get("numbers") or []
            if len(pair) >= 2
        })
    return out


#: 교체 직후라 견줄 것이 없을 때 붙이는 줄. **«문제 없음» 이 아니다.**
SAME_TRAIN_END = (
    "현행과 후보의 학습 끝이 같습니다 — 교체 직후라 견줄 새 후보가 없습니다"
)


def _same_train_end(report: dict[str, Any]) -> bool:
    """현행과 후보의 학습 끝이 같은가. **payload 에서 정확히 읽힐 때만** 참이다.

    2026-09-16 소매가 검증이 그 경우다 — 둘 다 2025-12-31 이고 WMAPE 가 소수점까지
    같아 세 품목 모두 «판정 불가» 가 나왔다. 그건 모델이 이상해서가 아니라
    **견줄 새 후보가 없어서**인데, 그 말이 어디에도 안 적혀 있었다.
    """
    for values in _numbers_of(report):
        now, cand = values.get("현행 학습 끝"), values.get("후보 학습 끝")
        if now and cand:
            return now == cand
    return False


def _candidate_line(report: dict[str, Any]) -> str:
    """«재학습 후보 n개» 한 줄. **글자 그대로** 옮긴다. 없으면 «—»."""
    for finding in (report.get("payload") or {}).get("findings") or []:
        if not isinstance(finding, dict):
            continue
        title = str(finding.get("title") or "")
        if title.startswith("재학습 후보"):
            return title
    return "—"


def _has_compare(finding: dict[str, Any]) -> bool:
    """현행과 후보를 나란히 잰 칸인가. **라벨을 지어내지 않고 있는 것만 본다.**"""
    labels = {str(pair[0]) for pair in finding.get("numbers") or [] if len(pair) >= 2}
    return "현행 WMAPE" in labels and "후보 WMAPE" in labels


def _retrain_block(report: dict[str, Any]) -> list[str]:
    """재학습 보고서 하나를 표로. **판정 문구를 고쳐 쓰지 않는다.**

    🔴 «판정 불가» 를 «문제 없음» 으로 바꾸면 안 된다 — 증명을 못 한 것과 문제가
      없는 것은 다르다 (CLAUDE.md §11). 제목과 수치를 **있는 그대로** 옮긴다.
    """
    payload = report.get("payload") or {}
    findings = [f for f in (payload.get("findings") or []) if isinstance(f, dict)]
    head = (
        f"**{report.get('name')} · {_report_kind(report)} "
        f"— {_stamp(report.get('ran_at'))} · 판정 {report.get('verdict') or '—'}**"
    )
    out = ["", head, ""]

    compare = [f for f in findings if _has_compare(f)]
    others = [f for f in findings if not _has_compare(f)]

    if compare:
        columns: list[str] = []
        for finding in compare:
            for pair in finding["numbers"]:
                if str(pair[0]) not in columns:
                    columns.append(str(pair[0]))
        out.append("| 대상 | " + " | ".join(columns) + " | 판정 |")
        out.append("|" + "---|" * (len(columns) + 2))
        for finding in compare:
            values = {str(p[0]): str(p[1]) for p in finding["numbers"] if len(p) >= 2}
            target, _, verdict = _kindly(finding.get("title")).partition(" — ")
            cells = [_cell(target)] + [_cell(values.get(c, "—")) for c in columns]
            cells.append(_cell(verdict or finding.get("level")))
            out.append("| " + " | ".join(cells) + " |")
        out.append("")

    if others:
        out += ["| 항목 | 수치 |", "|---|---|"]
        for finding in others:
            numbers = " · ".join(
                f"{p[0]} {p[1]}" for p in finding.get("numbers") or [] if len(p) >= 2
            )
            out.append(f"| {_cell(_kindly(finding.get('title')))} | {_cell(numbers) or '—'} |")
    if compare and _same_train_end(report):
        #   ★ 표 **아래** 한 줄. 숫자를 먼저 보이고 왜 그런지를 뒤에 붙인다.
        out += ["", f"> {SAME_TRAIN_END}."]
    return out


def _day(value) -> str:
    """날짜 한 칸. 시각이 붙어 있어도 **날짜만** 적는다."""
    if value is None:
        return "—"
    try:
        return value.strftime("%Y-%m-%d")
    except (AttributeError, ValueError, TypeError):
        return str(value)


def _minute(value) -> str:
    """교체 시각. `swapped_at` 은 **이미 한국 시간**이라 돌리지 않는다."""
    if value is None:
        return "—"
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError, TypeError):
        return str(value)


#: 교체 이력을 못 읽었을 때 «최근 교체» 칸에 적을 말.
_SWAP_CELL = {"absent": "교체 이력 없음", "error": "교체 이력을 읽지 못했습니다"}


def _models_table(state: QaState) -> list[str]:
    """**지금 무엇이 도나.** 이름 · 만든 날 · 학습 끝 · 최근 교체.

    🔴 **이름만으로는 알 수 없다.** `ops_rtl` 은 모델을 갈아 끼워도 그대로다 —
      매입 파트 필터가 이름 정확히 일치라 바꾸면 에러 없이 0건이 된다.
      2026-09-15 저녁에 소매가 모델이 바뀌었는데 답에 그 사실이 한 글자도
      안 남아 있었다. 그래서 **만든 날**을 같이 적는다.

    ★ 모델 이름(`ops_rtl`)은 코드 이름이지만 **사용자가 보여 달라고 한 것**이라
      그대로 적는다. 답 문장에서 코드 이름을 빼는 규칙의 예외다.
    """
    if state.get("models_read") == "error":
        return ["**현재 모델**", "", "현재 모델을 읽지 못했습니다."]

    models = state.get("models") or []
    if not models:
        return ["**현재 모델**", "", "현재 모델 기록이 없습니다."]

    cutover = state.get("cutover_read") or "ok"
    lines = [
        "**현재 모델**",
        "",
        "| 가격 | 이름 | 만든 날 | 학습 끝 | 최근 교체 |",
        "|---|---|---|---|---|",
    ]
    for row in models:
        kind = str(row.get("kind") or "")
        label = KIND_LABEL.get(kind, kind)
        swapped = row.get("last_swapped_at")
        if swapped is not None:
            #   ★ **시각을 모르면 날짜만 적는다** (2026-09-16 · 결정 ⑥).
            #     백필한 행은 백업 폴더 이름에서 되짚은 것이라 날짜만 확실한 것이
            #     있다. `00:00` 을 그대로 보이면 «한밤중에 바꿨나» 로 읽힌다 —
            #     실제로는 **모른다** 다.
            #
            #   🔴 칸이 아직 없으면(`None`) **예전 그대로** 날짜·시각을 적는다.
            #     «모른다» 와 «안 물어봤다» 를 같은 모양으로 내보내지 않는다.
            swap = (
                f"{_day(swapped)} (시각 미상)"
                if row.get("last_swap_time_known") is False
                else _minute(swapped)
            )
        else:
            #   «표가 아직 없다» 와 «표는 있는데 이 종류가 안 바뀐 적이 없다» 를
            #   섞지 않는다. 앞은 안내, 뒤는 사실이다.
            swap = _SWAP_CELL.get(cutover, "교체 기록 없음")
        lines.append(
            f"| {label} | {_cell(row.get('model_ver'))} "
            f"| {_day(row.get('created_at'))} | {_day(row.get('train_end'))} | {swap} |"
        )
    #   🔴 **꼬리말(`note`)은 안 적는다** (2026-09-16 · 결정 ⑥).
    #     실제 `note` 가 300자가 넘어 답이 꼬리말 세 줄로 덮였다. 칸에 넣었다가
    #     표 밖으로 내렸다가, 결국 뺐다. 그것이 말하려던 «시각이 추정이다» 는
    #     «(시각 미상)» 이 대신하고 **원문은 `model_cutover.note` 에 그대로 있다.**
    return lines


#: 콘솔에 못 물었을 때 적는 한 줄. **«후보 없음» 과 다른 말이다** — 앞은
#: 「모르겠다」, 뒤는 「봤는데 없다」다. 섞으면 콘솔이 죽은 동안 사람이
#: «바꿀 것이 없구나» 로 읽는다.
UPDATE_UNREADABLE = "업데이트 후보 확인 불가 (ML 콘솔 연결 안 됨)."

#: 버튼을 마크다운에 싣는 방법. **평범한 링크가 아니라 `action:` 스킴**이다.
#:
#: ★ 채팅으로 가는 길에 살아남는 것은 `answer_markdown` **한 덩어리뿐**이다
#:   (`master/answer.py::_MARKDOWN_AGENTS`). 나머지 payload 칸은 마스터가 사실
#:   줄로 펴 버려 화면 거품 안으로 «구조» 가 못 들어온다. 그래서 버튼을 글 안에
#:   싣는다 — 화면(`ml/Markdownish.tsx`)이 이 스킴만 버튼으로 그린다.
#:
#: 🔴 **경로를 여기서 지어내지 않는다.** 누르면 재학습 탭이 쓰는 그 함수
#:   (`lib/mlConsole.ts::graphAct(kind, "apply")`)가 그대로 돈다.
UPDATE_ACTION = "action:retrain-apply?kind={kind}"


def _candidate_train_end(state: QaState, label: str) -> str:
    """후보가 **어디까지 배웠나**. 그날 검증 보고서에 적혀 있을 때만 돌려준다.

    🔴 `/retrain/pending` 에는 이 값이 **없다** (2026-09-16 실측 · 그 행은
      `kind` · `state` · `sec` · `candidate` · `items` · `verify` 뿐이고,
      같이 오는 `current_models` 의 `train_end` 는 **현행** 것이다).
      후보 이름 `ops_rtl_cand_20260916` 의 날짜는 **만든 날**이지 학습 끝이
      아니다. 그걸로 되짚으면 맞을 때도 있지만 그건 우리가 지어낸 것이다.

    그래서 «후보 학습 끝» 이라고 **적혀 있는 곳**에서만 가져오고, 없으면
    빈 글자를 돌려준다 — 부르는 쪽이 괄호째 뺀다.
    """
    for report in state.get("retrain_rows") or []:
        if _report_kind(report) != label:
            continue
        for values in _numbers_of(report):
            end = values.get("후보 학습 끝")
            if end:
                return str(end)
    return ""


def _update_lines(state: QaState) -> list[str]:
    """«바꿀 수 있는 후보가 있다» 와 그 버튼. **없으면 한 줄도 안 적는다.**

    ★ 자리는 **답의 맨 아래**다 (`_perf_section` 머리말). 현재 모델 · 성능 아홉 칸 ·
      재학습 판정과 검증 표를 다 보인 **뒤에** 묻는다 — 누를지 정할 근거를 보기
      전에 버튼이 먼저 나오면 안 된다.
    """
    read = state.get("pending_read")
    if read is None:
        return []                                 # 성능 갈래가 아니다 — 물어본 적도 없다
    if read == "error":
        return ["", UPDATE_UNREADABLE]

    out: list[str] = []
    for row in state.get("pending") or []:
        kind = str(row.get("kind") or "").strip().lower()
        label = _KIND_CODE.get(kind)
        if not label:
            #   버튼을 만들 수 없는 종류다. 누를 수 없는 버튼을 그리지 않는다.
            continue
        end = _candidate_train_end(state, label)
        when = f" (학습 끝 {end})" if end else ""
        out += [
            "",
            f"**{label} 후보{when} — 현행보다 나음 · 업데이트할 수 있습니다**",
            "",
            f"[모델 업데이트 — {label}]({UPDATE_ACTION.format(kind=kind)})",
        ]
    return out


def _judge_table(reports: list[dict[str, Any]]) -> list[str]:
    """재학습 **판정**은 한 줄씩. 펼치면 하루 세 건이라 답을 덮는다.

    🔴 줄이되 **판정 문구는 글자 그대로** 옮긴다 (CLAUDE.md §12). «재학습 후보 2개»
      는 우리가 센 것이 아니라 보고서가 쓴 말이다.
    """
    if not reports:
        return []
    out = [
        "",
        f"**재학습판정 — {len(reports)}건**",
        "",
        "| 가격 | 시각 | 판정 | 후보 |",
        "|---|---|---|---|",
    ]
    for report in reports:
        out.append(
            f"| {_report_kind(report)} | {_stamp(report.get('ran_at'))} "
            f"| {_cell(report.get('verdict')) or '—'} | {_cell(_candidate_line(report))} |"
        )
    return out


def _perf_section(state: QaState) -> tuple[str, str]:
    """성능 갈래의 글. **현재 모델 + 봉인 개봉 아홉 칸 + 그날 재학습 이야기.**

    ★ 조건을 값과 **떼어 놓지 않는다** (CLAUDE.md §11). 표 제목에 언제·무엇으로
      잰 값인지가 늘 같이 간다.

    ★ 순서가 뜻이다 — **지금 무엇이 도나**를 먼저 보이고, 그 다음이 그 모델이
      얼마나 맞히나, 그 다음이 그날 무엇을 견줬나, **맨 마지막이 그래서 바꿀
      것이 있나**다.

    🔴 **업데이트 줄과 버튼은 맨 아래다** (2026-09-16 · 사용자 지시). 처음에는
      «현재 모델» 표 바로 아래에 뒀는데, 그러면 **누를지 정할 근거(검증 표)를
      보기 전에 버튼이 먼저 나온다.** 숫자를 다 보이고 나서 묻는다.

    ★ **나가는 문이 하나다.** 재학습 기록을 못 읽어도 그 줄 다음에 버튼이 붙게
      중간에서 빠져나가지 않는다 — 갈라 두면 한쪽에만 버튼이 붙는다.
    """
    lines = _models_table(state)
    lines += [
        "",
        f"**모델 성능 — {qa_tools.SEALED_SOURCE}**",
        "",
        "| 품목 | 가격 | 평균 실제가 | 평균 오차 | 오차율 |",
        "|---|---|---|---|---|",
    ]
    for (kind, item), cell in qa_tools.SEALED_ACCURACY.items():
        lines.append(
            f"| {item} | {KIND_LABEL.get(kind, kind)} "
            f"| {cell['avg']} | {cell['err']} | {cell['pct']}% |"
        )

    if state.get("perf_read") == "error":
        lines += ["", "재학습 기록을 읽지 못했습니다."]
    else:
        rows = state.get("retrain_rows") or []
        if not rows:
            lines += ["", "재학습 후보 없음. 지금 모델을 그대로 씁니다."]
        else:
            #   ★ **판정은 요약, 검증은 표 전부** (2026-09-16 · 되물음 ① 결정).
            #     검증은 «현행 vs 후보» 숫자가 판단의 근거라 줄이면 못 읽는다.
            lines += _judge_table([r for r in rows if r.get("name") == "재학습판정"])
            for report in rows:
                if report.get("name") != "재학습판정":
                    lines += _retrain_block(report)

    #   ★ **맨 아래.** 숫자를 다 보인 뒤에 «그래서 바꿀까요» 를 묻는다
    lines += _update_lines(state)
    return "\n".join(lines), "ok"


def compose(state: QaState) -> QaState:
    """유일한 출구. 정상 답·되묻기·거절·배치·성능이 **같은 자리에서** 나간다.

    ★ 갈래를 **부른 순서대로** 이어 붙인다. 「5일 뒤 배추 경락가랑 배치 상태」면
      가격 표가 먼저고 배치가 뒤다 — 물어본 차례가 답의 차례다.
    """
    routes = state.get("routes") or ["forecast"]
    parts: list[str] = []
    grades: list[str] = []
    forecast_status: str | None = None
    meta: QaMeta | None = None

    for route in routes:
        if route == "forecast":
            if state.get("status") and not state.get("blocks"):
                forecast_status = state["status"]
                meta = QaMeta(
                    status=forecast_status,                  # type: ignore[arg-type]
                    item=state.get("item"),
                    kind=state.get("kind"),
                    items=state.get("items") or [],
                    kinds=state.get("kinds") or [],
                    base_dt=state.get("base_dt"),
                    out_of_range=state.get("out_of_range") or [],
                )
                parts.append(state.get("message", ""))
                grades.append(_FORECAST_GRADE.get(forecast_status, "ok"))
            else:
                built = _answer_markdown(state)
                forecast_status = built["status"]
                meta = built["meta"]
                parts.append(built["markdown"])
                grades.append("empty" if forecast_status == "NO_DATA" else "ok")
        elif route == "batch":
            body, grade = _batch_section(state)
            parts.append(body)
            grades.append(grade)
        elif route == "perf":
            body, grade = _perf_section(state)
            parts.append(body)
            grades.append(grade)

    if meta is None:
        meta = QaMeta(status="OK", base_dt=state.get("base_dt"))
    meta.status = _combined_status(routes, forecast_status, grades)  # type: ignore[assignment]
    meta.routes = list(routes)
    note = [state["note"], ""] if state.get("note") else []
    return {
        "markdown": "\n\n".join(note + [p for p in parts if p]).strip(),
        "meta": meta,
        "status": meta.status,
    }


def _combined_status(routes: list[str], forecast: str | None, grades: list[str]) -> str:
    """갈래 여럿의 상태를 하나로.

    ★ **가격만 물었으면 예전 값 그대로다.** 갈래를 늘렸다고 이미 나가던 답의
      상태가 바뀌면, 그 값으로 분기하는 마스터 쪽이 조용히 달라진다.
    """
    if routes == ["forecast"]:
        return forecast or "OK"
    if grades and all(g == "error" for g in grades):
        return "SOURCE_UNAVAILABLE"
    if "error" in grades:
        return "PARTIAL"
    if grades and all(g == "empty" for g in grades):
        return "NO_DATA"
    if forecast in {"PARTIAL", "NEED_CLARIFY", "OUT_OF_SCOPE"}:
        return "PARTIAL"
    return "OK"


def _line(label: str, row: dict[str, Any], unit: str, note: str = "") -> str:
    if row.get("is_filled"):
        note = (note + " · " if note else "") + "휴일의 경우 직전 예측값을 사용합니다."
    #   ★ 「모델 대신 출발점을 그대로 씀」은 **문장에서 뺐다** (2026-09-15 · 화면에서 발견).
    #     표 아래 설명 줄(출발점)을 뺄 때 비고 칸의 이 문구를 놓쳤다. 출발점이라는 말을
    #     화면에서 없앴는데 비고에만 남아 뜻 모를 말이 됐다. 값은 meta.is_gated 로 간다.
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

    as_of = state["request"].as_of or base_dt
    out = [
        f"**{item} · {KIND_LABEL.get(kind, kind)} · 기준일 {as_of}**",
        "",
        "| 날짜 | 예측 | 예상 구간 | 비고 |",
        "|---|---|---|---|",
    ]
    if today:
        out.append(_line(f"오늘 {today['target_dt']}", today, unit))
    for row in rows:
        #   화면 기준일과 같은 날이면 「오늘」로 적는다 — 예측일이 하루 앞서 계산된
        #   날(휴일 뒤 등)에도 사람이 보는 「오늘」은 화면 날짜다.
        if row["target_dt"] == as_of:
            label = f"오늘 {row['target_dt']}"
        else:
            label = f"{row['target_dt']} (D+{(row['target_dt'] - as_of).days})"
        out.append(_line(label, row, unit))

    #   ★ **설명 줄을 문장에 안 적는다** (2026-09-15 · 화면을 깨끗이 하라는 지시).
    #     출발점 · 평균 오차 · 값의 정체는 `meta` 로 옮겼다 — 없앤 것이 아니다.
    #
    #   ★ **«쓰지 마세요» 경고도 뺐다** (2026-09-16 · 사용자 결정 ⑦). 마지막까지
    #     문장에 남겨 두었던 한 줄인데, 가격 답은 표만 깔끔하게 두기로 했다.
    #
    #   🔴 **없앤 것이 아니라 옮긴 것이다.** `use_recommended` · `quality_note` ·
    #     `is_gated` 는 `meta` 와 마스터 payload 에 그대로 간다 — **판단하는 쪽이
    #     저기다.** 문장에서 빼는 것과 값에서 지우는 것은 전혀 다른 일이다.
    if many:
        out.append("")
    return out, unit


def _first_of(block: dict[str, Any], key: str) -> Any:
    """블록의 첫 행에서 한 칸. 없으면 당일 값에서, 그것도 없으면 규격표에서.

    🔴 **당일 행에는 규격 칸이 아예 없다** (2026-09-15 실측). 원본 창고는
      `market_name` · `grade_name` · `spec_desc` 를 담지 않는다. 그래서 오늘 값만
      답할 때 규격이 통째로 비었다 — 문장에서 뺀 값이 **정말로 사라진** 것이다.

      `SPEC` 은 우리가 쥔 상수이므로 거기서 채운다. 지어내는 것이 아니라
      **같은 사실을 다른 자리에서** 가져오는 것이다.
    """
    rows = block.get("rows") or []
    if rows and rows[0].get(key) is not None:
        return rows[0][key]
    today = block.get("today") or {}
    if today.get(key) is not None:
        return today[key]
    spec = SPEC.get(block.get("kind") or "") or {}
    if key == "market_name":
        return spec.get("market")
    if key == "grade_name":
        return spec.get("grade")
    if key == "spec_desc":
        return (spec.get("desc") or {}).get(block.get("item"))
    return None


def _filled_line(filled: list[str], blocks: list[dict[str, Any]]) -> str:
    """무엇을 기본값으로 채웠는지 한 줄. **채운 것만 적는다.**

    ★ 실제로 답한 것을 적는다 — 상수 목록이 아니라 블록에서 뽑는다. 둘이 어긋나면
      «전부 보여드립니다» 라고 써 놓고 일부만 나가는 답이 된다.
    """
    said: list[str] = []
    if "품목" in filled:
        names = list(dict.fromkeys(b["item"] for b in blocks))
        said.append("**" + " · ".join(names) + "**")
    if "가격 종류" in filled:
        names = list(dict.fromkeys(KIND_LABEL.get(b["kind"], b["kind"]) for b in blocks))
        said.append("**" + " · ".join(names) + "**")
    #   ★ 조사를 붙여 둔다 — «품목를» 이 나오면 사람이 먼저 그걸 본다.
    what = "품목과 가격 종류를" if len(filled) > 1 else (
        "품목을" if filled == ["품목"] else "가격 종류를"
    )
    return f"{what} 말씀하지 않으셔서 {'의 '.join(said)}를 전부 보여드립니다."


def _all_usable(blocks: list[dict[str, Any]]) -> bool | None:
    """**답에 든 조합을 전부 써도 되나.** 하나라도 막혔으면 `False` 다.

    🔴 **첫 조합만 보던 것을 고쳤다** (2026-09-16 · 사용자 결정). 「가격 알려줘」에
      아홉 조합이 나가는데, 그중 양파 중도매가는 막힌 조합이다. 그런데 이 칸은
      **첫 조합(배추 경락가)** 만 보고 `True` 를 내보냈다 — 답 문장에서 «쓰지 마세요»
      경고를 뺀 뒤로는 그 사실이 **어디에도 안 남았다.**

    ```text
    하나라도 False   -> False    (막힌 것이 섞여 있다)
    전부 True        -> True
    그 밖            -> None     (모른다 — 조합이 하나뿐일 때 예전과 같다)
    ```

    ★ **답 문장은 한 글자도 안 바뀐다.** 바뀌는 것은 이 칸 하나의 뜻이고,
      판단은 그 값을 받는 마스터가 한다.
    """
    values = [(b.get("usability") or {}).get("use_recommended") for b in blocks]
    if any(value is False for value in values):
        return False
    if values and all(value is True for value in values):
        return True
    return None


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
    filled = state.get("filled_defaults") or []
    if filled:
        #   ★ **무엇을 기본값으로 채웠는지 한 줄로 밝힌다** (2026-09-16 · 사용자 지시).
        #     되묻는 대신 전부 보여주기로 했으니, 사람이 «왜 아홉 개가 나오지» 를
        #     묻지 않게 그 자리에서 말한다. 군더더기는 이 한 줄뿐이다.
        head = [_filled_line(filled, blocks), ""]
    elif many:
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
            + f" {ASK_DATE_HINT}"
        )
    if state.get("out_of_range"):
        last = base_dt + timedelta(days=QA_MAX_OFFSET)
        days = " · ".join(str(d) for d in state["out_of_range"])
        tail.append(
            #   ★ 오늘 값도 답하므로 시작은 **오늘**이다 (범위 밖만 물었을 때 안내와 맞춘다).
            f"> ⚠ {days} 은 예측 범위 밖입니다. "
            f"{state['request'].as_of or base_dt} 부터 {last} 까지 답할 수 있습니다."
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
        is_gated=[bool(r.get("is_gated")) for r in (first.get("rows") or [])],
        band_method=(all_rows[0].get("band_method") if all_rows
                     else (todays[0] if todays else {}).get("band_method")),
        use_recommended=_all_usable(blocks),
        #   ★ 문장에서 뺀 값들 — 여기로 옮겼다. 없앤 것이 아니다.
        current_price=_first_of(first, "current_price"),
        accuracy_pct=(first.get("accuracy") or {}).get("pct"),
        accuracy_note=qa_tools.SEALED_SOURCE if first.get("accuracy") else None,
        market_name=_first_of(first, "market_name"),
        grade_name=_first_of(first, "grade_name"),
        spec_desc=_first_of(first, "spec_desc"),
    )
    #   ★ 무엇을 무시했는지는 **compose 가** 답 맨 앞에 적는다 (2026-09-16).
    #     갈래가 여럿이면 그 줄은 가격 표 앞이 아니라 **답 전체 앞**에 와야 한다.
    return {
        "markdown": "\n".join(head + body + tail).rstrip(),
        "meta": meta,
        "status": status,
    }


def build_graph():
    """노드 여섯 · 분기 하나. **체크포인트 없이** 맨몸으로 컴파일한다.

    ★ 배치·성능 노드는 **줄 세워 둔다.** 자기 갈래가 아니면 아무것도 안 하고 지나간다
      (`return {}`). 갈래마다 가지를 치면 조합이 여덟 갈래가 되는데, 그렇게 얻는 것이
      «안 물어본 노드를 안 지나간다» 뿐이라 값을 못 한다.
    """
    graph = StateGraph(QaState)
    graph.add_node("supervise", supervise)
    graph.add_node("gate", gate)
    graph.add_node("fetch", fetch)
    graph.add_node("batch", batch_node)
    graph.add_node("perf", perf_node)
    graph.add_node("compose", compose)

    graph.add_edge(START, "supervise")
    graph.add_edge("supervise", "gate")
    graph.add_conditional_edges("gate", after_gate, ["fetch", "batch"])
    graph.add_edge("fetch", "batch")
    graph.add_edge("batch", "perf")
    graph.add_edge("perf", "compose")
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
        batch_for_evidence=_batch_evidence(final),
        performance_for_evidence=_performance_evidence(final),
        models_for_payload=_models_payload(final),
    )


def _models_payload(final: dict[str, Any]) -> list[dict[str, Any]]:
    """«현재 모델» 을 기계용 칸 모양으로. **날짜는 글자로** 낸다 (JSON 으로 나간다).

    ★ 못 읽었으면 **빈 목록**이다. 어댑터가 그때 칸을 아예 안 만든다 —
      «안 읽었다» 와 «비어 있다» 를 같은 모양으로 내보내지 않는다.
    """
    if "perf" not in (final.get("routes") or []):
        return []
    return [
        {
            "kind": row.get("kind"),
            "model_ver": row.get("model_ver"),
            "created_at": _day(row.get("created_at")) if row.get("created_at") else None,
            "train_end": _day(row.get("train_end")) if row.get("train_end") else None,
            "last_swapped_at": (
                _minute(row.get("last_swapped_at")) if row.get("last_swapped_at") else None
            ),
            #   ★ 시각을 아는가. `None` 은 «칸이 아직 없다» 지 «모른다» 가 아니다.
            "last_swap_time_known": row.get("last_swap_time_known"),
        }
        for row in final.get("models") or []
    ]


def _batch_evidence(final: dict[str, Any]) -> dict[str, Any] | None:
    """배치 갈래가 읽은 것을 어댑터가 쓸 모양으로. **안 물었으면 `None`.**

    🔴 «못 읽었다» 와 «없다» 를 `read` 칸으로 갈라 둔다. 하나로 뭉치면 마스터
      이력에서 창고가 죽은 날과 배치가 안 돈 날이 같아 보인다.
    """
    if "batch" not in (final.get("routes") or []):
        return None
    row = final.get("batch_row") or {}
    report = final.get("check_row") or {}
    return {
        "on": final.get("batch_on"),
        "read": final.get("batch_read"),
        "run_id": row.get("run_id"),
        "status": qa_tools.BATCH_STATUS_LABEL.get(
            str(row.get("status")), str(row.get("status"))
        ) if row else None,
        "n_ok": row.get("n_ok"),
        "n_fail": row.get("n_fail"),
        "report_read": final.get("check_read"),
        "report_ran_at": _stamp(report.get("ran_at")) if report else None,
    }


def _performance_evidence(final: dict[str, Any]) -> list[dict[str, Any]]:
    """봉인 개봉 아홉 칸. **조건(`SEALED_SOURCE`)은 어댑터가 근거에 적는다.**"""
    if "perf" not in (final.get("routes") or []):
        return []
    return [
        {
            "item": item,
            "kind": kind,
            "avg_price": cell["avg"],
            "avg_error": cell["err"],
            #   ★ 오차율만 숫자로 낸다 — 근거를 붙일 수 있는 칸이 이것뿐이다
            #     (`Evidence.value` 는 수치다).
            "pct": float(cell["pct"]),
        }
        for (kind, item), cell in qa_tools.SEALED_ACCURACY.items()
    ]
