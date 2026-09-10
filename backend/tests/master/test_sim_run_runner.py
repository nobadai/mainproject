"""**새 실행을 여는 문** (2026-09-10 · `#531` · `#539` · `#545` 의 후속).

```text
① 지운다 (다시 열 때만)   reset_sim_run_ledger        ← --reset 을 줬을 때만
② 실행 한 행              create_sim_run
③ 시작 재무 상태          seed_opening_finance_state
④ 시작 물류 fixture       seed_opening_logistics_fixture   ← #551
                          ⋮
🔴 넷이 **한 트랜잭션** — 커밋은 이 문이 한 번
```

🔴 **DB 를 안 탄다.** 커넥션도 넷도 전부 대역이다 — 이 판은 문을 세우는 것까지고,
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

from app.master.backfill import BackfillRuleMissing
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
물류씨앗 = "LOG-WALK-202601-OPEN"
#: 🔴 **어휘의 주인은 물류다** — 검사가 값을 들되 이 문은 안 든다
#:   (`test_usage_scope_를_문에_안_박는다` 가 그것을 잰다).
쓰임 = "AGENT_MVP_DEMO"
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
    """넷이 **언제 어떤 인자로** 불렸는지를 한 목록에 모은다."""

    def __init__(self) -> None:
        self.부른것: list[str] = []
        self.create인자: dict[str, Any] = {}
        self.seed인자: dict[str, Any] = {}
        self.물류인자: dict[str, Any] = {}
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

    def 물류(self, conn: Any, **kw: Any) -> str:
        self.부른것.append("물류")
        self.물류인자 = kw
        self._터질까("물류")
        return str(kw["fixture_id"])


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
        "opening_fixture_id": 물류씨앗,
        "opening_usage_scope": 쓰임,
        "reset_fn": 기록.reset,
        "create_fn": 기록.create,
        "seed_fn": 기록.seed,
        "logistics_seed_fn": 기록.물류,
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
        ("--opening-fixture-id", 물류씨앗),
        ("--opening-usage-scope", 쓰임),
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
    """🔴 **`--reset` · `--note` · `--backfill-rules` 말고는 기본값이 하나도 없다.**

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
        if 이름 == "--backfill-rules":
            # 🔴 **기본이 「안 싣는다」다** (2026-09-11). 여기에 기본 규칙 파일을 두면
            #    아무도 안 정한 규칙으로 곡선이 서고, 승인까지 그 규칙으로 돈다.
            #
            # ⚠️ **필수로 안 만든다.** 승인 없이 여는 실행이 여전히 정상이고, 필수로
            #   만들면 규칙을 쓸 일 없는 실행까지 규칙을 지어내야 한다.
            assert action.default is None, "🔴 백필 규칙에 기본값이 있다"
            assert not action.required, "--backfill-rules 는 안 줘도 열려야 한다"
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
            opening_fixture_id=물류씨앗,
            opening_usage_scope=쓰임,
            reset_fn=기록.reset,
            create_fn=기록.create,
            seed_fn=기록.seed,
            logistics_seed_fn=기록.물류,
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


# ── 🔴 넷을 이 순서로 부른다 ────────────────────────────────────────────


def test_넷을_이_순서로_부른다() -> None:
    """🔴 **지우기 → 실행 행 → 시작 재무 상태 → 시작 물류 fixture.**

    ⚠️ 실행 행이 서야 시작 상태가 그 축을 가리킬 수 있다 —
      `finance_states.sim_run_id` 가 `sim_runs` 를 참조하는 FK 다.
    """
    _, 기록, _ = _연다(reset=True, 이미있다=True)
    assert 기록.부른것 == ["reset", "create", "seed", "물류"]


def test_reset_없이도_실행_행이_시작_상태보다_먼저다() -> None:
    _, 기록, _ = _연다()
    assert 기록.부른것 == ["create", "seed", "물류"]


