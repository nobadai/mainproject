"""**자동 백필 진입점 — 세어 보이는 것이 기본이고, 가드가 셋이다.**

🔴 **DB 를 안 탄다.** 규칙 · 실행 이력 · 기존 결정 · 승인 문을 전부 대역으로 준다.
   이 판은 **한 행도 안 쓴다** — 승인 문 대역은 부른 횟수를 세기만 하고, 세어 보기가
   걸린 자리에서는 그 목록이 **비어 있어야 한다.**

⚠️ **실제로 채우지 않는다.** 이 판은 문을 세우는 것까지다. 179 영업일을 실제로
  채우는 것은 사람이 `--commit` 을 직접 치는 다음 판이다.

★★ **잡으려는 사고의 모양이 하나 있다** — *"결과에는 적을 것처럼 세어 놓고 문은
  부르는"* 것. 값이 맞는 것으로는 부족해서, 아래 검사들은 **어휘별 수와 문을 부른
  횟수를 같이** 잰다.
"""

from __future__ import annotations

import ast
import inspect
import unicodedata
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.master import backfill_runner
from app.master.backfill import ALWAYS_BASE, AUTO_BACKFILL, BackfillOut, backfill_decisions
from app.master.backfill_runner import (
    BackfillRunOut,
    CountingDoor,
    format_summary,
    run_backfill,
)
from app.master.decision import DecisionIn, DecisionOut
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

#: 이 검사가 쓰는 실행 축. 🔴 **번인을 안 쓴다** — 번인은 거부 검사에서만 쓴다.
실행축 = "SIM-TEST-BACKFILL"

기본 = "기본"
공격 = "공격"

설정 = {"backfill": {"procurement": {"rule": ALWAYS_BASE, "scenario_label": 기본}}}

#: 경계(`BACKFILL_BOUNDARY_AS_OF`) 안쪽 날들. ⚠️ **경계를 여기서 다시 검사하지
#: 않는다** — 이 판이 재는 것은 문이지 경계가 아니다.
하루 = date(2026, 9, 7)
이틀 = date(2026, 9, 8)
사흘 = date(2026, 9, 9)


# ── 대역 ────────────────────────────────────────────────────────────────


def 실행행(
    as_of: date,
    *,
    end_code: str | None = "E1_APPROVED",
    labels: tuple[str, ...] = (기본, 공격),
    request_id: str = "REQ-1",
) -> dict[str, Any]:
    """`master_agent_runs` 매입 한 행의 대역. **읽는 칸만 채운다.**"""
    return {
        "run_id": uuid4(),
        "request_id": request_id,
        "as_of": as_of,
        "cycle": "PROCUREMENT",
        "end_code": end_code,
        "response_payload": {
            "end_code": end_code,
            "scenarios": [{"label": label} for label in labels],
            "candidates": [{"scenario": {"scenario_id": label}} for label in labels],
        },
    }


