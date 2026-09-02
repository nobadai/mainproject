"""출력 문면에 내부 용어가 새지 않는지 — **사람이 읽는 자리만** 본다.

#164 로 매입 근거가 H1 화면까지 나가기 시작했다 (`EvidencePanel`). 그 전에는
`rationale`·`risks` 를 Critic 만 읽었는데, 이제 사람도 읽는다. 필드명·설정 키 경로·
문서 절 번호가 섞이면 **"왜 이 수량인가" 가 안 읽힌다.**

🔴 **규칙 8을 어떻게 피했나.**
  금지어 목록을 코드(``app/``)에 두고 그 목록으로 검사하면, 같은 상수를 두 곳에서
  보는 것이라 "검사한다"를 증명하지 못한다 — 목록을 고치면 검사도 같이 물러난다.
  그래서 목록은 **이 파일에만** 산다. ``app/`` 어디에도 이 낱말들이 상수로 없고,
  검사는 **실제로 생성된 문장**을 훑는다. 변이(문면에 내부어를 되돌림)를 넣으면
  이 검사가 운다 — 그 확인은 아래 ``test_the_banlist_actually_bites`` 가 한다.

⚠️ ``claim`` 은 검사 대상이 아니다. 경로 표기(``scenarios[0].total_qty_kg``)가
  **계약 값**이고 마스터 ``canonical_claim`` 이 그 표기로 대조한다 — 화면 라벨 문제는
  프론트 쪽에서 따로 푼다.
"""

from datetime import date

import pytest

from app.purchase_agent.graph import run_purchase_agent

#: 사람이 읽는 문장에 나오면 안 되는 낱말. **뜻이 아니라 표기**를 막는다.
#:
#: 왜 각각 막는가:
#:   필드명·설정 키   — 코드를 안 보면 무슨 값인지 알 수 없다
#:   문서 절 번호     — 화면 독자에게 참조할 문서가 없다
#:   내부 열거값      — `stable` · `SIM_FIXED` 는 우리 어휘지 사람 말이 아니다
#:   규칙 번호        — CLAUDE.md 를 읽어야만 뜻이 통한다
BANNED = (
    # 필드명 · 설정 키 경로
    "inbound_lead_days",
    "purchase_payment_days",
    "expected_arrival_date",
    "payment_date",
    "medium_grade_factor",
    "grade_unit_price",
    "sourcing_plan[]",
    "by_label",
    "constraints.",
    "contract_price",
    "cap_by_date",
    "operational_limit_days",
    # 내부 열거값 · 축 이름
    "SIM_FIXED",
    "ASSUMED",
    "rule_only",
    # 통계·내부 약어
    "q90",
    # 문서 절 번호 · 규칙 번호
    "상세설계 §",
    "IO명세 §",
    "규칙 3",
    "규칙 4",
    "규칙 5",
    "규칙 7",
)

#: 앵커 5개 × 4품목. 문면은 날짜·품목마다 갈리므로 전부 훑는다.
ANCHORS = ("2025-12-31", "2026-08-21", "2026-08-28", "2026-09-04", "2026-09-11")
ITEMS = ("배추", "무", "양파", "피마늘")


def _human_strings(as_of: str, item: str) -> list[tuple[str, str]]:
    """사람이 읽는 문장 전부 — ``(자리, 문장)``."""
    state = run_purchase_agent(item, date.fromisoformat(as_of))
    out: list[tuple[str, str]] = []
    for scenario in state.get("scenarios") or []:
        where = f"{as_of}/{item}/{scenario.get('label')}"
        for risk in scenario.get("risks") or []:
            out.append((f"{where}/risks", risk))
        for row in scenario.get("rationale") or []:
            # ``claim`` 은 제외 — 계약 표기다 (모듈 docstring 참조).
            for key in ("evidence_detail",):
                if row.get(key):
                    out.append((f"{where}/rationale.{key}", row[key]))
    for reason in state.get("rejected_reasons") or []:
        out.append((f"{as_of}/{item}/rejected", reason.get("reason", "")))
    return out


@pytest.mark.parametrize("as_of", ANCHORS)
def test_output_wording_carries_no_internal_terms(as_of: str) -> None:
    """risks · rationale 문면에 내부 용어가 없다."""
    offenders: list[str] = []
    for item in ITEMS:
        for where, text in _human_strings(as_of, item):
            for word in BANNED:
                if word in text:
                    offenders.append(f"{where}: {word!r} in {text[:90]!r}")
    assert not offenders, "내부 용어가 사람이 읽는 문장에 샜다:\n" + "\n".join(offenders)


def test_the_banlist_actually_bites() -> None:
    """🔴 **목록이 실제로 무는지** — 값 비교가 아니라 동작으로 본다 (규칙 8).

    금지어가 든 문장을 넣어 검사가 우는지 본다. 이게 없으면 위 테스트가 통과하는 이유가
    *"문면이 깨끗해서"* 인지 *"검사가 아무것도 안 봐서"* 인지 구분되지 않는다.
    """
    clean = "물류 입고 소요일이 미확정이라 회차별 도착일을 계산하지 않는다"
    dirty = "inbound_lead_days(N4) 미확정이라 expected_arrival_date를 계산하지 않는다"
    assert not [w for w in BANNED if w in clean]
    assert [w for w in BANNED if w in dirty], "금지어가 든 문장을 못 잡으면 목록이 죽은 것이다"


def test_every_anchor_produced_something_to_check() -> None:
    """**빈 목록을 훑고 통과하지 않는다.**

    ``_human_strings`` 가 0건을 돌려주면 위 테스트는 자동으로 통과한다 — 그건 검사가
    아니라 침묵이다. 앵커마다 문장이 실제로 나오는지 먼저 못 박는다.
    """
    counts = {a: sum(len(_human_strings(a, i)) for i in ITEMS) for a in ANCHORS}
    assert all(n > 0 for n in counts.values()), f"훑을 문장이 없는 앵커가 있다: {counts}"
