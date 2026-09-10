"""**새 실행을 여는 문** (2026-09-10 · `#531` · `#539` · `#545` 의 후속).

```text
① 지운다 (다시 열 때만)   reset_sim_run_ledger        ← --reset 을 줬을 때만
② 실행 한 행              create_sim_run
③ 시작 재무 상태          seed_opening_finance_state
                          ⋮
🔴 셋이 **한 트랜잭션** — 커밋은 이 문이 한 번
```

🔴 **DB 를 안 탄다.** 커넥션도 셋도 전부 대역이다 — 이 판은 문을 세우는 것까지고,
   실제로 열거나 걷거나 행을 쓰거나 지우는 것은 이 판이 하지 않는다.

⚠️ **한글 문장을 잴 때는 `NFC` 로 맞춘다.** 조합형/분해형이 섞이면 같은 글자가
  안 같아지고, 그때 검사는 코드가 아니라 인코딩을 재게 된다.
"""

from __future__ import annotations

import ast
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Self

import pytest

from app.master.sim_run_open import BaselineLineage, LedgerReset
from app.master.sim_run_runner import (
    SimRunOpened,
    _parser,
    format_summary,
    open_sim_run,
)

_문 = Path(__file__).resolve().parents[2] / "app" / "master" / "sim_run_runner.py"

새실행 = "SIM-WALK-202601"
새조달 = "LOAN_BASELINE"
출발실행 = "SIM-BURNIN-202512"
출발상태 = "FIN-DAY30-LOAN"
시작상태 = "FIN-WALK-202601-OPEN"
계보 = BaselineLineage(from_sim_run_id=출발실행, finance_state_id=출발상태)

#: `--reset` 을 안 줄 때만 쓰는 인자 넷. 🔴 **하나라도 없으면 터져야 한다.**
필수넷 = ("--sim-run-id", "--financing-mode", "--baseline-run-id", "--baseline-state-id")


def _NFC(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _벗긴_트리() -> ast.Module:
    """주석과 docstring 을 걷어낸 트리.

    🔴 **왜 걷어내나.** 이 문은 근거를 길게 적는다 — 금지어가 **설명 문장 안에**
      있어서 원문 잠금이 늘 실패하면, 그 검사는 코드가 아니라 문장을 재는 것이 된다.
    """
    tree = ast.parse(_문.read_text(encoding="utf-8"))
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
    return tree


# ── 대역 ───────────────────────────────────────────────────────────────


class _대역커서:
    """존재 확인 질의에만 답한다."""

    def __init__(self, 대장: _대역커넥션) -> None:
        self.대장 = 대장

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> None:
        문장 = query.as_string(None) if hasattr(query, "as_string") else str(query)
        self.대장.log.append((문장, list(params or [])))

    def fetchone(self) -> dict[str, Any] | None:
        return {"present": 1} if self.대장.이미있다 else None


class _대역커넥션:
    def __init__(self, *, 이미있다: bool = False) -> None:
        self.log: list[tuple[str, list[Any]]] = []
        self.이미있다 = 이미있다
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self) -> _대역커서:
        return _대역커서(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


class _순서기록:
    """셋이 **언제 어떤 인자로** 불렸는지를 한 목록에 모은다."""

    def __init__(self) -> None:
        self.부른것: list[str] = []
        self.create인자: dict[str, Any] = {}
        self.seed인자: dict[str, Any] = {}
        self.reset인자: dict[str, Any] = {}
        self.터뜨릴것: str | None = None

    def _터질까(self, 이름: str) -> None:
        if self.터뜨릴것 == 이름:
            raise RuntimeError(f"{이름} 에서 터졌다")

    def reset(self, conn: Any, **kw: Any) -> LedgerReset:
        self.부른것.append("reset")
        self.reset인자 = kw
        self._터질까("reset")
        return LedgerReset(sim_run_id=kw["sim_run_id"], order=("sales",), deleted={"sales": 9})

    def create(self, conn: Any, **kw: Any) -> str:
        self.부른것.append("create")
        self.create인자 = kw
        self._터질까("create")
        return str(kw["sim_run_id"])

    def seed(self, conn: Any, **kw: Any) -> str:
        self.부른것.append("seed")
        self.seed인자 = kw
        self._터질까("seed")
        return str(kw["finance_state_id"])


def _연다(**over: Any) -> tuple[_대역커넥션, _순서기록, Any]:
    """문을 한 번 부른다. 터지면 예외를 그대로 올린다."""
    conn = _대역커넥션(이미있다=over.pop("이미있다", False))
    기록 = _순서기록()
    기록.터뜨릴것 = over.pop("터뜨릴것", None)
    인자: dict[str, Any] = {
        "sim_run_id": 새실행,
        "company_persona_id": "PERSONA-HAETDEUL",
        "run_type": "WALK",
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 6, 29),
        "as_of": date(2026, 1, 1),
        "status": "RUNNING",
        "financing_mode": 새조달,
        "baseline": 계보,
        "opening_finance_state_id": 시작상태,
        "opening_state_date": date(2026, 1, 1),
        "opening_state_type": "OPENING",
        "reset_fn": 기록.reset,
        "create_fn": 기록.create,
        "seed_fn": 기록.seed,
    }
    인자.update(over)
    return conn, 기록, open_sim_run(conn, **인자)