def test_문이_물류_씨앗을_부른다() -> None:
    """🔴 **재무만 놓으면 새 실행에 물류 행이 한 행도 없다.**

    ★★ 물류는 자기 개장 여부를 `logistics_runtime_fixture` 에서 읽는다 — 한 행도
      없으면 마스터가 상한만큼 거슬러도 anchor 를 못 찾고 `REJECTED_GAP` 으로
      거절한다. 이 한 줄이 그 거절을 안 나게 하는 유일한 자리다.
    """
    _, 기록, _ = _연다()

    assert "물류" in 기록.부른것, "🔴 문이 물류 씨앗을 안 부른다"
    assert 기록.물류인자 != {}


def test_물류_씨앗에_새_실행과_baseline_을_함께_넘긴다() -> None:
    """🔴 **행이 앉는 곳은 새 실행, 사실을 가져오는 곳은 baseline 이다.**

    ⚠️ 둘을 같은 값으로 넘기면 새 실행에서 source 를 찾게 되고, 거기엔 한 행도
      없으니 늘 못 찾는다.
    """
    _, 기록, _ = _연다()

    assert 기록.물류인자["sim_run_id"] == 새실행
    assert 기록.물류인자["baseline_run_id"] == 출발실행
    assert 기록.물류인자["baseline_run_id"] == 계보.from_sim_run_id


def test_물류_씨앗의_이름을_새로_준다() -> None:
    """🔴 **identity 는 새로** — `--opening-state-id` 와 대칭이다."""
    _, 기록, opened = _연다()

    assert 기록.물류인자["fixture_id"] == 물류씨앗
    assert 기록.물류인자["fixture_id"] != 시작상태
    assert opened.opening_logistics_fixture_id == 물류씨앗


def test_재무_씨앗과_물류_씨앗이_같은_날짜를_쓴다() -> None:
    """★★ **갈릴 자리를 안 만든다.**

    ⚠️ 둘이 갈리면 두 파트의 anchor 가 갈리고, 그러면 이유 없이 한 파트만 며칠 더
      걷는다. 물류용 날짜 인자를 따로 만들지 않는 것이 그 결정이다.
    """
    _, 기록, _ = _연다(opening_state_date=date(2025, 12, 31))

    assert 기록.seed인자["state_date"] == date(2025, 12, 31)
    assert 기록.물류인자["as_of"] == 기록.seed인자["state_date"]


def test_물류용_날짜_인자를_따로_안_만든다() -> None:
    """★★ **날짜는 `--opening-state-date` 하나다.**

    ⚠️ 물류용 날짜를 따로 받으면 사람이 둘을 다르게 줄 수 있고, 그 순간 두 파트가
      다른 날에서 출발한다.
    """
    날짜인자 = [
        action.option_strings[0]
        for action in _parser()._actions
        if action.option_strings and "date" in action.option_strings[0]
    ]

    assert 날짜인자 == ["--opening-state-date"], f"날짜 인자가 여럿이다: {날짜인자}"


def test_usage_scope_를_문에_안_박는다() -> None:
    """🔴 **어휘의 주인은 물류다.**

    ★★ 마스터가 제 코드에 박으면 물류가 값을 바꾸는 날 말없이 갈린다 — 그때
      마스터가 놓은 씨앗을 물류가 못 읽고, 개장은 다시 거절한다. 물류 상수를
      import 해 오는 것도 같은 이유로 안 된다. **문에서 눈에 보이게 받는다.**
    """
    원문 = _NFC(ast.unparse(_벗긴_트리()))

    for 금지 in (쓰임, "USAGE_SCOPE", "LOGISTICS_POLICY_USAGE_SCOPE", "app.logistics"):
        assert 금지 not in 원문, f"물류 어휘를 문에 박았다: {금지}"

    받는인자 = [
        action.option_strings[0]
        for action in _parser()._actions
        if action.option_strings and action.option_strings[0] == "--opening-usage-scope"
    ]
    assert 받는인자 == ["--opening-usage-scope"], "🔴 눈에 보이게 안 받는다"


def test_문이_넘긴_usage_scope_가_받은_그_값이다() -> None:
    """🔴 **문이 받은 값을 그대로 넘긴다** — 중간에 제 값으로 바꾸지 않는다."""
    _, 기록, _ = _연다(opening_usage_scope="물류가_내일_바꿀_scope")

    assert 기록.물류인자["usage_scope"] == "물류가_내일_바꿀_scope"


