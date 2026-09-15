"""**시뮬레이션 실행 축이 상수에서 인자로 올라섰나** (2026-09-10).

```text
~2026-09-09   ledger_repository.BURN_IN_SIM_RUN_ID 하나가 축이었다
2026-09-10~   walk 이 축을 받고 · 승인이 실행 행에서 축을 읽고 · 원장이 그것을 쓴다
```

🔴 **DB 를 안 탄다.** 커넥션도 실행 행도 전부 대역이다 — 이 판은 자리를 세우는 것까지고,
   실제로 걷거나 `sim_runs` 에 행을 쓰는 것은 이 판이 하지 않는다.

⚠️ **`run_scheduled_day` 의 기본값은 여기서 안 잰다.** 그 사실의 주인은
  `tests/master/test_closing.py::test_하루_실행의_sim_run_id_기본값이_마스터_상수다`
  이고, 여기에 한 벌 더 두면 한쪽만 고치는 날 두 검사가 갈린다.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

import pytest

from app.master import decision_service as svc
from app.master import ledger, transition
from app.master.backtest_runner import walk
from app.master.commitment import ApprovedCommitment, ArrivalLeg
from app.master.decision import DecisionIn, DecisionOut
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.scheduler import DayRunOutcome, ScheduledAction
from app.master.sim_run import build_sim_run_id, create_sim_run

AS_OF = date(2025, 12, 31)

#: 이 검사가 쓰는 실행 축. 🔴 **운영값(`BURN_IN_SIM_RUN_ID`)과 다르다** — 같으면
#:   축을 상수에서 다시 읽는 뮤턴트가 전부 살아남는다.
실행축 = "SIM-WALK-202601"

_MASTER = Path(__file__).resolve().parents[2] / "app" / "master"


def _벗긴_원문(path: Path) -> str:
    """주석과 docstring 을 걷어낸 코드.

    🔴 **왜 걷어내나.** 이 파일들은 근거를 길게 적는다 — `"SIM-"` 이나 `split` 이
      **설명 문장 안에** 있어서 원문 잠금이 늘 통과하거나 늘 실패하면, 그 검사는
      코드를 재는 것이 아니라 문장을 재는 것이 된다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree)


# ── ① 걷기가 축을 받는다 ───────────────────────────────────────────────


class _달력:
    def is_market_open(self, day: date) -> bool:
        return True


def _게이트(as_of: date) -> Any:
    from app.master.forecast_gate import DayForecastReadiness

    return DayForecastReadiness(as_of=as_of, readiness="ALL_READY", items=())


class _하루대역:
    """하루 실행 대역. **받은 `sim_run_id` 를 그대로 모아 둔다.**"""

    def __init__(self) -> None:
        self.axes: list[str | None] = []

    def __call__(self, action: ScheduledAction, **kwargs: Any) -> DayRunOutcome:
        self.axes.append(kwargs.get("sim_run_id"))
        return DayRunOutcome(as_of=action.as_of, action=action.action, reason=action.reason)


def _걷는다(**over: Any) -> tuple[Any, _하루대역]:
    하루 = over.pop("run_day_fn", None) or _하루대역()
    인자: dict[str, Any] = {
        "sim_run_id": 실행축,
        "start": date(2026, 2, 9),
        "end": date(2026, 2, 11),
        "now": datetime(2026, 2, 9, 10, 35, tzinfo=UTC),
        "calendar": _달력,
        "readiness": _게이트,
        "run_day_fn": 하루,
        "ticks": lambda: 0.0,
    }
    인자.update(over)
    return walk(**인자), 하루


def test_걷기에_축을_안_주면_터진다() -> None:
    """🔴 **조용히 번인으로 안 떨어진다.**

    ★★ 179일을 **어느 실행에 쌓는지**가 곧 그 곡선의 정체다. 기본값이 있으면
      말 안 하고 번인(`SIM-BURNIN-202512`)에 쌓을 수 있고, 사람이 심어 둔 30일 위에
      179일이 겹쳐 앉는다 — 그 뒤로는 어느 행이 무엇인지 되가를 방법이 없다.
    """
    with pytest.raises(TypeError, match="sim_run_id"):
        walk(  # type: ignore[call-arg]
            start=date(2026, 2, 9),
            end=date(2026, 2, 9),
            now=datetime(2026, 2, 9, 10, 35, tzinfo=UTC),
        )


def test_걷기의_축에_기본값이_없다() -> None:
    """★ **자기 생존.** 위 검사는 `TypeError` 만 보므로, 기본값이 살아나는 뮤턴트를
    잡으려면 서명 자체를 잠가야 한다.
    """
    파라미터 = inspect.signature(walk).parameters["sim_run_id"]

    assert 파라미터.default is inspect.Parameter.empty, (
        f"걷기의 축에 기본값이 생겼다: {파라미터.default!r} — 말 안 하고 그 실행에 쌓인다"
    )
    assert 파라미터.kind is inspect.Parameter.KEYWORD_ONLY