def _인자줄(빼기: str | None = None) -> list[str]:
    """CLI 인자 한 벌. `빼기` 가 주어지면 그 하나만 뺀다."""
    쌍 = [
        ("--sim-run-id", 새실행),
        ("--company-persona-id", "PERSONA-HAETDEUL"),
        ("--run-type", "WALK"),
        ("--period-start", "2026-01-01"),
        ("--period-end", "2026-06-29"),
        ("--as-of", "2026-01-01"),
        ("--status", "RUNNING"),
        ("--financing-mode", 새조달),
        ("--baseline-run-id", 출발실행),
        ("--baseline-state-id", 출발상태),
        ("--opening-state-id", 시작상태),
        ("--opening-state-date", "2026-01-01"),
        ("--opening-state-type", "OPENING"),
    ]
    줄: list[str] = []
    for 이름, 값 in 쌍:
        if 이름 == 빼기:
            continue
        줄 += [이름, 값]
    return 줄


# ── 🔴 인자에 기본값이 없다 ─────────────────────────────────────────────


@pytest.mark.parametrize("빠진것", 필수넷)
def test_넷_중_하나라도_없으면_터진다(빠진것: str) -> None:
    """🔴 재무가 청한 넷. **하나라도 없으면 문이 안 열린다.**

    ★★ *"한 값을 보고 다른 값을 추측하지 않는다"* — 없는 인자를 다른 인자에서
      지어내면 그 순간 추측이 규칙이 된다.
    """
    with pytest.raises(SystemExit):
        _parser().parse_args(_인자줄(빼기=빠진것))


def test_인자에_기본값이_없다() -> None:
    """🔴 **`--reset` 과 `--note` 말고는 기본값이 하나도 없다.**

    ⚠️ 기본값을 두면 **그 값이 곧 업무 규칙이 된다** — 아무도 정한 적이 없는데
      실행마다 그 출발점이 찍히고, 나중에 *"왜 저 baseline 인가"* 에 답할 사람이 없다.

    ★ `required` 와 `default` 를 **둘 다** 잰다. 하나만 재면 `required=True` 를 걷고
      기본값을 심는 뮤턴트가 살아남는다.
    """
    for action in _parser()._actions:
        if not action.option_strings or "--help" in action.option_strings:
            continue
        이름 = action.option_strings[0]
        if 이름 == "--reset":
            assert action.default is False, "🔴 --reset 의 기본은 「안 지운다」여야 한다"
            assert not action.required, "--reset 은 안 줘도 돌아야 한다"
            continue
        if 이름 == "--note":
            assert action.default is None
            continue
        assert action.required is True, f"{이름} 이 필수가 아니다"
        assert action.default is None, f"{이름} 에 기본값이 있다: {action.default!r}"


