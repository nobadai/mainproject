"""자동 백필 승인 — **과거 구간을 규칙 하나로 재현해 장부를 만든다.**

```text
백필 승인           과거 구간을 재현해 장부를 만드는 것   = 데이터 생성   ← 이 파일
에이전트 자율 승인   오늘의 안을 스스로 정하는 것          = 판단 위임    🔴 팀이 금지했다
```

★★ **어느 안이 나은지 판단하는 코드가 여기 없다.** 규칙이 가리키는 안을 그대로
  고르거나, 그 안이 없으면 **안 고른다.** 두 갈래뿐이라 고를 것이 없다.

★ **`backtest_runner.walk` 과 같은 모양이다** — 범위를 받고, 하루가 막혀도 밖으로
  예외를 안 내고, 무엇을 했는지를 값으로 돌려준다.

🔴 **CLI 진입점이 없다 — 일부러 없다.** 실수로 돌아갈 문을 안 만든다. 돌리는 것은
  별도 판이다 (`tests/master/test_backfill.py` 가 이것도 잠근다).

## 규칙의 집

```json
{"backfill": {"rule": "ALWAYS_BASE", "scenario_label": "기본"}}
```

`sim_runs.config_json` 이다. **한 실행 = 한 규칙**이고, 규칙은 행이 아니라 실행에 속한다.

🔴 **코드에 라벨을 박지 않는다.** `decision.scenario_labels_of` 가 이미 그 규율을
  적어 뒀다 — *"'보수·기본·공격' 은 매입의 계약이다. 여기에 복제하면 매입이 라벨을
  바꿀 때 조용히 어긋난다."* 그래서 코드가 아는 것은 **「고정 라벨 규칙」이라는
  모양**(`ALWAYS_BASE`)뿐이고, 어느 라벨인지는 **설정이 말한다.**

🔴 **기본 규칙을 지어내지 않는다.** 규칙 칸이 없으면 `NO_RULE` 이고 한 행도 안 쓴다 —
  기본값은 곧 업무 규칙이고, 그러면 아무도 안 정한 규칙으로 곡선이 선다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from app.master.decision import (
    SALES_CYCLE,
    DecisionIn,
    DecisionOut,
    RevalidationOutcome,
    approve_end_codes,
    available_scenario_names,
)
from app.master.decision_repository import list_decisions
from app.master.decision_service import record_decision
from app.master.ledger_repository import get_burn_in
from app.master.run_repository import list_runs

AUTO_BACKFILL = "AUTO-BACKFILL"
"""자동으로 채운 승인의 `decided_by`.

🔴 **사람 이름을 안 쓴다.** `master_decisions.decided_by` 는 지금 전부 사람 이름이라,
  자동으로 채우면서 거기 사람 이름을 적으면 **사람이 안 눌렀는데 눌렀다고 기록**되고
  그 표는 append-only 라 못 지운다.

★ `ask_service` 가 적어 둔 *"승인자가 없는 승인은 승인이 아니다"* 를 지키는 길이
  이것이다 — 자동일 때도 **「누가」를 정직하게** 적는다.
"""

BACKFILL_BOUNDARY_AS_OF = date(2026, 9, 9)
"""자동으로 채울 수 있는 마지막 날. **이 날까지 포함이다.**

```text
as_of <= 2026-09-09   자동으로 채운다
as_of >= 2026-09-10   🔴 사람만. 한 행도 안 쓴다
```

🔴 **`config_json` 으로 빼지 않는다.** 이건 규칙이 아니라 **가드**라, 옮기려면
  diff 에 보여야 한다. 설정으로 내리면 오늘 이후를 자동 승인하는 것이 **행 하나
  고치는 일**이 된다.
"""

ALWAYS_BASE = "ALWAYS_BASE"
"""**아는 규칙 이름은 이것 하나다.** 「그 실행이 정한 라벨 하나를 늘 고른다」는 모양.

⚠️ 이름이 가리키는 라벨은 여기 없다 — `scenario_label` 이 설정에 있다.
"""


BackfillOutcome = Literal[
    "RECORDED",
    "ALREADY_DECIDED",
    "NOT_APPROVABLE",
    "LABEL_NOT_OFFERED",
    "BLOCKED_BY_BOUNDARY",
    "FAILED",
]
"""실행 이력 한 행에 무슨 일이 있었나. **여섯을 접지 않는다.**