# ── 🔴 한 트랜잭션 ──────────────────────────────────────────────────────


def test_다_되면_커밋을_한_번_부른다() -> None:
    """🔴 **커밋을 이 문이 한다.** 셋 다 커밋을 안 하고, 지금까지 부르는 쪽이 없었다."""
    conn, _, _ = _연다()
    assert conn.commits == 1
    assert conn.rollbacks == 0


@pytest.mark.parametrize("터진곳", ["reset", "create", "seed", "물류"])
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
            opening_fixture_id=물류씨앗,
            opening_usage_scope=쓰임,
            reset=True,
            reset_fn=기록.reset,
            create_fn=기록.create,
            seed_fn=기록.seed,
            logistics_seed_fn=기록.물류,
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
        ("logistics_seed_fn", "seed_opening_logistics_fixture"),
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
                opening_logistics_fixture_id=물류씨앗,
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
            opening_logistics_fixture_id=물류씨앗,
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
            opening_logistics_fixture_id=물류씨앗,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 6, 29),
            ledger_reset=LedgerReset(sim_run_id=새실행, order=("sales",), deleted={"sales": 9}),
        )
    )
    assert _NFC("안 지웠다") in _NFC(안지움)
    assert _NFC("지웠다 (--reset)") in _NFC(지움)


# ── 🔴 백필 규칙을 문에서 받아 그대로 싣는다 (2026-09-11) ───────────────
#
# 🔴 **어휘의 주인은 부서다.** 규칙 이름도 라벨도 축 이름도 이 문은 모른다 —
#    `--opening-usage-scope` 를 문에 안 박은 것과 **같은 이유·같은 모양**이다.
#
# ⚠️ 검사는 값을 들되 문은 안 든다. 아래 잠금이 그것을 잰다.

#: 검사용 규칙 한 벌. **이 값이 문 코드에 있으면 안 된다.**
규칙 = {
    "procurement": {"rule": "ALWAYS_BASE", "scenario_label": "기본"},
    "sales": {"rule": "ALWAYS_FIXED_TYPE", "scenario_type": "CONSERVATIVE"},
}


def test_안_주면_backfill_칸이_아예_안_선다() -> None:
    """★ *"규칙을 안 정했다"* 와 *"규칙을 비워 뒀다"* 를 같은 값으로 안 적는다.

    🔴 빈 칸을 만들어 두면 걷기가 그 둘을 못 가르고, `--auto-approve` 가 **막지
      못한 채** 0건으로 걷는다.
    """
    _, 기록, 열림 = _연다()

    assert "backfill" not in 기록.create인자["config_json"]
    assert 열림.backfill_rules is None


def test_주면_받은_것을_그대로_config_json_에_싣는다() -> None:
    """🔴 **한 글자도 고쳐 적지 않는다.** 고치면 부서의 계약이 이 문에서 갈린다."""
    _, 기록, 열림 = _연다(backfill_rules=규칙)

    설정 = 기록.create인자["config_json"]
    assert 설정["backfill"] == 규칙
    assert 설정["baseline"] == {"from_sim_run_id": 출발실행, "finance_state_id": 출발상태}
    assert 열림.backfill_rules == 규칙


def test_규칙을_실어도_계보를_덮지_않는다() -> None:
    """🔴 **두 칸이 한 설정에 나란히 앉는다.** 한쪽이 다른 쪽을 밀어내면 안 된다."""
    _, 기록, _ = _연다(backfill_rules=규칙)

    assert sorted(기록.create인자["config_json"]) == ["backfill", "baseline"]


def test_모르는_규칙이면_여는_자리에서_터진다() -> None:
    """⚠️ 179일을 걷고 나서 *"모르는 규칙이었다"* 를 알면 늦다.

    🔴 **판정을 여기서 베끼지 않는다.** `backfill.read_rules` 를 불러서 막는다 —
      두 곳에서 막으면 언젠가 한쪽만 고쳐진다.
    """
    with pytest.raises(BackfillRuleMissing):
        _연다(backfill_rules={"procurement": {"rule": "아무거나", "scenario_label": "기본"}})


