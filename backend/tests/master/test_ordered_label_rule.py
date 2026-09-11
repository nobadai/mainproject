"""매입 규칙이 **라벨 순서를 갖는다** — `FIRST_OFFERED` (2026-09-11).

★★ **왜 순서가 생겼나 — 실측.** 매입 `#584`(보유 차감)가 `dev` 에 들어오면서 같은
  열흘·같은 규칙·같은 출발점에서 이렇게 됐다.

```text
            기본안    보수안
차감 없음     15       15
차감 있음     24      **3**     ← 보수안 80% 가 사라졌다
승인어휘    LABEL_NOT_OFFERED **51**
```

실행 규칙이 `scenario_label` 하나라 **51번 고를 것이 없었다.**

🟢 **걷기가 대체하지 않은 것은 맞다.** `backfill._approve_one` 의 *"없으면 다른
  안으로 대체하지 않는다"* 가 일했다.

🔴 **그래서 조용한 대체가 아니라 「순서를 밝힌 한 규칙」으로 만든다.** 사람이 순서를
  정해 **설정 파일에 적고**, 코드는 그것을 읽기만 한다.

🔴 **이 파일이 잡으려는 다섯.**

```text
① 앞선 라벨이 있으면 그것이 선다
② 앞선 라벨이 없고 뒤엣것이 있으면 뒤엣것이 선다
③ 하나도 없으면 LABEL_NOT_OFFERED 이고 사유에 **찾은 순서**가 있다
④ 배열을 뒤집으면 고르는 것도 뒤집힌다 (순서가 설정의 것)
⑤ 요약의 라벨 어휘가 접히지 않고 올라온다 · 안 돈 날은 안 센다
```

★ **DB 를 안 탄다.** 규칙 · 실행 이력 · 기존 결정 · 승인 문을 전부 대역으로 준다.
  이 파일은 **한 행도 안 쓴다.**

⚠️ **라벨 이름은 대역이 적는다.** 백필 코드에는 없어야 하고
  (`test_코드에_시나리오_안_이름이_박혀_있지_않다`), 그 규율은 이 판에서도 그대로다.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.master.backfill import (
    ALWAYS_BASE,
    FIRST_OFFERED,
    BackfilledRun,
    BackfillOut,
    BackfillRuleMissing,
    OrderedLabelRule,
    backfill_decisions,
    read_rules,
)
from app.master.backtest_runner import WalkResult, format_summary
from app.master.decision import DecisionIn, DecisionOut
from app.master.scheduler import DayRunOutcome

#: 🔴 **라벨 이름의 주인은 매입이다.** 대역이 적고 코드는 모른다.
보수 = "보수"
기본 = "기본"
공격 = "공격"

하루 = date(2026, 9, 7)
이틀 = date(2026, 9, 8)


def _NFC(text: str) -> str:
    """한글 비교 전 정규화. **자모 분리형과 완성형이 섞이면 `in` 이 거짓말한다.**"""
    return unicodedata.normalize("NFC", text)


def _순서규칙(*labels: str) -> dict[str, Any]:
    return {"backfill": {"procurement": {"rule": FIRST_OFFERED, "scenario_labels": list(labels)}}}


def 실행행(as_of: date, *labels: str) -> dict[str, Any]:
    """`master_agent_runs` 매입 한 행의 대역. **그날 제시된 안이 인자 그대로다.**"""
    return {
        "run_id": uuid4(),
        "request_id": f"REQ-{as_of.isoformat()}",
        "as_of": as_of,
        "cycle": "PROCUREMENT",
        "end_code": "E1_APPROVED",
        "response_payload": {
            "end_code": "E1_APPROVED",
            "scenarios": [{"label": label} for label in labels],
        },
    }


class _승인문:
    """`record_decision` 대역. **무엇을 골라 왔는지만 남긴다.**"""

    def __init__(self) -> None:
        self.calls: list[DecisionIn] = []

    def __call__(self, request_id: str, payload: DecisionIn) -> DecisionOut:
        self.calls.append(payload)
        return DecisionOut(
            decision_id=uuid4(),
            request_id=request_id,
            decision_seq=1,
            decision=payload.decision,
            scenario_label=payload.scenario_label,
            decided_by=payload.decided_by,
            end_code_at_decision="E1_APPROVED",
            history_run_id=payload.history_run_id,
            revalidation_outcome="PASSED",
            note=payload.note,
            created_at=datetime(2026, 9, 9, 10, 0, tzinfo=UTC),
        )


def _돌린다(행들: dict[date, list[dict[str, Any]]], 설정: dict[str, Any]):
    """**진짜 `backfill_decisions`** 에 DB 대신 대역만 물린 것.

    ★★ **판단을 흉내 내지 않는다.** 순서가 실제로 도는지를 재려면 규칙 읽기 · 제시된
      안 대조 · 승인 문 호출을 **진짜 코드가** 해야 한다.
    """
    문 = _승인문()
    결과 = backfill_decisions(
        sim_run_id="SIM-ORDERED",
        start=min(행들),
        end=max(행들),
        load_config=lambda _: 설정,
        runs_on=lambda *, sim_run_id, as_of, limit: 행들.get(as_of, []),
        decisions_of=lambda request_id: [],
        decide=문,
    )
    return 결과, 문


# ---------------------------------------------------------------------------
# 규칙 읽기 — 순서의 주인이 설정이다
# ---------------------------------------------------------------------------


def test_순서를_설정이_말한다() -> None:
    """🟢 **자기 생존 검사.** 적은 그대로 읽혀야 아래 검사들이 뜻을 갖는다."""
    읽은것 = read_rules(_순서규칙(보수, 기본)).procurement

    assert 읽은것 == OrderedLabelRule(name=FIRST_OFFERED, scenario_labels=(보수, 기본))
    assert 읽은것.labels_in_order == (보수, 기본)


def test_옛_규칙은_그대로_산다() -> None:
    """🔴 **`ALWAYS_BASE` 를 갈아끼우지 않았다.**

    한 라벨만 고르는 규칙은 여전히 옳고, **그것으로 돈 실행들이 이미 있다.**
    새 규칙은 더하는 것이다.
    """
    옛설정 = {"backfill": {"procurement": {"rule": ALWAYS_BASE, "scenario_label": 기본}}}
    읽은것 = read_rules(옛설정).procurement

    assert 읽은것 is not None
    assert 읽은것.name == ALWAYS_BASE
    assert 읽은것.labels_in_order == (기본,), "한 라벨 규칙도 같은 눈으로 읽혀야 한다"


@pytest.mark.parametrize(
    "labels",
    [[], ["  "], [기본, ""], 보수, {"a": 1}, [기본, 3]],
    ids=["빈배열", "공백", "빈칸섞임", "문자열", "객체", "숫자섞임"],
)
def test_고를_것이_비면_규칙이_선_것이_아니다(labels: Any) -> None:
    """🔴 **빈 배열은 *"아무 라벨이나"* 가 아니라 규칙이 안 선 것이다.**

    ⚠️ **문자열 하나를 배열로 안 접는다.** `"보수"` 를 적으면 파이썬이 글자 하나씩
      도는 순회 가능 객체라 `("보", "수")` 가 되고, 있지도 않은 라벨 둘을 찾다가
      `LABEL_NOT_OFFERED` 로 끝난다 — 터지는 편이 낫다.
    """
    with pytest.raises(BackfillRuleMissing):
        read_rules(
            {"backfill": {"procurement": {"rule": FIRST_OFFERED, "scenario_labels": labels}}}
        )


def test_모르는_규칙_이름은_안다고_안_한다() -> None:
    """★ 사유가 **아는 이름 전부**를 말한다 — 둘이 됐으니 둘 다 말해야 한다."""
    with pytest.raises(BackfillRuleMissing) as 터진것:
        read_rules({"backfill": {"procurement": {"rule": "ALWAYS_CONSERVATIVE"}}})

    사유 = str(터진것.value)
    assert ALWAYS_BASE in 사유 and FIRST_OFFERED in 사유, f"아는 규칙을 다 안 말한다: {사유}"


def test_이름으로_가르지_모양으로_짐작하지_않는다() -> None:
    """🔴 **이름은 옛것인데 모양은 새것인 파일이 조용히 돌면 안 된다.**

    `scenario_labels` 가 있다고 새 규칙으로 읽으면, **실행 이력에 적히는 규칙 이름이
    실제로 돈 규칙과 갈린다.**
    """
    섞인것 = {
        "backfill": {
            "procurement": {"rule": ALWAYS_BASE, "scenario_labels": [보수, 기본]},
        }
    }

    with pytest.raises(BackfillRuleMissing):
        read_rules(섞인것)


# ---------------------------------------------------------------------------
# ①② 순서대로 찾아 먼저 있는 것
# ---------------------------------------------------------------------------


def test_앞선_라벨이_있으면_그것이_선다() -> None:
    """★ 뒤엣것이 그날 같이 있어도 **앞엣것**이다."""
    결과, 문 = _돌린다({하루: [실행행(하루, 보수, 기본)]}, _순서규칙(보수, 기본))

    assert [one.outcome for one in 결과.runs] == ["RECORDED"]
    assert [one.scenario_label for one in 문.calls] == [보수]


def test_앞선_라벨이_없으면_다음_것이_선다() -> None:
    """🔴 **이것이 `LABEL_NOT_OFFERED` 51건을 없애는 자리다.**

    ⚠️ 그래도 **대체가 아니다.** 사람이 적어 둔 순서를 따르는 것이고, 안 적힌
      라벨(`공격`)은 그날 있어도 안 고른다 — 바로 아래 검사가 그것을 잠근다.
    """
    결과, 문 = _돌린다({하루: [실행행(하루, 기본, 공격)]}, _순서규칙(보수, 기본))

    assert [one.outcome for one in 결과.runs] == ["RECORDED"]
    assert [one.scenario_label for one in 문.calls] == [기본]


def test_안_적힌_라벨은_그날_있어도_안_고른다() -> None:
    """🔴 **순서는 「찾아볼 곳」을 늘릴 뿐 아무거나 고르라는 뜻이 아니다.**

    ★ 그날 안이 하나뿐이어도 그것을 고르지 않는다 — *"하나밖에 없으니 그것"* 은
      **어느 안이 나은지 판단하는 코드**이고, 팀이 금지한 정책 자동 선택이다.
    """
    결과, 문 = _돌린다({하루: [실행행(하루, 공격)]}, _순서규칙(보수, 기본))

    assert [one.outcome for one in 결과.runs] == ["LABEL_NOT_OFFERED"]
    assert 문.calls == [], "안 적힌 라벨을 골랐다"


# ---------------------------------------------------------------------------
# ③ 하나도 없으면 LABEL_NOT_OFFERED · 사유에 찾은 순서가 있다
# ---------------------------------------------------------------------------


def test_하나도_없으면_사유가_찾은_순서를_말한다() -> None:
    """★ **「하나를 못 찾았다」와 「둘 다 못 찾았다」는 다른 사실이다.**

    규칙을 늘렸는데도 안 섰다는 것이 이 줄로 보여야, 다음에 무엇을 더할지가 정해진다.
    """
    결과, 문 = _돌린다({하루: [실행행(하루, 공격)]}, _순서규칙(보수, 기본))

    사유 = _NFC(결과.runs[0].reason or "")

    assert _NFC("찾은 순서") in 사유, f"찾은 순서가 사유에 없다: {사유}"
    assert _NFC(f"{보수} → {기본}") in 사유, f"찾은 순서가 적힌 그대로가 아니다: {사유}"
    assert _NFC(공격) in 사유, "그날 제시된 안이 사유에 없다"
    assert 문.calls == []


def test_옛_규칙도_찾은_순서를_말한다() -> None:
    """★ 한 라벨 규칙도 「찾아본 것」이 하나 있다 — 그 하나를 적는다.

    🔴 두 규칙의 사유 모양이 갈리면 읽는 쪽이 어느 규칙이었는지를 먼저 알아야 한다.
    """
    옛설정 = {"backfill": {"procurement": {"rule": ALWAYS_BASE, "scenario_label": 보수}}}
    결과 = _돌린다({하루: [실행행(하루, 공격)]}, 옛설정)[0]

    사유 = _NFC(결과.runs[0].reason or "")

    assert _NFC(f"찾은 순서: {보수} ·") in 사유, f"한 라벨 규칙의 사유가 다르다: {사유}"


# ---------------------------------------------------------------------------
# ④ 🔴 순서의 주인이 설정이다 — 배열을 뒤집으면 고르는 것도 뒤집힌다
# ---------------------------------------------------------------------------


def test_배열을_뒤집으면_고르는_것도_뒤집힌다() -> None:
    """🔴 **이것이 이 판의 핵심 잠금이다.**

    ★★ 코드가 순서를 알면 **규칙의 주인이 설정이 아니게 되고**, 그 순간 «어느 안을
      우선하나» 가 diff 가 아니라 배포로 바뀐다.

    ★ **같은 날 같은 안 둘**에 규칙만 뒤집는다 — 갈리는 이유가 배열임을 못 박는다.
    """
    그날 = {하루: [실행행(하루, 보수, 기본)]}

    문가 = _돌린다(그날, _순서규칙(보수, 기본))[1]
    문나 = _돌린다(그날, _순서규칙(기본, 보수))[1]

    assert [one.scenario_label for one in 문가.calls] == [보수]
    assert [one.scenario_label for one in 문나.calls] == [기본], (
        "배열을 뒤집었는데 같은 것을 골랐다 — 순서가 코드에 박혀 있다"
    )


def test_셋을_적어도_순서대로_간다() -> None:
    """★ 둘로만 재면 *"첫째 아니면 둘째"* 라는 특수한 모양에 맞춘 코드도 통과한다."""
    결과, 문 = _돌린다({하루: [실행행(하루, 공격)]}, _순서규칙(보수, 기본, 공격))

    assert [one.outcome for one in 결과.runs] == ["RECORDED"]
    assert [one.scenario_label for one in 문.calls] == [공격]


# ---------------------------------------------------------------------------
# ⑤ 어느 라벨이 실제로 섰는지 요약이 센다
# ---------------------------------------------------------------------------


def test_고른_라벨이_행에_남는다() -> None:
    """🔴 **규칙 파일이 적은 순서와 그날 선 라벨은 다른 사실이다.**

    ★★ 순서가 생긴 순간 *"그날 어느 라벨이 돌았나"* 가 규칙 파일만 보고는 안 풀린다.

    ⚠️ `master_decisions.scenario_label` 에도 같은 값이 적힌다 (승인 문이
      `DecisionIn.scenario_label` 로 받아 저장소에 넘긴다) — **그쪽이 장부의 정본**
      이고 이 칸은 걷기 한 번을 요약하려고 든 것이다.
    """
    결과, 문 = _돌린다(
        {하루: [실행행(하루, 보수, 기본)], 이틀: [실행행(이틀, 기본)]},
        _순서규칙(보수, 기본),
    )

    assert [one.picked_label for one in 결과.runs] == [보수, 기본]
    assert [one.scenario_label for one in 문.calls] == [보수, 기본], (
        "행에 남은 라벨과 승인 문에 간 라벨이 다르다 — 같은 사실이 두 곳에서 갈렸다"
    )
    assert dict(결과.label_outcomes) == {보수: 1, 기본: 1}


def test_못_고른_행은_라벨을_안_센다() -> None:
    """★ 못 고른 행에는 라벨이라는 사실 자체가 없다.

    🔴 0 으로도 세면 *"아무것도 안 골랐다"* 가 그 수만큼 부풀어 오른다.
    """
    결과 = _돌린다({하루: [실행행(하루, 공격)]}, _순서규칙(보수, 기본))[0]

    assert 결과.runs[0].picked_label is None
    assert dict(결과.label_outcomes) == {}


def test_승인_어휘와_한_칸에_안_담긴다() -> None:
    """🔴 *"승인을 적었나"* 와 *"무엇을 골랐나"* 는 축이 다르다."""
    결과 = _돌린다(
        {하루: [실행행(하루, 보수)], 이틀: [실행행(이틀, 보수)]},
        _순서규칙(보수, 기본),
    )[0]

    assert dict(결과.label_outcomes) == {보수: 2}
    assert dict(결과.outcomes) == {"RECORDED": 2}, "승인 줄에 라벨이 섞였다"


def _하루(승인: BackfillOut | None) -> DayRunOutcome:
    return DayRunOutcome(as_of=하루, action="RUN_NOW", reason="", procurement_approval=승인)


def _백필(*labels: str) -> BackfillOut:
    return BackfillOut(
        sim_run_id="SIM-ORDERED",
        start=하루,
        end=하루,
        status="RAN",
        runs=tuple(
            BackfilledRun(
                as_of=하루,
                run_id=f"RUN-{i}",
                request_id=f"REQ-{i}",
                outcome="RECORDED",
                picked_label=label,
            )
            for i, label in enumerate(labels)
        ),
    )


def test_라벨_어휘가_요약에_접히지_않고_올라온다() -> None:
    """⚠️ **화면에 안 나오는 어휘는 없는 어휘와 같다.**

    ★★ 사람이 이 선택지를 고를 때 *"곡선이 한 규칙의 것이 아니게 된다"* 가 위험으로
      지적됐고, **이 줄이 그 지적에 대한 답**이다 — 날마다 결정 행을 되짚지 않아도
      어느 라벨이 몇 번 섰는지가 요약 한 줄에 있다.
    """
    결과 = WalkResult(start=하루, end=하루, days=(_하루(_백필(보수, 보수, 기본)),))

    센것 = dict(결과.label_outcomes)
    요약 = _NFC(format_summary(결과))

    assert 센것 == {보수: 2, 기본: 1}
    for 라벨 in (보수, 기본):
        assert _NFC(라벨) in 요약, f"요약에 라벨 '{라벨}' 이 없다"
    assert _NFC("라벨어휘  ") in 요약, "요약에 라벨 줄 자체가 없다"


def test_승인을_안_탄_날은_요약이_세지_않는다() -> None:
    """★ 안 탄 날은 `{}` 다 — 0 으로 채우면 *"고른 것이 없다"* 와 같아 보인다."""
    결과 = WalkResult(start=하루, end=하루, days=(_하루(None),))

    assert dict(결과.label_outcomes) == {}
    assert _NFC("라벨어휘  {}") in _NFC(format_summary(결과))


def test_라벨_줄을_승인_줄과_한_칸에_담지_않는다() -> None:
    """🔴 어휘가 다르고 축이 다르다 — 묶으면 어느 쪽 수가 는 것인지를 못 읽는다."""
    결과 = WalkResult(start=하루, end=하루, days=(_하루(_백필(보수)),))

    assert dict(결과.label_outcomes) == {보수: 1}
    assert dict(결과.approval_outcomes) == {"RECORDED": 1}
    assert 보수 not in dict(결과.approval_outcomes), "승인 줄에 라벨이 섞였다"