def test_빈_축은_걷기가_막는다() -> None:
    """★ 빈 문자열은 *"안 준 것"* 이지 *"준 것"* 이 아니다."""
    with pytest.raises(ValueError, match="sim_run_id"):
        _걷는다(sim_run_id="   ")


def test_걷기가_받은_축을_하루_실행까지_그대로_나른다() -> None:
    """🔴 **중간에서 안 바뀐다.** 걷기가 말한 실행과 하루가 쌓은 실행이 갈리면,
    성적표가 자기가 무엇을 쟀는지 모른다.
    """
    _, 하루 = _걷는다()

    assert 하루.axes == [실행축] * 3, f"하루 실행이 받은 축: {하루.axes}"
    assert BURN_IN_SIM_RUN_ID not in 하루.axes, "걷기가 중간에서 상수로 바꿨다"


def test_걷기_원문이_번인_상수를_안_든다() -> None:
    """★ **자기 생존.** 위 검사가 통과해도, 상수를 몰래 섞어 쓰는 갈래가 있으면 안 된다."""
    원문 = _벗긴_원문(_MASTER / "backtest_runner.py")

    assert "BURN_IN_SIM_RUN_ID" not in 원문, "걷기가 실행 축 상수를 스스로 든다"


# ── ② 원장이 받은 축을 쓴다 ────────────────────────────────────────────


def _commitment(*, payment_due_date: date | None = AS_OF) -> ApprovedCommitment:
    return ApprovedCommitment(
        approval_id="H1-REQ-1-1",
        request_id="REQ-1",
        as_of=AS_OF,
        item="배추",
        scenario_label="보수",
        total_qty_kg=44.0,
        total_amount_krw=228800.0,
        arrival_schedule=(
            ArrivalLeg(
                item="배추",
                qty_kg=44.0,
                arrival_date=date(2026, 1, 2),
                purchase_date=AS_OF,
                seq=1,
                payment_due_date=payment_due_date,
            ),
        ),
        inbound_lead_days=2.0,
    )


def test_원장이_받은_축을_돌려준다() -> None:
    """🔴 **상수를 안 읽는다.** 읽으면 부르는 쪽이 무엇을 지정하든 소용이 없다."""
    assert ledger.sim_run_id_for(_commitment(), sim_run_id=실행축) == 실행축
    assert ledger.sim_run_id_for(_commitment(), sim_run_id=실행축) != BURN_IN_SIM_RUN_ID


@pytest.mark.parametrize("없는_축", [None, "", "   "])
def test_축을_못_받으면_원장이_터진다(없는_축: str | None) -> None:
    """🔴 **조용히 번인으로 떨어지면 재무 채무와 매입 원장이 다른 실행에 앉는다.**

    ★ 재무는 자기 축(`finance_states`)을 읽고 원장만 번인을 쓰게 되므로, 그 어긋남은
      **아무 오류도 안 낸다** — 여기서 막지 않으면 아무 데서도 안 막힌다.
    """
    with pytest.raises(ValueError, match="sim_run_id"):
        ledger.sim_run_id_for(_commitment(), sim_run_id=없는_축)


def test_원장_원문이_번인_상수를_안_든다() -> None:
    """★ **자기 생존.** `sim_run_id_for` 가 상수를 다시 읽으면 여기서 걸린다."""
    원문 = _벗긴_원문(_MASTER / "ledger.py")

    assert "BURN_IN_SIM_RUN_ID" not in 원문, "매입 원장이 실행 축 상수를 스스로 든다"


def test_원장_행이_받은_축을_싣는다() -> None:
    """★ 함수가 값을 돌려주는 것과 **행에 실리는 것**은 다른 사실이다."""
    purchase_ids = {1: transition.purchase_id_for(_commitment(), 1)}
    행들 = ledger.build_purchase_rows(_commitment(), purchase_ids=purchase_ids, sim_run_id=실행축)

    assert [row.sim_run_id for row in 행들] == [실행축]


# ── ③ 승인이 실행 행에서 축을 읽는다 ───────────────────────────────────


class _가짜커서:
    """`items` 조회만 답하고 나머지는 센다."""

    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self.rowcount = 1
        self._row: dict[str, str] | None = None
        self._log = log

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        text = str(query)
        self._log.append((text, params))
        self._row = {"item_id": "ITEM-BAECHU"} if "FROM" in text and "items" in text else None

    def fetchone(self) -> dict[str, str] | None:
        return self._row


class _가짜커넥션:
    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []
        self.commits = 0

    def cursor(self) -> _가짜커서:
        return _가짜커서(self.log)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


