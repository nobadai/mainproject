"""**자동 백필 승인 — 규칙이 가리키는 안만, 경계 안에서만.**

🔴 **DB 를 안 탄다.** 규칙 · 실행 이력 · 기존 결정 · 승인 문을 전부 대역으로 준다.
   이 판은 **한 행도 안 쓴다** — 그래서 승인 문 대역은 부른 횟수를 세기만 하고,
   가드가 걸린 자리에서는 그 목록이 비어 있어야 한다.

⚠️ **실제로 채우지 않는다.** 이 판은 배선을 세우는 것까지다. 179 영업일을 실제로
  채우는 것은 다음 판이고, 여기서 흉내 내면 대역이 낸 숫자가 장부처럼 보인다.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.master import backfill
from app.master.backfill import (
    ALWAYS_BASE,
    AUTO_BACKFILL,
    BACKFILL_BOUNDARY_AS_OF,
    BackfillRule,
    BackfillRuleMissing,
    backfill_decisions,
    read_rule,
)
from app.master.decision import DecisionIn, DecisionOut, DecisionRejected

기본 = "기본"
공격 = "공격"

규칙설정 = {"backfill": {"rule": ALWAYS_BASE, "scenario_label": 기본}}


# ── 대역 ────────────────────────────────────────────────────────────────


def 실행행(
    as_of: date,
    *,
    end_code: str | None = "E1_APPROVED",
    labels: tuple[str, ...] = (기본, 공격),
    cycle: str = "PROCUREMENT",
    request_id: str | None = "REQ-1",
    run_id: UUID | None = None,
) -> dict[str, object]:
    """`master_agent_runs` 한 행의 대역. **읽는 칸만 채운다.**"""
    return {
        "run_id": run_id or uuid4(),
        "request_id": request_id,
        "as_of": as_of,
        "cycle": cycle,
        "end_code": end_code,
        "response_payload": {
            "end_code": end_code,
            "scenarios": [{"label": label} for label in labels],
            "candidates": [{"scenario": {"scenario_id": label}} for label in labels],
        },
    }


class _승인문:
    """`record_decision` 대역. **부른 것을 그대로 모아 둔다.**

    🔴 **아무 데도 안 쓴다.** 이 검사가 재는 것은 *"불렀나 안 불렀나"* 이지 적재가
      아니다 — 적재를 흉내 내면 이 판이 *"한 행도 안 쓴다"* 를 못 지킨다.
    """

    def __init__(self, *, outcome: str | None = "PASSED", raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, DecisionIn]] = []
        self._outcome = outcome
        self._raises = raises

    def __call__(self, request_id: str, payload: DecisionIn) -> DecisionOut:
        self.calls.append((request_id, payload))
        if self._raises is not None:
            raise self._raises
        return DecisionOut(
            decision_id=uuid4(),
            request_id=request_id,
            decision_seq=1,
            decision=payload.decision,
            scenario_label=payload.scenario_label,
            decided_by=payload.decided_by,
            end_code_at_decision="E1_APPROVED",
            history_run_id=payload.history_run_id,
            revalidation_outcome=self._outcome,  # type: ignore[arg-type]
            note=payload.note,
            created_at=datetime(2026, 9, 9, 10, 0, tzinfo=UTC),
        )


def 하루조회(행들: dict[date, list[dict[str, object]]]):
    """`list_runs` 대역. **그날 행만 돌려준다.**"""

    def _조회(*, sim_run_id: str, as_of: date, limit: int) -> list[dict[str, object]]:
        return 행들.get(as_of, [])

    return _조회


def 결정없음(request_id: str) -> list[DecisionOut]:
    return []


def 백필(
    행들: dict[date, list[dict[str, object]]],
    문: _승인문,
    *,
    start: date,
    end: date,
    설정: dict[str, object] | None = None,
    기존결정=결정없음,
):
    return backfill_decisions(
        sim_run_id="SIM-BACKFILL",
        start=start,
        end=end,
        load_config=lambda _: 규칙설정 if 설정 is None else 설정,
        runs_on=하루조회(행들),
        decisions_of=기존결정,
        decide=문,
    )


# ── 경계 ────────────────────────────────────────────────────────────────


def test_경계_밖_실행은_한_행도_안_쓰고_BLOCKED_BY_BOUNDARY_로_남는다() -> None:
    """🔴 **`as_of >= 2026-09-10` 은 사람만이다.**

    ⚠️ 값이 남는 것으로는 부족하다 — **승인 문을 한 번도 안 불렀는지**까지 잰다.
      결과에는 막혔다고 적으면서 문은 부르는 경우가 실제 사고의 모양이다.
    """
    넘은날 = date(2026, 9, 10)
    문 = _승인문()

    결과 = 백필({넘은날: [실행행(넘은날)]}, 문, start=넘은날, end=넘은날)

    assert [one.outcome for one in 결과.runs] == ["BLOCKED_BY_BOUNDARY"]
    assert 문.calls == [], "경계 밖인데 승인 문을 불렀다"


def test_경계_당일은_자동으로_채운다() -> None:
    """🟢 **자기 생존 검사.** 경계가 `<=` 라 2026-09-09 은 안쪽이다.

    ★ 이것이 없으면 *"경계 밖은 안 쓴다"* 는 **아무 날도 안 쓰는 코드**로도 통과한다.
    """
    문 = _승인문()

    결과 = 백필(
        {BACKFILL_BOUNDARY_AS_OF: [실행행(BACKFILL_BOUNDARY_AS_OF)]},
        문,
        start=BACKFILL_BOUNDARY_AS_OF,
        end=BACKFILL_BOUNDARY_AS_OF,
    )

    assert [one.outcome for one in 결과.runs] == ["RECORDED"]
    assert len(문.calls) == 1


def test_경계를_넘는_범위는_조용히_안_잘리고_넘은_날이_결과에_남는다() -> None:
    """⚠️ **조용히 자르면 부르는 쪽이 *"다 채웠다"* 고 믿는다.**

    ★ 그날에 실행 행이 하나도 없어도 `blocked_days` 에는 남는다 — 못 채운 날은
      행의 유무와 무관한 사실이다.
    """
    문 = _승인문()

    결과 = 백필({}, 문, start=date(2026, 9, 8), end=date(2026, 9, 12))

    assert 결과.blocked_days == (date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12))
    assert 문.calls == []


def test_경계는_설정이_아니라_코드에_있다() -> None:
    """🔴 **가드는 `config_json` 으로 안 내린다** — 옮기려면 diff 에 보여야 한다."""
    assert BACKFILL_BOUNDARY_AS_OF == date(2026, 9, 9)

    설정 = {
        "backfill": {"rule": ALWAYS_BASE, "scenario_label": 기본, "boundary_as_of": "2999-12-31"}
    }
    넘은날 = date(2026, 9, 10)
    문 = _승인문()

    결과 = 백필({넘은날: [실행행(넘은날)]}, 문, start=넘은날, end=넘은날, 설정=설정)

    assert [one.outcome for one in 결과.runs] == ["BLOCKED_BY_BOUNDARY"]
    assert 문.calls == []


# ── 규칙 ────────────────────────────────────────────────────────────────


def test_규칙이_없으면_승인_문을_한_번도_안_부른다() -> None:
    """🔴 **기본 규칙을 지어내지 않는다.** 기본값은 곧 업무 규칙이다."""
    날 = date(2026, 9, 1)
    문 = _승인문()

    결과 = 백필({날: [실행행(날)]}, 문, start=날, end=날, 설정={})

    assert 결과.status == "NO_RULE"
    assert 결과.rule is None
    assert 결과.runs == ()
    assert 문.calls == [], "규칙이 없는데 승인 문을 불렀다"


def test_모르는_규칙_이름도_아는_규칙으로_접지_않는다() -> None:
    날 = date(2026, 9, 1)
    문 = _승인문()
    설정 = {"backfill": {"rule": "ALWAYS_WHATEVER", "scenario_label": 기본}}

    결과 = 백필({날: [실행행(날)]}, 문, start=날, end=날, 설정=설정)

    assert 결과.status == "NO_RULE"
    assert 문.calls == []


def test_고를_안이_비면_규칙이_선_것이_아니다() -> None:
    """★ 규칙 이름만 있고 라벨이 없으면 **코드가 라벨을 채울 자리**가 생긴다."""
    with pytest.raises(BackfillRuleMissing):
        read_rule({"backfill": {"rule": ALWAYS_BASE, "scenario_label": "  "}})


def test_규칙이_가리키는_라벨은_설정이_말한다() -> None:
    """🟢 **자기 생존 검사.** 설정이 다른 라벨을 말하면 그 라벨이 승인된다."""
    assert read_rule(규칙설정) == BackfillRule(name=ALWAYS_BASE, scenario_label=기본)
    다른설정 = {"backfill": {"rule": ALWAYS_BASE, "scenario_label": 공격}}
    assert read_rule(다른설정).scenario_label == 공격


def test_규칙_라벨이_그날_안에_없으면_다른_안으로_대체하지_않는다() -> None:
    """🔴 **대체하면 곡선이 규칙과 다른 것을 재현한다.**

    ⚠️ 그날 안이 하나뿐이어도 그것을 고르지 않는다 — *"하나밖에 없으니 그것"* 은
      **어느 안이 나은지 판단하는 코드**이고, 팀이 금지한 정책 자동 선택이다.
    """
    날 = date(2026, 9, 1)
    문 = _승인문()

    결과 = 백필({날: [실행행(날, labels=(공격,))]}, 문, start=날, end=날)

    assert [one.outcome for one in 결과.runs] == ["LABEL_NOT_OFFERED"]
    assert 문.calls == [], "그날 없는 안을 다른 안으로 대체해 승인했다"


# ── 승인 문 ─────────────────────────────────────────────────────────────


def test_decided_by_에_사람_이름을_안_쓴다() -> None:
    """🔴 **`master_decisions` 는 append-only 라 못 지운다.**

    사람이 안 눌렀는데 사람 이름을 적으면 **눌렀다고 기록**되고 되돌릴 수 없다.
    """
    날 = date(2026, 9, 1)
    문 = _승인문()

    백필({날: [실행행(날)]}, 문, start=날, end=날)

    (_, 본문) = 문.calls[0]
    assert 본문.decided_by == "AUTO-BACKFILL"
    assert AUTO_BACKFILL == "AUTO-BACKFILL"


def test_승인은_규칙이_가리키는_안을_그_실행_행에_건다() -> None:
    날 = date(2026, 9, 1)
    행 = 실행행(날)
    문 = _승인문()

    백필({날: [행]}, 문, start=날, end=날)

    (업무키, 본문) = 문.calls[0]
    assert 업무키 == "REQ-1"
    assert 본문.decision == "APPROVE"
    assert 본문.scenario_label == 기본
    assert 본문.history_run_id == str(행["run_id"])


def test_note_에_규칙_이름을_남긴다() -> None:
    """🟡 **되짚을 때 눈으로 보라는 것뿐이다.** 규칙 이름의 주인은 `config_json` 이다."""
    날 = date(2026, 9, 1)
    문 = _승인문()

    백필({날: [실행행(날)]}, 문, start=날, end=날)

    (_, 본문) = 문.calls[0]
    assert 본문.note is not None
    assert ALWAYS_BASE in 본문.note
    assert AUTO_BACKFILL in 본문.note


def test_재검증_결과를_접지_않고_그대로_싣는다() -> None:
    """🔴 **`RECORDED` 는 결정 행을 적었다는 뜻이지 효력이 섰다는 뜻이 아니다.**"""
    날 = date(2026, 9, 1)
    문 = _승인문(outcome="FAILED")

    결과 = 백필({날: [실행행(날)]}, 문, start=날, end=날)

    assert 결과.runs[0].outcome == "RECORDED"
    assert 결과.runs[0].revalidation_outcome == "FAILED"


def test_승인_문이_거절하면_FAILED_로_남고_나머지_날은_이어진다() -> None:
    """★ **한 날이 터져도 밖으로 예외를 안 낸다** (`walk` 과 같은 태도)."""
    첫날, 둘째날 = date(2026, 9, 1), date(2026, 9, 2)
    문 = _승인문(raises=DecisionRejected("재검증 대상이 없다"))

    결과 = 백필(
        {첫날: [실행행(첫날)], 둘째날: [실행행(둘째날, request_id="REQ-2")]},
        문,
        start=첫날,
        end=둘째날,
    )

    assert [one.outcome for one in 결과.runs] == ["FAILED", "FAILED"]
    assert 결과.runs[0].reason is not None and "DecisionRejected" in 결과.runs[0].reason
    assert len(문.calls) == 2


# ── 대상 고르기 ─────────────────────────────────────────────────────────


def test_이미_결정이_있으면_덮지_않는다() -> None:
    날 = date(2026, 9, 1)
    문 = _승인문()
    사람이_누른_것 = DecisionOut(
        decision_id=uuid4(),
        request_id="REQ-1",
        decision_seq=1,
        decision="REJECT_ALL",
        decided_by="이현서",
        end_code_at_decision="E1_APPROVED",
        created_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
    )

    결과 = 백필({날: [실행행(날)]}, 문, start=날, end=날, 기존결정=lambda _: [사람이_누른_것])

    assert [one.outcome for one in 결과.runs] == ["ALREADY_DECIDED"]
    assert 문.calls == [], "이미 결정이 있는데 자동이 덮어썼다"


@pytest.mark.parametrize("종료코드", ["E2_HELD", "E3_REJECTED", "E5_NO_FEASIBLE_PLAN", None])
def test_승인할_안이_없는_실행은_NOT_APPROVABLE_이다(종료코드: str | None) -> None:
    날 = date(2026, 9, 1)
    문 = _승인문()

    결과 = 백필({날: [실행행(날, end_code=종료코드)]}, 문, start=날, end=날)

    assert [one.outcome for one in 결과.runs] == ["NOT_APPROVABLE"]
    assert 문.calls == []


def test_판매_실행은_백필_대상이_아니다() -> None:
    """🔴 이 판이 재현하는 것은 **매입 승인 구간**이다."""
    날 = date(2026, 9, 1)
    문 = _승인문()

    결과 = 백필({날: [실행행(날, cycle="SALES", end_code="SL1_PRESENTED")]}, 문, start=날, end=날)

    assert [one.outcome for one in 결과.runs] == ["NOT_APPROVABLE"]
    assert 문.calls == []


def test_여섯_결과를_한_통에_넣지_않는다() -> None:
    """★ *"없다"* 와 *"안 했다"* 와 *"못 했다"* 가 한 걷기 안에서 갈려 보인다."""
    문 = _승인문()
    행들 = {
        date(2026, 9, 1): [실행행(date(2026, 9, 1))],
        date(2026, 9, 2): [실행행(date(2026, 9, 2), end_code="E2_HELD", request_id="REQ-2")],
        date(2026, 9, 3): [실행행(date(2026, 9, 3), labels=(공격,), request_id="REQ-3")],
        date(2026, 9, 10): [실행행(date(2026, 9, 10), request_id="REQ-4")],
    }

    결과 = 백필(행들, 문, start=date(2026, 9, 1), end=date(2026, 9, 10))

    assert dict(결과.outcomes) == {
        "RECORDED": 1,
        "NOT_APPROVABLE": 1,
        "LABEL_NOT_OFFERED": 1,
        "BLOCKED_BY_BOUNDARY": 1,
    }


def test_범위가_거꾸로면_막고_사유를_낸다() -> None:
    with pytest.raises(ValueError, match="거꾸로"):
        백필({}, _승인문(), start=date(2026, 9, 5), end=date(2026, 9, 1))


# ── 원문을 읽어 잠근다 ──────────────────────────────────────────────────


def _코드만(source: str) -> str:
    """docstring 과 `#` 주석을 걷어낸 **실제로 실행되는 코드**.

    ⚠️ 원문을 그대로 뒤지면 *"라벨을 박지 않는다"* 고 **설명하는 문장**이 위반으로
       잡힌다. 설명과 실행문은 다른 것이고, 잠가야 할 것은 후자다.

    ★ **문자열 리터럴은 남긴다.** 라벨을 코드에 박는 것이 바로 잡으려는 위반이라,
      문자열까지 걷어내면 검사가 아무것도 안 잰다.
    """
    tree = ast.parse(source)
    코드 = source
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                코드 = 코드.replace(docstring, "", 1)
    return chr(10).join(line.split("#", 1)[0] for line in 코드.splitlines())


def _백필_코드() -> str:
    return _코드만(Path(backfill.__file__).read_text(encoding="utf-8"))


@pytest.mark.parametrize("라벨", ["기본", "공격", "보수"])
def test_코드에_시나리오_라벨이_박혀_있지_않다(라벨: str) -> None:
    """🔴 **'보수·기본·공격' 은 매입의 계약이다.**

    복제하면 매입이 라벨을 바꿀 때 조용히 어긋난다 — 어느 라벨인지는 **설정이 말한다.**
    """
    assert 라벨 not in _백필_코드(), f"백필 코드에 시나리오 라벨 '{라벨}' 이 박혀 있다"


def test_라벨_잠금이_실제로_잡는다() -> None:
    """🟢 **위 검사가 아무것도 안 재고 초록이 되는 것을 막는다.**

    ★ 라벨이 **코드**에 있으면 잡고, **docstring** 에만 있으면 안 잡는다 — 그 둘을
      가르는 것이 `_코드만` 의 일이다.
    """
    박은코드 = 'def f():\n    """설명: 기본 을 고른다."""\n    label = "기본"\n'
    설명만 = 'def f():\n    """설명: 기본 을 고른다."""\n    label = LABEL\n'

    assert "기본" in _코드만(박은코드)
    assert "기본" not in _코드만(설명만)


def test_승인_문을_우회하지_않는다() -> None:
    """★★ **다른 길을 내면 그 순간 「승인」이 두 종류가 된다.**

    백필 승인도 사람 승인과 같은 검사 · 같은 재검증 · 같은 이력을 지나야 한다.
    """
    원문 = Path(backfill.__file__).read_text(encoding="utf-8")
    코드 = _코드만(원문)

    for 우회 in ("save_decision", "apply_approval", "confirm_approved_sale", "revalidate_scenario"):
        assert f"{우회}(" not in 코드, f"백필이 승인 문을 건너뛰고 {우회} 를 직접 부른다"

    가져온것 = {
        별칭.name
        for node in ast.walk(ast.parse(원문))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for 별칭 in node.names
    }
    assert "save_decision" not in 가져온것, "백필이 적재 함수를 직접 들여왔다"
    assert "record_decision" in 가져온것, "백필이 승인 문을 안 들여왔다"


def test_CLI_진입점을_만들지_않았다() -> None:
    """🔴 **실수로 돌아갈 문을 안 만든다.** 돌리는 것은 별도 판이다."""
    코드 = _백필_코드()

    assert "argparse" not in 코드
    assert "def main(" not in 코드
    assert "__main__" not in 코드


def test_진입점이_걷기와_같은_모양이다() -> None:
    """★ 범위를 키워드로 받고, 갈아 끼울 자리에 **기본값이 진짜 함수**다."""
    서명 = inspect.signature(backfill_decisions)

    assert [이름 for 이름 in ("sim_run_id", "start", "end") if 이름 in 서명.parameters] == [
        "sim_run_id",
        "start",
        "end",
    ]
    assert all(칸.kind is inspect.Parameter.KEYWORD_ONLY for 칸 in 서명.parameters.values()), (
        "인자를 순서로 받으면 부르는 쪽이 범위를 뒤집어 넣을 수 있다"
    )
    assert 서명.parameters["decide"].default is not None