# ── 🔴 다시 여는 것은 따로 밝혀야 한다 ──────────────────────────────────


def test_실행이_이미_있는데_reset_이_없으면_터진다() -> None:
    """🔴 **이미 있는 실행 위에 조용히 앉지 않는다.**"""
    with pytest.raises(ValueError, match="이미 있다"):
        _연다(이미있다=True)


def test_이미_있는데_reset_이_없으면_셋을_하나도_안_부른다() -> None:
    """🔴 **막힌 자리에서 아무것도 안 한다.** 커밋도 안 한다."""
    conn = _대역커넥션(이미있다=True)
    기록 = _순서기록()
    with pytest.raises(ValueError):
        open_sim_run(
            conn,
            sim_run_id=새실행,
            company_persona_id="PERSONA-HAETDEUL",
            run_type="WALK",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 6, 29),
            as_of=date(2026, 1, 1),
            status="RUNNING",
            financing_mode=새조달,
            baseline=계보,
            opening_finance_state_id=시작상태,
            opening_state_date=date(2026, 1, 1),
            opening_state_type="OPENING",
            reset_fn=기록.reset,
            create_fn=기록.create,
            seed_fn=기록.seed,
        )
    assert 기록.부른것 == []
    assert conn.commits == 0


def test_reset_이_없으면_지우는_함수를_한_번도_안_부른다() -> None:
    """🔴 **기본이 「안 지운다」다.** 지우는 것은 되돌릴 수 없다."""
    _, 기록, _ = _연다()
    assert "reset" not in 기록.부른것
    assert 기록.reset인자 == {}


def test_reset_을_주면_지우고_다시_연다() -> None:
    """🔴 **`--reset` 을 줬을 때만 지운다.**"""
    _, 기록, opened = _연다(reset=True, 이미있다=True)
    assert 기록.부른것[0] == "reset"
    assert 기록.reset인자 == {"sim_run_id": 새실행}
    assert opened.ledger_reset is not None
    assert opened.ledger_reset.total_deleted == 9


def test_안_지웠을_때와_0행_지웠을_때가_다른_값이다() -> None:
    """🔴 **`None` 은 「안 지웠다」다.** 0 행을 지운 것과 같은 값으로 안 적는다."""
    _, _, 안지움 = _연다()
    assert 안지움.ledger_reset is None


# ── 🔴 셋을 이 순서로 부른다 ────────────────────────────────────────────


def test_셋을_이_순서로_부른다() -> None:
    """🔴 **지우기 → 실행 행 → 시작 상태.**

    ⚠️ 실행 행이 서야 시작 상태가 그 축을 가리킬 수 있다 —
      `finance_states.sim_run_id` 가 `sim_runs` 를 참조하는 FK 다.
    """
    _, 기록, _ = _연다(reset=True, 이미있다=True)
    assert 기록.부른것 == ["reset", "create", "seed"]


def test_reset_없이도_실행_행이_시작_상태보다_먼저다() -> None:
    _, 기록, _ = _연다()
    assert 기록.부른것 == ["create", "seed"]


# ── 🔴 한 트랜잭션 ──────────────────────────────────────────────────────


def test_다_되면_커밋을_한_번_부른다() -> None:
    """🔴 **커밋을 이 문이 한다.** 셋 다 커밋을 안 하고, 지금까지 부르는 쪽이 없었다."""
    conn, _, _ = _연다()
    assert conn.commits == 1
    assert conn.rollbacks == 0