class _가짜전이:
    def __init__(self, name: str) -> None:
        self.name = name

    def build(self, commitment: Any, **kwargs: Any) -> Any:
        return {"part": self.name}

    def persist(self, conn: Any, row: Any) -> None:
        return None


def _응답() -> dict[str, Any]:
    return {
        "end_code": "E1_APPROVED",
        "as_of": AS_OF.isoformat(),
        "scenarios": [
            {
                "label": "보수",
                "total_qty_kg": 44.0,
                "total_amount_krw": 228800.0,
                "split_plan": [{"seq": 1, "date": AS_OF.isoformat(), "qty_kg": 44.0}],
            }
        ],
        "judgment": {"meta": {"item": "배추"}},
        "constraints": {
            "inventory": {"inbound_lead_days": 2.0},
            "finance": {"purchase_payment_days": 0},
        },
    }


def _승인한다(monkeypatch: pytest.MonkeyPatch, *, 실행행_축: str | None) -> tuple[Any, _가짜커넥션]:
    """결정 경로를 대역으로 태우고 **실행 행이 실은 축**만 갈아 끼운다."""
    conn = _가짜커넥션()
    transition.reset()
    transition.register_transition("finance", _가짜전이("finance"))
    transition.register_transition("logistics", _가짜전이("logistics"))

    def _run_for(request_id: str, history_run_id: str | None) -> dict[str, Any]:
        return {
            "run_id": uuid4(),
            "request_id": request_id,
            "response_payload": _응답(),
            "sim_run_id": 실행행_축,
        }

    def _save(**kw: Any) -> DecisionOut:
        return DecisionOut(
            decision_id=uuid4(),
            created_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
            is_current=True,
            **kw,
        )

    real_apply = transition.apply_approval
    monkeypatch.setattr(svc, "_run_for", _run_for)
    monkeypatch.setattr(svc, "list_decisions", lambda request_id: [])
    monkeypatch.setattr(svc, "save_decision", _save)
    monkeypatch.setattr(
        svc,
        "apply_approval",
        lambda commitment, *, sim_run_id, **_: real_apply(
            commitment, sim_run_id=sim_run_id, connect=lambda: conn
        ),
    )
    out = svc.record_decision(
        "REQ-1",
        DecisionIn(decision="APPROVE", scenario_label="보수", decided_by="lhs"),
    )
    transition.reset()
    return out, conn


def test_승인이_실행_행의_축을_원장까지_흘린다(monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ `sim_run_id_for` 가 요구한 계약이 이것이다 — *"마스터 실행 이력이
    `sim_run_id` 를 싣도록 계약을 세우고 이 함수가 그것을 읽어야 한다."*
    """
    out, conn = _승인한다(monkeypatch, 실행행_축=실행축)

    assert out.transition is not None and out.transition.status == "APPLIED", out.transition
    매입 = [params for text, params in conn.log if "INSERT INTO" in text and "purchases" in text]
    assert 매입, "매입 원장이 안 나갔다"
    assert 실행축 in 매입[0], f"승인이 실행 행의 축을 안 흘렸다: {매입[0]}"
    assert BURN_IN_SIM_RUN_ID not in 매입[0], "승인이 중간에서 상수로 바꿨다"


def test_실행_행에_축이_없으면_번인으로_안_떨어진다(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 **메우지 않는다.** 상수로 채우면 축이 안 실린 옛 실행의 승인이 조용히 번인
    장부에 앉고, 재무는 자기 축을 읽으므로 **채무와 원장이 서로 다른 실행에 앉는다.**

    ★ **그래도 결정은 산다.** `apply_approval` 이 예외를 값으로 바꾸므로 사람이
      승인한 사실은 남고, 무엇이 막았는지는 `reason` 이 든다.
    """
    out, conn = _승인한다(monkeypatch, 실행행_축=None)

    assert out.transition is not None and out.transition.status == "FAILED"
    assert "sim_run_id" in out.transition.reason
    assert not [text for text, _ in conn.log if "INSERT INTO" in text], "축도 없이 원장을 썼다"


# ── ④ 새 실행을 만드는 자리 ────────────────────────────────────────────


_설정 = {"backfill": {"rule": "AUTO_APPROVE_CONSERVATIVE"}, "seed": 7}


def _만든다(**over: Any) -> tuple[_가짜커넥션, str]:
    conn = _가짜커넥션()
    인자: dict[str, Any] = {
        "sim_run_id": 실행축,
        "company_persona_id": "CP-HAETDEUL-001",
        "run_type": "WALK",
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 9, 9),
        "as_of": date(2026, 1, 1),
        "status": "PLANNED",
        "financing_mode": "LOAN_BASELINE",
        "config_json": _설정,
    }
    인자.update(over)
    return conn, create_sim_run(conn, **인자)