def test_모르는_규칙이면_한_행도_안_세운다() -> None:
    """🔴 **막았으면 아무것도 안 선다.** 반쪽 실행을 남기지 않는다."""
    conn = _대역커넥션()
    기록 = _순서기록()
    with pytest.raises(BackfillRuleMissing):
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
            opening_fixture_id=물류씨앗,
            opening_usage_scope=쓰임,
            backfill_rules={"모르는칸": {}},
            reset_fn=기록.reset,
            create_fn=기록.create,
            seed_fn=기록.seed,
            logistics_seed_fn=기록.물류,
        )

    assert 기록.부른것 == []
    assert conn.commits == 0


@pytest.mark.parametrize(
    "어휘",
    ["ALWAYS_BASE", "ALWAYS_FIXED_TYPE", "기본", "보수", "공격", "CONSERVATIVE", "AGGRESSIVE"],
)
def test_규칙_어휘를_문에_안_박는다(어휘: str) -> None:
    """🔴 **규칙 이름도 라벨도 축 이름도 이 문의 것이 아니다.**

    ★ `test_usage_scope_를_문에_안_박는다` 와 같은 모양이다 — 어휘의 주인이
      밖에 있으면 잠금도 그 자리에 선다.

    🔴 **글자 조각이 아니라 문자열 값을 잰다.** 이 문의 도움말에 *"기본값 없음"* 이
      여러 번 나오는데, 조각으로 재면 그 문장이 라벨 `기본` 으로 읽혀 **잠금이 코드가
      아니라 도움말 문장을 재게 된다.**
    """
    값들 = {
        _NFC(node.value)
        for node in ast.walk(_벗긴_트리())
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert _NFC(어휘) not in 값들, f"문 코드에 규칙 어휘 '{어휘}' 가 박혀 있다"


def test_어휘_잠금이_실제로_잡는다() -> None:
    """🟢 **위 검사가 아무것도 안 재고 초록이 되는 것을 막는다.**

    ★ 라벨이 **문자열 값**이면 잡고, 긴 문장의 **조각**이면 안 잡는다.
    """
    박은것 = {
        _NFC(node.value)
        for node in ast.walk(ast.parse('label = "기본"\nhelp = "🔴 기본값 없음"'))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    설명만 = {
        _NFC(node.value)
        for node in ast.walk(ast.parse('help = "🔴 기본값 없음"'))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert _NFC("기본") in 박은것
    assert _NFC("기본") not in 설명만


def test_요약이_규칙을_실었는지_말한다() -> None:
    """🔴 **안 실은 실행은 `--auto-approve` 로 못 걷는다.** 그 사실이 눈에 보여야 한다."""
    공통: dict[str, Any] = {
        "sim_run_id": 새실행,
        "financing_mode": 새조달,
        "baseline": 계보,
        "opening_finance_state_id": 시작상태,
        "opening_logistics_fixture_id": 물류씨앗,
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 6, 29),
        "ledger_reset": None,
    }
    안실음 = _NFC(format_summary(SimRunOpened(**공통)))
    실음 = _NFC(format_summary(SimRunOpened(**공통, backfill_rules=규칙)))

    assert _NFC("안 실었다") in 안실음
    assert _NFC("실었다") in 실음
    # ★ **다음 명령 줄만 본다.** 사실을 말하는 줄에도 인자 이름이 나오므로,
    #   사람이 실제로 붙여 넣는 마지막 줄에서 재야 뜻이 맞는다.
    assert "--auto-approve" not in 안실음.splitlines()[-1], (
        "규칙도 없는데 승인 인자를 적어 주면 사람이 그대로 붙여 넣고 걷기 첫 줄에서 막힌다"
    )
    assert "--auto-approve" in 실음.splitlines()[-1], (
        "규칙을 실었으면 걷는 명령에 그 인자가 보여야 한다"
    )