class _승인문:
    """`record_decision` 대역. **부른 것을 그대로 모아 둔다.**

    🔴 **아무 데도 안 쓴다.** 이 판이 재는 것은 *"불렀나 안 불렀나"* 이지 적재가
      아니다 — 적재를 흉내 내면 이 판이 *"한 행도 안 쓴다"* 를 못 지킨다.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, DecisionIn]] = []

    def __call__(self, request_id: str, payload: DecisionIn) -> DecisionOut:
        self.calls.append((request_id, payload))
        return DecisionOut(
            decision_id=uuid4(),
            request_id=request_id,
            decision_seq=1,
            decision=payload.decision,
            scenario_label=payload.scenario_label,
            decided_by=payload.decided_by,
            end_code_at_decision="E1_APPROVED",
            history_run_id=payload.history_run_id,
            note=payload.note,
            created_at=datetime(2026, 9, 9, 10, 0, tzinfo=UTC),
        )


def _진짜_백필(행들: dict[date, list[dict[str, Any]]]):
    """**진짜 `backfill_decisions`** 에 DB 대신 대역만 물린 것.

    ★★ **판단을 흉내 내지 않는다.** 어휘별 수가 갈리는지를 재려면 경계 · 종료 코드 ·
      규칙 · 기존 결정 · 안의 유무를 **진짜 판단이** 봐야 한다 — 여기서 가짜 수를
      만들면 이 판은 자기가 만든 숫자를 다시 읽는 것뿐이다.
    """
    return partial(
        backfill_decisions,
        load_config=lambda _: 설정,
        runs_on=lambda *, sim_run_id, as_of, limit: 행들.get(as_of, []),
        decisions_of=lambda request_id: [],
    )


def _돌린다(
    행들: dict[date, list[dict[str, Any]]],
    문: _승인문,
    *,
    commit: bool = False,
    sim_run_id: str = 실행축,
) -> BackfillRunOut:
    return run_backfill(
        sim_run_id=sim_run_id,
        start=하루,
        end=사흘,
        commit=commit,
        backfill_fn=_진짜_백필(행들),
        door=문,
    )


def _세줄(rows: dict[date, list[dict[str, Any]]] | None = None) -> dict[date, list[dict[str, Any]]]:
    """세 날에 승인 가능한 행이 하나씩."""
    return rows or {
        하루: [실행행(하루, request_id="REQ-1")],
        이틀: [실행행(이틀, request_id="REQ-2")],
        사흘: [실행행(사흘, request_id="REQ-3")],
    }


def _코드만(source: str) -> str:
    """docstring 과 `#` 주석을 걷어낸 **실제로 실행되는 코드**.

    ⚠️ 원문을 그대로 뒤지면 *"경계를 다시 검사하지 않는다"* 고 **설명하는 문장**이
       위반으로 잡힌다. 설명과 실행문은 다른 것이고, 잠가야 할 것은 후자다.
    """
    tree = ast.parse(source)
    코드 = source
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                코드 = 코드.replace(docstring, "", 1)
    return chr(10).join(line.split("#", 1)[0] for line in 코드.splitlines())


def _NFC(text: str) -> str:
    """한글 문장을 자모 분리 없이 대조한다."""
    return unicodedata.normalize("NFC", text)


# ── ① 기본은 안 쓴다 ────────────────────────────────────────────────────


def test_commit_없이_돌면_승인_문을_한_번도_안_부른다() -> None:
    """🔴 **`master_decisions` 는 append-only 라 못 지운다.**

    ★★ **값만으로는 부족하다.** *"적을 것처럼 세어 놓고 문은 부르는"* 것이 실제
      사고의 모양이라, 어휘별 수와 문을 부른 횟수를 **같이** 잰다.
    """
    문 = _승인문()

    결과 = _돌린다(_세줄(), 문)

    assert 문.calls == [], "세어 보기인데 승인 문을 불렀다"
    assert 결과.committed is False
    assert 결과.result.outcomes["RECORDED"] == 3, "적을 것을 세지도 않았다"


def test_commit_을_주면_승인_문을_부른다() -> None:
    """🟢 **자기 생존 검사.** 이것이 없으면 *"안 부른다"* 는 **아무것도 안 부르는
    코드**로도 통과한다.
    """
    문 = _승인문()

    결과 = _돌린다(_세줄(), 문, commit=True)

    assert [request_id for request_id, _ in 문.calls] == ["REQ-1", "REQ-2", "REQ-3"]
    assert {payload.decided_by for _, payload in 문.calls} == {AUTO_BACKFILL}
    assert 결과.committed is True
    assert 결과.result.outcomes["RECORDED"] == 3


def test_세어_본_수가_적었을_때_실제로_적히는_수와_같다() -> None:
    """★★ **세어 보이기가 쓸모 있으려면 두 수가 같아야 한다.**

    다르면 사람은 세어 본 수를 믿고 `--commit` 을 주는데 장부에는 다른 수가 앉는다.
    """
    행들 = _세줄()

    센문 = _승인문()
    세어봄 = _돌린다(행들, 센문)

    적은문 = _승인문()
    적었음 = _돌린다(행들, 적은문, commit=True)

    assert 센문.calls == [], "세어 보기인데 승인 문을 불렀다"
    assert 세어봄.result.outcomes["RECORDED"] == len(적은문.calls)
    assert dict(세어봄.result.outcomes) == dict(적었음.result.outcomes)


def test_세어_보이는_값이_어휘별로_갈린다() -> None:
    """🟢 **접지 않는다.** *"적힐 것"* 과 *"안 될 것"* 과 *"못 할 것"* 은 다른 사실이다.

    ⚠️ 한 수로 묶으면 사람이 `--commit` 을 주기 전에 **무엇이 왜 안 채워지는지**를
      못 본다.
    """
    문 = _승인문()

    결과 = _돌린다(
        {
            하루: [실행행(하루, request_id="REQ-1")],
            # 규칙이 가리키는 안이 그날 없다.
            이틀: [실행행(이틀, request_id="REQ-2", labels=(공격,))],
            # 승인이 성립하는 종료 코드가 아니다.
            사흘: [실행행(사흘, request_id="REQ-3", end_code="E4_BLOCKED")],
        },
        문,
    )

    assert dict(결과.result.outcomes) == {
        "RECORDED": 1,
        "LABEL_NOT_OFFERED": 1,
        "NOT_APPROVABLE": 1,
    }
    assert 문.calls == [], "어휘별로 세면서 승인 문을 불렀다"


def test_commit_기본값이_안_쓰는_쪽이다() -> None:
    """🔴 **기본이 쓰는 쪽이면 실수 한 번이 되돌릴 수 없는 일이 된다.**"""
    args = backfill_runner._parser().parse_args(
        ["--sim-run-id", 실행축, "--start", "2026-09-07", "--end", "2026-09-09"]
    )

    assert args.commit is False, "--commit 을 안 줬는데 적는 쪽으로 섰다"
    assert inspect.signature(run_backfill).parameters["commit"].default is False


# ── ② 실행 축 ───────────────────────────────────────────────────────────


def test_sim_run_id_에_기본값이_없다() -> None:
    """🔴 `walk()` 과 같은 규율이다 — **안 주면 터진다.**"""
    칸 = inspect.signature(run_backfill).parameters["sim_run_id"]

    assert 칸.default is inspect.Parameter.empty, "실행 축에 기본값이 생겼다"

    with pytest.raises(SystemExit):
        backfill_runner.main(["--start", "2026-09-07", "--end", "2026-09-09"])


def test_빈_실행_축이면_한_행도_안_쓰고_터진다() -> None:
    """⚠️ argparse 는 빈 문자열을 통과시킨다 — **그 자리도 막는다.**"""
    문 = _승인문()

    with pytest.raises(ValueError, match="sim_run_id"):
        _돌린다(_세줄(), 문, commit=True, sim_run_id="   ")

    assert 문.calls == []


# ── ③ 번인 ──────────────────────────────────────────────────────────────


def test_번인_실행이면_터지고_백필을_아예_안_부른다() -> None:
    """🔴 **번인 장부는 모든 실행의 기초 상태다.**

    ⚠️ 값으로 막는 것으로는 부족하다 — 백필이 **불리지도 않아야** 한다.
    """
    불린것: list[dict[str, Any]] = []

    def _백필(**kwargs: Any) -> BackfillOut:
        불린것.append(kwargs)
        raise AssertionError("번인인데 백필이 돌았다")

    with pytest.raises(ValueError, match=BURN_IN_SIM_RUN_ID):
        run_backfill(
            sim_run_id=BURN_IN_SIM_RUN_ID,
            start=하루,
            end=사흘,
            commit=True,
            backfill_fn=_백필,
        )

    assert 불린것 == []


def test_번인이_아닌_실행은_통과한다() -> None:
    """🟢 **자기 생존 검사.** 이것이 없으면 *"번인은 거부한다"* 는 **모든 실행을
    거부하는 코드**로도 통과한다.
    """
    문 = _승인문()

    결과 = _돌린다(_세줄(), 문)

    assert 결과.result.status == "RAN"


# ── ④ 출력 ──────────────────────────────────────────────────────────────


def test_안_썼다는_것이_요약에_뚜렷하다() -> None:
    """⚠️ **조용히 안 쓰면 사람이 썼다고 믿는다.**"""
    요약 = _NFC(format_summary(_돌린다(_세줄(), _승인문())))

    assert _NFC("안 썼다") in 요약
    assert "--commit" in 요약
    assert _NFC("적었다") not in 요약


def test_적었을_때는_적었다고_말한다() -> None:
    """🟢 **자기 생존 검사.** 두 판이 같은 문장을 내면 문장이 아무것도 안 가른다."""
    요약 = _NFC(format_summary(_돌린다(_세줄(), _승인문(), commit=True)))

    assert _NFC("적었다") in 요약
    assert _NFC("안 썼다") not in 요약


def test_요약이_어휘별로_보여_준다() -> None:
    """⚠️ **화면에 안 나오는 어휘는 없는 어휘와 같다.**"""
    요약 = _NFC(
        format_summary(
            _돌린다(
                {
                    하루: [실행행(하루, request_id="REQ-1")],
                    이틀: [실행행(이틀, request_id="REQ-2", labels=(공격,))],
                },
                _승인문(),
            )
        )
    )

    assert "RECORDED" in 요약
    assert "LABEL_NOT_OFFERED" in 요약


# ── ⑤ 진입점이 그대로 넘긴다 ────────────────────────────────────────────


def _가짜_결과(sim_run_id: str, start: date, end: date) -> BackfillRunOut:
    return BackfillRunOut(
        committed=False,
        result=BackfillOut(sim_run_id=sim_run_id, start=start, end=end, status="RAN"),
    )


def test_진입점이_commit_을_그대로_넘긴다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ **진입점에 로직이 없다.** 문자열을 날짜로 바꾸는 것뿐이다."""
    본것: dict[str, Any] = {}

    def _가짜(**kwargs: Any) -> BackfillRunOut:
        본것.update(kwargs)
        return _가짜_결과(kwargs["sim_run_id"], kwargs["start"], kwargs["end"])

    monkeypatch.setattr(backfill_runner, "run_backfill", _가짜)

    코드 = backfill_runner.main(
        ["--sim-run-id", 실행축, "--start", "2026-09-07", "--end", "2026-09-09"]
    )

    assert 본것["sim_run_id"] == 실행축
    assert 본것["start"] == date(2026, 9, 7)
    assert 본것["end"] == date(2026, 9, 9)
    assert 본것["commit"] is False, "--commit 을 안 줬는데 적는 쪽으로 넘겼다"
    assert 코드 == 0