def test_실행_한_행을_만든다() -> None:
    """🔴 **한 행이다.** `sim_runs` 에 INSERT 하는 코드가 저장소에 한 곳도 없었다."""
    conn, 이름 = _만든다()

    문장 = [text for text, _ in conn.log]
    assert len(문장) == 1, f"한 행이 아니다: {문장}"
    assert "INSERT INTO" in 문장[0] and "sim_runs" in 문장[0]
    assert 이름 == 실행축


def test_config_json_을_그대로_적는다() -> None:
    """🔴 **해석하지 않는다.** 백필 규칙의 주인은 `backfill.py` 다 (`#518` · `#527`)."""
    conn, _ = _만든다()

    _, params = conn.log[0]
    실린것 = [one for one in params if hasattr(one, "obj")]
    assert 실린것, f"jsonb 로 실린 값이 없다: {params}"
    assert 실린것[0].obj == _설정, f"설정을 고쳐서 적었다: {실린것[0].obj}"


def test_persona_가_없으면_터진다() -> None:
    """🔴 **지어내지 않는다.** `company_persona_id` 는 FK 이고, 아무거나 집으면
    **남의 회사 설정 위에서 걸은 성적**이 남는다 — FK 는 그것을 안 막는다.
    """
    for 없는_값 in ("", "   "):
        with pytest.raises(ValueError, match="company_persona_id"):
            _만든다(company_persona_id=없는_값)


def test_축_이름을_짓되_되읽지_않는다() -> None:
    """★ 이름은 **사람이 읽는 것**이다. 종류를 알아야 하면 `run_type` 칸을 읽는다."""
    assert build_sim_run_id(run_type="WALK", month=date(2026, 1, 1)) == "SIM-WALK-202601"
    assert (
        build_sim_run_id(run_type="WALK", month=date(2026, 1, 31), suffix="재시도")
        == "SIM-WALK-202601-재시도"
    )


def test_실행_모듈이_이름을_파싱하지_않는다() -> None:
    """🔴 **파싱하는 순간 이름이 사실의 주인이 된다.**

    ⚠️ 구분자가 붙은 이름(`SIM-WALK-202601-재시도`) 하나로 판정이 갈리고, 그때는
      이름을 못 바꾼다.
    """
    원문 = _벗긴_원문(_MASTER / "sim_run.py")

    for 금지 in (".split(", ".rsplit(", ".partition(", ".startswith(", ".endswith(", "re."):
        assert 금지 not in 원문, f"실행 이름을 되읽는다: {금지}"


def test_기간이_거꾸로면_막는다() -> None:
    """★ **여기서 바로잡지 않는다.** 어느 쪽이 시작인지는 부르는 쪽이 안다."""
    with pytest.raises(ValueError, match="거꾸로"):
        _만든다(period_start=date(2026, 9, 9), period_end=date(2026, 1, 1))


def test_실행을_만드는_자리가_커밋하지_않는다() -> None:
    """🔴 **커밋은 부르는 쪽이 한다.** 여기서 커밋하면 실행 행만 먼저 확정되고,
    뒤이어 초기 상태 적재가 터졌을 때 **아무 장부도 없는 실행**이 남는다.
    """
    conn, _ = _만든다()

    assert conn.commits == 0


def test_실행을_만드는_자리가_커넥션을_스스로_안_연다() -> None:
    """🔴 **커넥션은 인자다.** 안에서 열면 이 검사들이 실 DB 로 나가고, 이 판이
    *"DB 에 한 행도 안 쓴다"* 를 못 지킨다.
    """
    원문 = _벗긴_원문(_MASTER / "sim_run.py")

    assert "get_connection" not in 원문, "실행을 만드는 자리가 커넥션을 스스로 연다"


def test_실행_시각을_모듈이_안_읽는다() -> None:
    """★ `started_at` · `finished_at` 도 인자다 — 여기서 시계를 읽으면 같은 실행을
    두 번 만들 때 값이 갈리고, 그 사실이 어디에도 안 남는다.
    """
    원문 = _벗긴_원문(_MASTER / "sim_run.py")

    for 금지 in ("now(", "utcnow(", "today(", "seoul_now"):
        assert 금지 not in 원문, f"실행을 만드는 자리가 시계를 읽는다: {금지}"


def test_시작과_끝_시각을_받은_그대로_싣는다() -> None:
    """★ 안 주면 `None` 이다 — **지어내지 않는다.**"""
    시작 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    conn, _ = _만든다(started_at=시작, finished_at=시작 + timedelta(days=1))

    _, params = conn.log[0]
    assert 시작 in params
    assert 시작 + timedelta(days=1) in params