@pytest.mark.parametrize("터진곳", ["reset", "create", "seed"])
def test_중간에_터지면_롤백하고_커밋을_안_부른다(터진곳: str) -> None:
    """🔴 **반쪽 실행을 남기지 않는다.**

    ⚠️ 실행 행만 서고 시작 상태가 없으면 첫날 마감이 baseline 을 못 찾는다.
      장부만 지워지고 시작 상태 적재가 터지면 **출발점 없는 빈 실행**이 남는다.
    """
    conn = _대역커넥션(이미있다=True)
    기록 = _순서기록()
    기록.터뜨릴것 = 터진곳
    with pytest.raises(RuntimeError, match="터졌다"):
        open_sim_run(
            conn,
            sim_run_id=새실행,
            company_persona_id="PERSONA-HAETDEUL",
            run_type="WALK",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 6, 29),
            as_of=date(2026, 1, 1),
            status="RUNNING",
            financing_mode=새조달,
            baseline=계보,
            opening_finance_state_id=시작상태,
            opening_state_date=date(2026, 1, 1),
            opening_state_type="OPENING",
            reset=True,
            reset_fn=기록.reset,
            create_fn=기록.create,
            seed_fn=기록.seed,
        )
    assert conn.commits == 0, "🔴 터졌는데 커밋했다 — 반쪽 실행이 남는다"
    assert conn.rollbacks == 1


def test_커밋이_한_자리에만_있다() -> None:
    """🔴 **커밋을 부르는 자리가 하나다.** 둘이면 한쪽만 고쳐지는 날이 온다."""
    커밋들 = [
        node
        for node in ast.walk(_벗긴_트리())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
    ]
    assert len(커밋들) == 1, f"커밋 자리가 {len(커밋들)} 곳이다"


# ── 🟢 config_json 에 lineage 만 ────────────────────────────────────────


def test_config_json_에_lineage_만_들어간다() -> None:
    """🟢 **가리키기만 한다.** 잔액도 한도도 안 담는다.

    ⚠️ 숫자를 설정에 복사해 두면 원본 행과 복사본이 **두 진실**이 되고, 원본이
      고쳐지는 날 갈린다 — 그때 어느 쪽이 맞는지 답할 방법이 없다.
    """
    _, 기록, _ = _연다()
    assert 기록.create인자["config_json"] == {
        "baseline": {"from_sim_run_id": 출발실행, "finance_state_id": 출발상태}
    }


def test_조달_방식과_baseline_을_함께_넘긴다() -> None:
    """🔴 **한쪽을 보고 다른 쪽을 고르지 않는다** (재무 청함)."""
    _, 기록, _ = _연다()
    assert 기록.create인자["financing_mode"] == 새조달
    assert 기록.seed인자["financing_mode"] == 새조달
    assert 기록.seed인자["baseline"] is 계보


def test_시작_상태의_이름을_출발점에서_물려받지_않는다() -> None:
    """🔴 **identity 는 새로.** source 것을 물려받으면 남의 실행 id 가 들어온다."""
    _, 기록, _ = _연다()
    assert 기록.seed인자["finance_state_id"] == 시작상태
    assert 기록.seed인자["finance_state_id"] != 출발상태
    assert 기록.seed인자["sim_run_id"] == 새실행


# ── 🟢 원문 잠금 ────────────────────────────────────────────────────────


def test_이름을_파싱하지_않는다() -> None:
    """🟢 **이름은 사람이 읽는 것이다.**

    ⚠️ 파싱하는 순간 `SIM-WALK-202601-재시도` 같은 구분자 하나로 판정이 갈리고,
      그때는 이름을 못 바꾼다. 종류를 알아야 하면 `run_type` **칸**을 읽는다.
    """
    원문 = _NFC(ast.unparse(_벗긴_트리()))
    for 금지 in ("split(", "startswith(", "endswith(", "partition(", "build_sim_run_id"):
        assert 금지 not in 원문, f"이름을 파싱한다: {금지}"