def test_진입점이_commit_을_줬을_때만_참으로_넘긴다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🟢 **자기 생존 검사.** 늘 거짓을 넘기는 코드로는 통과하면 안 된다."""
    본것: dict[str, Any] = {}

    def _가짜(**kwargs: Any) -> BackfillRunOut:
        본것.update(kwargs)
        return _가짜_결과(kwargs["sim_run_id"], kwargs["start"], kwargs["end"])

    monkeypatch.setattr(backfill_runner, "run_backfill", _가짜)

    backfill_runner.main(
        ["--sim-run-id", 실행축, "--start", "2026-09-07", "--end", "2026-09-09", "--commit"]
    )

    assert 본것["commit"] is True


# ── ⑥ 안 한 것을 안 한 채로 둔다 ────────────────────────────────────────


def test_경계를_다시_검사하지_않는다() -> None:
    """🔴 **두 곳에서 막으면 언젠가 한쪽만 고쳐진다.**

    ★ 경계의 주인은 `backfill.BACKFILL_BOUNDARY_AS_OF` 하나다 — 이 파일은 그것이
      막아 놓은 날을 **세어 보이기만** 한다.
    """
    코드 = _코드만(Path(backfill_runner.__file__).read_text(encoding="utf-8"))

    assert "BACKFILL_BOUNDARY_AS_OF" not in 코드, "진입점이 경계를 다시 검사한다"
    assert "date(2026" not in 코드, "진입점에 경계 날짜가 박혀 있다"


def test_조립_뿌리가_저절로_승인을_켜지_않는다() -> None:
    """🔴 **불러야 돈다.** `bootstrap` 에 안 끼운다.

    ⚠️ 끼우면 자동 승인을 명시로만 켠다는 규율이 **프로세스 시작 한 번으로** 뚫린다.

    ★★ **`scheduler` 는 이 잠금에서 빠졌다** (2026-09-11). 걷기 안에 승인 자리가
      섰기 때문이다 — 그 자리가 없으면 **다음 날이 어제 산 것을 못 보고** 179일을
      걸어도 재고가 안 쌓인다.

      🔴 **규율은 그대로다. 잠금이 옮겨 갔을 뿐이다.** 거기도 기본이 꺼짐이고
        `--auto-approve` 를 명시로 줘야 서며, 그 사실은
        `tests/master/test_walk_auto_approve.py` 가 잠근다 — 기본값 셋 · 안 주면
        이름조차 안 불림 · 설정에 규칙이 있어도 안 켜짐.
    """
    from app.master import bootstrap

    원문 = Path(bootstrap.__file__).read_text(encoding="utf-8")

    assert "backfill" not in _코드만(원문), "조립 뿌리가 백필을 부른다"


def test_세어_보기가_승인_문을_들여오지_않는다() -> None:
    """🔴 **`CountingDoor` 는 적재 함수를 모른다.** 아는 순간 세어 보기가 아니다."""
    문 = CountingDoor()

    돌려준것 = 문("REQ-1", DecisionIn(decision="APPROVE", scenario_label=기본, decided_by="X"))

    assert 돌려준것.revalidation_outcome is None
    assert not isinstance(돌려준것, DecisionOut), "세어 보기가 결정을 흉내 냈다"
    assert [request_id for request_id, _ in 문.would_record] == ["REQ-1"]