```text
RECORDED              승인을 적었다
ALREADY_DECIDED       이미 결정이 있다 — 덮지 않는다
NOT_APPROVABLE        승인이 성립하는 종료 코드가 아니다
LABEL_NOT_OFFERED     규칙이 가리키는 안이 그날 없다
BLOCKED_BY_BOUNDARY   as_of 가 경계 밖이다
FAILED                해 봤는데 터졌다 (사유를 같이 적는다)
```

⚠️ *"없다"* 와 *"안 했다"* 와 *"못 했다"* 를 묶으면 **왜 곡선이 평평한지를 결과가
  못 답한다.**
"""

BackfillStatus = Literal["RAN", "NO_RULE"]
"""실행 전체가 돌았나. 🔴 **`BackfillOutcome` 과 축이 다르다** — 저쪽은 행 하나이고
이쪽은 걷기 한 번이다. `NO_RULE` 이면 행을 아예 안 본다.
"""


class BackfillRuleMissing(Exception):
    """`sim_runs.config_json` 이 백필 규칙을 말하지 않는다.

    ★ **밖으로 안 나간다.** `backfill_decisions` 가 잡아 `NO_RULE` 로 값에 담는다 —
      이 사실도 결과의 일부라, 예외로 던지면 부르는 쪽이 *"안 돌았다"* 와 *"돌았는데
      할 것이 없었다"* 를 못 가른다.
    """


@dataclass(frozen=True)
class BackfillRule:
    """그 실행이 정한 백필 규칙."""

    #: 규칙의 이름. 지금은 `ALWAYS_BASE` 하나다.
    name: str
    #: 그 규칙이 늘 고르는 안의 이름. 🔴 **설정에서 온다 — 코드에 없다.**
    scenario_label: str


@dataclass(frozen=True)
class BackfilledRun:
    """실행 이력 한 행의 처리 결과."""

    as_of: date
    run_id: str
    request_id: str | None
    outcome: BackfillOutcome
    #: 왜 그 결과가 됐나. `RECORDED` 에는 없다.
    reason: str | None = None
    #: 승인 문이 돌린 재검증 결과. 🔴 **`RECORDED` 여도 `FAILED` 일 수 있다** —
    #: 결정 행은 쓰이고 효력은 재검증이 정한다 (`record_decision` 의 규율 그대로).
    revalidation_outcome: RevalidationOutcome | None = None


@dataclass(frozen=True)
class BackfillOut:
    """백필 한 번의 결과. **예외 대신 이것을 돌려준다.**"""

    sim_run_id: str
    start: date
    end: date
    status: BackfillStatus
    #: 그 실행이 정한 규칙. `NO_RULE` 이면 `None`.
    rule: BackfillRule | None = None
    #: `NO_RULE` 의 사유. 🔴 **`status` 와 짝이다.**
    reason: str | None = None
    #: 본 실행 이력 행마다 하나씩. **본 순서 그대로.**
    runs: tuple[BackfilledRun, ...] = ()
    #: 범위 안이지만 경계를 넘어 **자동으로는 못 채우는 날.**
    #:
    #: 🔴 **조용히 자르지 않는다.** 자르면 부르는 쪽이 *"179일을 채웠다"* 고 믿는다 —
    #:   그날에 행이 하나도 없어도 이 목록에는 남는다.
    blocked_days: tuple[date, ...] = ()

    @property
    def outcomes(self) -> Mapping[str, int]:
        """결과 분포. **여섯 값을 그대로 센다** — 새 이름을 안 붙인다."""
        return Counter(one.outcome for one in self.runs)


def read_rule(config_json: Mapping[str, Any]) -> BackfillRule:
    """`config_json` 이 말하는 백필 규칙.

    🔴 **없으면 지어내지 않고 막는다.** 모르는 규칙 이름도 마찬가지다 — 아는 모양이
      아닌 것을 아는 모양으로 접으면, 설정이 시킨 적 없는 규칙으로 장부가 선다.

    :raises BackfillRuleMissing: 규칙 칸이 없거나, 모르는 규칙이거나, 고를 안이 비었을 때.
    """
    section = config_json.get("backfill")
    if section is None:
        # ⚠️ 사유 문장에 시나리오 라벨을 쓰지 않는다 — `test_backfill` 이 원문을 읽어
        #   잠그므로, 안 이름과 겹치는 낱말은 설명에서도 피한다.
        raise BackfillRuleMissing(
            "sim_runs.config_json 에 backfill 칸이 없다 — 없는 규칙을 지어내지 않는다"
        )
    if not isinstance(section, Mapping):
        raise BackfillRuleMissing(
            f"backfill 칸이 객체가 아니다: {type(section).__name__} — 규칙을 읽을 수 없다"
        )
    name = section.get("rule")
    if name != ALWAYS_BASE:
        raise BackfillRuleMissing(
            f"모르는 백필 규칙이다: {name!r} — 아는 규칙은 {ALWAYS_BASE} 하나다"
        )
    label = section.get("scenario_label")
    if not isinstance(label, str) or not label.strip():
        raise BackfillRuleMissing(
            f"{ALWAYS_BASE} 는 고를 안을 설정이 말해야 한다 — scenario_label 이 비었다"
        )
    return BackfillRule(name=name, scenario_label=label)


def _config_of(sim_run_id: str) -> Mapping[str, Any]:
    """그 실행의 `config_json`.

    ★ **`sim_runs` 를 읽는 주인을 하나 더 만들지 않는다.** `ledger_repository` 가
      이미 그 행을 읽는다 — 여기에 SELECT 를 또 적으면 컬럼이 바뀌는 날 두 곳이
      어긋난다.
    """
    run = get_burn_in(sim_run_id).get("run")
    if not isinstance(run, Mapping):
        return {}
    config = run.get("config_json")
    return config if isinstance(config, Mapping) else {}


def backfill_decisions(
    *,
    sim_run_id: str,
    start: date,
    end: date,
    load_config: Callable[[str], Mapping[str, Any]] = _config_of,
    runs_on: Callable[..., Sequence[Mapping[str, Any]]] = list_runs,
    decisions_of: Callable[[str], Sequence[DecisionOut]] = list_decisions,
    decide: Callable[[str, DecisionIn], DecisionOut] = record_decision,
    limit_per_day: int = 500,
) -> BackfillOut:
    """`start` 부터 `end` 까지, 규칙이 가리키는 안을 **승인 문으로** 승인한다.

    ★★ **승인 문을 우회하지 않는다.** `record_decision` 을 그대로 부른다 —
      `save_decision` 을 직접 부르거나 재검증을 건너뛰면 그 순간 **「승인」이 두
      종류**가 된다. 백필 승인도 사람 승인과 같은 검사 · 같은 재검증 · 같은 이력을
      지나야 한다. 느려도 그것이 맞다.

    ⚠️ 재검증이 벽시계로 돌지 않는다 — `record_decision` 안쪽의 `_revalidation_for`
      가 **그 실행 행의 `as_of`** 를 쓴다. 그래서 과거 구간을 오늘 재현해도 오늘로
      개장을 묻지 않는다.

    :param load_config: 규칙을 읽는 자리. 기본은 `sim_runs.config_json`.
    :param runs_on: 하루치 실행 이력. `list_runs` 와 같은 키워드로 부른다.
    :param decisions_of: 그 업무 키에 이미 붙은 결정.
    :param decide: 🔴 **승인 문.** 기본값이 `record_decision` 자체다 — `None` 을
        안 받는다 (`backtest_runner.walk` 의 `run_day_fn` 과 같은 규율).
    :raises ValueError: 범위가 거꾸로일 때. **막고 사유를 낸다.**
    :raises LookupError: 그 실행을 못 찾아 규칙을 **읽지도 못했을** 때.
        🔴 *"규칙이 없다"* 와 섞지 않는다 — 저쪽은 값(`NO_RULE`)이고 이쪽은 사고다.
    """
    if start > end:
        raise ValueError(
            f"백필 범위가 거꾸로다: {start.isoformat()} ~ {end.isoformat()}"
            " — 어느 쪽이 시작인지를 여기서 정하지 않는다"
        )

    try:
        rule = read_rule(load_config(sim_run_id))
    except BackfillRuleMissing as exc:
        # 🔴 **한 행도 안 쓰고 돌아간다.** `decide` 를 한 번도 안 부른다.
        return BackfillOut(
            sim_run_id=sim_run_id, start=start, end=end, status="NO_RULE", reason=str(exc)
        )

    results: list[BackfilledRun] = []
    blocked: list[date] = []

    day = start
    while day <= end:
        if day > BACKFILL_BOUNDARY_AS_OF:
            blocked.append(day)
        for row in runs_on(sim_run_id=sim_run_id, as_of=day, limit=limit_per_day):
            results.append(_backfill_one(row, rule, decisions_of=decisions_of, decide=decide))
        day += timedelta(days=1)

    return BackfillOut(
        sim_run_id=sim_run_id,
        start=start,
        end=end,
        status="RAN",
        rule=rule,
        runs=tuple(results),
        blocked_days=tuple(blocked),
    )


def _backfill_one(
    row: Mapping[str, Any],
    rule: BackfillRule,
    *,
    decisions_of: Callable[[str], Sequence[DecisionOut]],
    decide: Callable[[str, DecisionIn], DecisionOut],
) -> BackfilledRun:
    """실행 이력 한 행을 규칙대로 처리한다. **어느 안이 나은지 안 따진다.**"""
    as_of = row["as_of"]
    run_id = str(row["run_id"])
    request_id = row.get("request_id")
    cycle = row.get("cycle") if isinstance(row.get("cycle"), str) else ""

    def 결과(outcome: BackfillOutcome, reason: str | None = None) -> BackfilledRun:
        return BackfilledRun(
            as_of=as_of,
            run_id=run_id,
            request_id=request_id,
            outcome=outcome,
            reason=reason,
        )

    # ── ① 경계 ──────────────────────────────────────────────────────
    # 🔴 **가장 먼저 본다.** 뒤로 밀면 그 앞 검사가 하나 바뀌는 날 경계가 새 나간다.
    #    루프가 아니라 **행의 `as_of`** 로 잰다 — 조회 필터가 무엇을 걸었든 가드는
    #    자기 눈으로 본다.
    if as_of > BACKFILL_BOUNDARY_AS_OF:
        return 결과(
            "BLOCKED_BY_BOUNDARY",
            f"{as_of.isoformat()} 은 백필 경계({BACKFILL_BOUNDARY_AS_OF.isoformat()}) 밖이다"
            " — 그 뒤는 사람만 승인한다",
        )

    # ── ② 승인이 성립하는 실행인가 ──────────────────────────────────
    # ★ 어느 종료 코드에 승인이 서는지는 `decision.approve_end_codes` 가 주인이다.
    #   여기에 코드를 베끼면 그쪽이 바뀌는 날 백필만 옛 규칙으로 돈다.
    #
    # 🔴 **판매 실행은 대상이 아니다.** 백필이 재현하는 것은 매입 승인 구간이고,
    #   판매 확정을 같이 자동화하면 이 판이 정한 범위 밖으로 나간다.
    end_code = row.get("end_code")
    if cycle == SALES_CYCLE or end_code not in approve_end_codes(cycle):
        return 결과("NOT_APPROVABLE", f"종료 코드가 {end_code!r} 다 (cycle={cycle!r})")

    if not isinstance(request_id, str) or not request_id:
        return 결과("FAILED", "실행 행에 업무 키가 없어 승인 문을 부를 수 없다")

    # ── ③ 이미 결정이 있으면 덮지 않는다 ────────────────────────────
    if decisions_of(request_id):
        return 결과("ALREADY_DECIDED", "이미 붙은 결정이 있다 — 자동이 사람 결정을 덮지 않는다")

    # ── ④ 규칙이 가리키는 안이 그날 있었나 ──────────────────────────
    # 🔴 **없으면 다른 안으로 대체하지 않는다.** 대체하면 곡선이 규칙과 다른 것을
    #   재현하고, *"규칙이 정한 안을 늘 고른 곡선"* 이라는 발표 문장이 거짓이 된다.
    response_payload = row.get("response_payload") or {}
    available = available_scenario_names(response_payload, cycle)
    if rule.scenario_label not in available:
        shown = ", ".join(available) if available else "(없음)"
        return 결과(
            "LABEL_NOT_OFFERED",
            f"규칙이 가리키는 안이 그날 없다 (제시된 안: {shown})",
        )

    # ── ⑤ 승인 문 ───────────────────────────────────────────────────
    try:
        saved = decide(
            request_id,
            DecisionIn(
                decision="APPROVE",
                scenario_label=rule.scenario_label,
                decided_by=AUTO_BACKFILL,
                history_run_id=run_id,
                # 🟡 되짚을 때 눈으로 보라는 것뿐이다. ⚠️ **파싱하지 않는다** —
                #    자유 텍스트라 규칙 이름의 주인이 아니다. 주인은 `config_json` 이다.
                note=f"{AUTO_BACKFILL} rule={rule.name}",
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 한 날이 막혀도 나머지 날은 채운다.
        return 결과("FAILED", f"{type(exc).__name__}: {exc}")

    return BackfilledRun(
        as_of=as_of,
        run_id=run_id,
        request_id=request_id,
        outcome="RECORDED",
        revalidation_outcome=saved.revalidation_outcome,
    )