def test_걷기를_안_부르고_import_도_안_한다() -> None:
    """🟡 **이 문은 여는 것까지다.**

    ★ 둘을 한 문에 묶으면 *"열었는데 안 걸었다"* 와 *"열고 걸었다"* 를 사람이
      못 고른다.
    """
    tree = _벗긴_트리()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert all(alias.name != "walk" for alias in node.names), "walk 를 import 했다"
        if isinstance(node, ast.Call):
            이름 = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert 이름 != "walk", "이 문이 walk 를 부른다"


def test_정합성_검사와_번인_가드를_다시_만들지_않는다() -> None:
    """🔴 **두 곳에서 막으면 언젠가 한쪽만 고쳐진다.**

    정합성 셋은 `seed_opening_finance_state` 가, 번인 거부는 `reset_sim_run_ledger`
    가 이미 본다.
    """
    원문 = _NFC(ast.unparse(_벗긴_트리()))
    for 금지 in ("BURN_IN_SIM_RUN_ID", "information_schema", "pg_constraint"):
        assert 금지 not in 원문, f"이미 있는 판단을 다시 만든다: {금지}"


def test_대역을_운영_경로에_안_심는다() -> None:
    """🔴 **주입 자리의 기본값이 진짜 셋이다.**

    ⚠️ 기본이 대역이면 운영 경로가 조용히 아무것도 안 하고, 그때 *"열었다"* 는
      말만 남는다. 대역은 검사에서만 넣는다.
    """
    문 = next(
        node
        for node in ast.walk(_벗긴_트리())
        if isinstance(node, ast.FunctionDef) and node.name == "open_sim_run"
    )
    이름들 = [one.arg for one in 문.args.kwonlyargs]
    기본값 = dict(zip(이름들, 문.args.kw_defaults, strict=True))
    for 자리, 본체 in (
        ("reset_fn", "reset_sim_run_ledger"),
        ("create_fn", "create_sim_run"),
        ("seed_fn", "seed_opening_finance_state"),
    ):
        기본 = 기본값[자리]
        assert isinstance(기본, ast.Name) and 기본.id == 본체, f"{자리} 의 기본이 {본체} 가 아니다"


# ── 🟡 다음 명령을 적어 준다 ────────────────────────────────────────────


def test_요약이_다음에_부를_명령을_적어_준다() -> None:
    """🟡 **열고 나서 무엇을 부르면 걷는지.** 사람이 두 번째 명령을 찾아 헤매지 않게."""
    요약 = _NFC(
        format_summary(
            SimRunOpened(
                sim_run_id=새실행,
                financing_mode=새조달,
                baseline=계보,
                opening_finance_state_id=시작상태,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 6, 29),
                ledger_reset=None,
            )
        )
    )
    assert "app.master.backtest_runner" in 요약
    assert f"--sim-run-id {새실행}" in 요약
    assert "--start 2026-01-01" in 요약
    assert "--end 2026-06-29" in 요약
    assert _NFC("걷지는 않았다") in 요약


def test_요약이_안_지웠다는_사실을_적는다() -> None:
    """🔴 **조용히 지나가지 않는다.** 지웠는지 안 지웠는지가 눈에 보여야 한다."""
    안지움 = format_summary(
        SimRunOpened(
            sim_run_id=새실행,
            financing_mode=새조달,
            baseline=계보,
            opening_finance_state_id=시작상태,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 6, 29),
            ledger_reset=None,
        )
    )
    지움 = format_summary(
        SimRunOpened(
            sim_run_id=새실행,
            financing_mode=새조달,
            baseline=계보,
            opening_finance_state_id=시작상태,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 6, 29),
            ledger_reset=LedgerReset(sim_run_id=새실행, order=("sales",), deleted={"sales": 9}),
        )
    )
    assert _NFC("안 지웠다") in _NFC(안지움)
    assert _NFC("지웠다 (--reset)") in _NFC(지움)
