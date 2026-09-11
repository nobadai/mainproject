"""**자동 걷기의 판매 요청이 상업 조건을 싣는다 — 규칙 파일이 말할 때만** (2026-09-11).

★★ **왜 이 판이 있나.** 걷기가 판매 판정까지 갔는데 **재무 검증에서 멈췄다.**

```text
판매코드   SL6_VALIDATION_UNRESOLVED
재무 응답  status "INPUT_INCOMPLETE" · reason_codes ["SALES_INPUT_INCOMPLETE"]
missing_fields  partner_id · unit_price_krw · reported_sales_amount_krw
                payment_terms_type · source_ref
```

**수량 하나만 물류에서 왔고 상업 조건은 아무도 안 정했다.** 자동 걷기에는 사람이 없다.

```text
규칙 파일에 sales_terms 가 있으면   → 그 값이 판매 요청에 실린다
없으면                              → 종전 그대로. 아무것도 안 실린다
요청이 이미 값을 들고 있으면         → 🟢 사람이 준 값이 이긴다
```

🔴 **값이 코드에 없다.** 거래처 id 도 지급조건 이름도 일수도 **규칙 파일이 말한다** —
  코드에 박으면 조건을 바꾸는 날 diff 가 아니라 배포가 된다. 그 사실을 원문으로
  잠근다.

⚠️ **DB 를 안 탄다.** 규칙도 ML 시세도 전부 대역이다. 이 판이 잠그는 것은 **무엇이
  실리고 무엇이 안 실리는가**이지 DB 가 무엇을 들고 있는가가 아니다.

⚠️ **한글 문장을 잴 때는 `NFC` 로 맞춘다.** 조합형/분해형이 섞이면 같은 글자가
  안 같아지고, 그때 검사는 코드가 아니라 인코딩을 재게 된다.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.master import backtest_runner, scheduler
from app.master import sales_terms as 조건모듈
from app.master.backfill import (
    FIXED_UNIT_PRICE,
    ML_CURRENT_PRICE,
    BackfillRuleMissing,
    BackfillRules,
    SalesTermsRule,
    read_rules,
)
from app.master.backtest_runner import walk
from app.master.clock import SEOUL
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.inputs import SourcedInput
from app.master.sales_terms import apply_sales_terms, read_run_sales_terms, rules_source_ref
from app.master.scheduler import (
    DayRunOutcome,
    ScheduledAction,
    plan_next_action,
    run_scheduled_day,
)
from app.master.schemas import SalesRunRequest

AS_OF = date(2026, 9, 8)
ITEMS = ("배추",)
실행 = "SIM-WALK-202601"

# ── 규칙 파일의 값. 🔴 **검사에만 있다 — 운영 코드에 같은 글자가 없다.** ──────
#
# ★ 실측에서 온 값이다 (`partners` 1행 · `sales_collection_days` 30 ·
#   `SalesPaymentTermsType` 둘 중 하나).
거래처 = "KIMCHI_FACTORY_001"
지급조건 = "SINGLE"
지급일수 = 30
고정단가 = Decimal(2000)
시세 = 1650

조건규칙 = SalesTermsRule(
    partner_id=거래처,
    payment_terms_type=지급조건,
    payment_days=지급일수,
    unit_price_source=FIXED_UNIT_PRICE,
    unit_price_krw=고정단가,
)

_조건 = Path(조건모듈.__file__)
_스케줄러 = Path(scheduler.__file__)
_걷기 = Path(backtest_runner.__file__)


def _NFC(text: str) -> str:
    return unicodedata.normalize("NFC", text)


# ── 대역 ────────────────────────────────────────────────────────────────


@dataclass
class _Out:
    status: str
    reason: str = ""
    end_code: str | None = None


class _단계:
    def __init__(self, out: object = None) -> None:
        self.out = out

    def __call__(self, *args: Any, **kwargs: Any):
        return self.out


class _판매:
    """`run_sales` 대역. **받은 요청을 그대로 모은다.**"""

    def __init__(self) -> None:
        self.requests: list[SalesRunRequest] = []

    def __call__(self, request, verifier=None, **kwargs: Any):
        self.requests.append(request)
        return _Out(status="RAN", end_code="SL1_PRESENTED")


class _달력:
    def is_market_open(self, as_of: date) -> bool:
        return True


def _준비(as_of: date = AS_OF) -> DayForecastReadiness:
    return DayForecastReadiness(
        as_of=as_of,
        readiness="ALL_READY",  # type: ignore[arg-type]
        items=tuple(
            ItemForecastGate(item=item, as_of=as_of, readiness="READY", grade="MEASURED")  # type: ignore[arg-type]
            for item in ITEMS
        ),
    )


def _계획(as_of: date = AS_OF) -> ScheduledAction:
    return plan_next_action(
        now=datetime(as_of.year, as_of.month, as_of.day, 9, 30, tzinfo=SEOUL),
        as_of=as_of,
        calendar=_달력(),
        gate_result=_준비(as_of),
    )


def _하루(**over: Any) -> _판매:
    """하루를 걷고 **판매가 받은 요청**을 돌려준다."""
    판매 = _판매()
    인자: dict[str, Any] = {
        "open_day_fn": _단계(_Out("OPENED")),
        "receive_fn": _단계(_Out("RECEIVED")),
        "issue_fn": _단계(_Out("ISSUED")),
        "collect_fn": _단계(_Out("COLLECTED")),
        "procure_fn": _단계(_Out("RAN")),
        "sales_fn": 판매,
        "outbound_fn": _단계(_Out("NOTHING_DUE")),
        "close_fn": _단계(_Out("CLOSED")),
        "sim_run_id": 실행,
        "items": ITEMS,
    }
    인자.update(over)
    run_scheduled_day(_계획(), **인자)  # type: ignore[arg-type]
    return 판매


def _요청(**over: Any) -> SalesRunRequest:
    인자: dict[str, Any] = {
        "as_of": AS_OF,
        "policy_version": "v1.3",
        "business_mode": "SPOT_SALES",
        "item": "배추",
        "sim_run_id": 실행,
    }
    인자.update(over)
    return SalesRunRequest(**인자)


def _시세(payload: dict[str, Any] | None) -> Any:
    def forecast_fn(item: str, as_of: date) -> SourcedInput:
        if payload is None:
            return SourcedInput("forecast", None, "MISSING", "-", "없다")
        return SourcedInput("forecast", payload, "MEASURED", "ml", "")

    return forecast_fn


def _코드만(source: str) -> str:
    """docstring 과 `#` 주석을 걷어낸 **실제로 실행되는 코드**.

    ⚠️ 원문을 그대로 뒤지면 *"거래처 id 를 코드에 안 박는다"* 고 **설명하는 문장**이
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


# ══════════════════════════════════════════════════════════════════════
#  ① 🔴 **규칙이 있으면 그대로 실린다**
# ══════════════════════════════════════════════════════════════════════


def test_규칙이_말한_조건이_판매_요청에_그대로_실린다() -> None:
    """🔴 **재무가 요구한 넷이 여기서 선다.**

    ★ 값을 고치지도 반올림하지도 않는다 — 규칙 파일에 적힌 그대로다.
    """
    판매 = _하루(sales_terms=조건규칙)

    보낸 = 판매.requests[0]
    assert 보낸.partner_id == 거래처
    assert 보낸.preferred_payment_terms_type == 지급조건
    assert 보낸.preferred_payment_days == 지급일수
    assert 보낸.preferred_unit_price_krw == 고정단가


def test_규칙이_없으면_아무것도_안_싣는다() -> None:
    """🔴 **종전 동작 그대로다.**

    ⚠️ **코드에 기본값을 두어 「늘 실리게」 하지 않는다.** 그러면 규칙을 안 적은
      사람도 **모르는 거래처에 팔게 된다.**
    """
    판매 = _하루()

    보낸 = 판매.requests[0]
    assert 보낸.partner_id is None
    assert 보낸.preferred_payment_terms_type is None
    assert 보낸.preferred_payment_days is None
    assert 보낸.preferred_unit_price_krw is None
    assert 보낸.source_ref is None


def test_하루는_설정을_제_손으로_안_읽는다() -> None:
    """🔴 **하루가 읽으면 이 함수를 부르는 모든 검사가 조용히 실 DB 를 친다.**

    ★ 읽는 자리는 `wake_up` 과 `walk` 이고 **실행당 한 번**이다 — 규칙은 날이
      아니라 실행에 속한다.

    🟢 **자기 생존 검사를 같이 둔다** — 같은 모듈의 `wake_up` 은 실제로 읽으므로,
      스캐너가 아무것도 안 재고 있으면 그쪽에서 갈린다.
    """
    하루코드 = _코드만(inspect.getsource(run_scheduled_day))
    깨어남코드 = _코드만(inspect.getsource(scheduler.wake_up))

    assert "terms_of" not in 하루코드, "하루 실행이 설정을 직접 읽는다"
    assert "read_run_sales_terms" not in 하루코드, "하루 실행이 설정을 직접 읽는다"
    assert "terms_of(" in 깨어남코드, "스캐너가 아무것도 안 재고 있다"


# ══════════════════════════════════════════════════════════════════════
#  ② 🟢 **사람이 준 값이 이긴다**
# ══════════════════════════════════════════════════════════════════════


def test_사람이_준_값을_규칙이_못_덮는다() -> None:
    """🟢 **덮으면 화면에서 거래처를 고른 사람이 자기가 안 고른 거래처로 판다.**"""
    사람값 = _요청(
        partner_id="P-사람",
        preferred_payment_terms_type="사람지급조건",
        preferred_payment_days=7,
        preferred_unit_price_krw=Decimal(999),
        source_ref="USER-REQ:U-9",
    )

    얹은 = apply_sales_terms(사람값, 조건규칙)

    assert 얹은.partner_id == "P-사람"
    assert 얹은.preferred_payment_terms_type == "사람지급조건"
    assert 얹은.preferred_payment_days == 7
    assert 얹은.preferred_unit_price_krw == Decimal(999)
    assert 얹은.source_ref == "USER-REQ:U-9"


def test_사람이_한_칸만_말해도_나머지는_규칙이_채운다() -> None:
    """★ **칸마다 따로 본다.** 한 칸이 찼다고 나머지를 통째로 안 채우면, 거래처만
    고른 사람이 단가 없는 요청을 받는다.
    """
    얹은 = apply_sales_terms(_요청(partner_id="P-사람"), 조건규칙)

    assert 얹은.partner_id == "P-사람"
    assert 얹은.preferred_payment_days == 지급일수


def test_지급일수_0_을_규칙이_안_덮는다() -> None:
    """⚠️ **0 도 사실이다** — *"당일 수금"* 은 정해진 조건이지 *"말 안 했다"* 가 아니다."""
    얹은 = apply_sales_terms(_요청(preferred_payment_days=0), 조건규칙)

    assert 얹은.preferred_payment_days == 0


def test_규칙이_없으면_요청을_그대로_돌려준다() -> None:
    """★ **같은 객체다.** 새로 만들면 *"안 얹었다"* 가 값으로 안 보인다."""
    요청 = _요청()

    assert apply_sales_terms(요청, None) is 요청


# ══════════════════════════════════════════════════════════════════════
#  ③ 🔴 **단가 — 마스터가 시세를 계산하지 않는다**
# ══════════════════════════════════════════════════════════════════════


def test_ML_이면_그날_시세를_그대로_옮긴다() -> None:
    """🔴 **곱하지도 반올림하지도 않는다.**"""
    규칙 = dataclasses.replace(
        조건규칙, unit_price_source=ML_CURRENT_PRICE, unit_price_krw=None
    )

    얹은 = apply_sales_terms(_요청(), 규칙, forecast_fn=_시세({"current_price": 시세}))

    assert 얹은.preferred_unit_price_krw == Decimal(시세)


def test_그날_예측이_없으면_단가를_안_싣는다() -> None:
    """⚠️ **옛 배치로 메우지 않는다.** 없는 것과 메운 것을 가르는 것이 §1.2-10 이다.

    ★ 나머지 셋은 그대로 실린다 — 단가 하나가 없다고 조건 전부를 버리지 않는다.
    """
    규칙 = dataclasses.replace(
        조건규칙, unit_price_source=ML_CURRENT_PRICE, unit_price_krw=None
    )

    얹은 = apply_sales_terms(_요청(), 규칙, forecast_fn=_시세(None))

    assert 얹은.preferred_unit_price_krw is None
    assert 얹은.partner_id == 거래처


def test_ML_이_0_을_주면_안_싣는다() -> None:
    """🔴 **ML 한 행이 그날 판매를 통째로 세우지 않는다.**"""
    규칙 = dataclasses.replace(
        조건규칙, unit_price_source=ML_CURRENT_PRICE, unit_price_krw=None
    )

    얹은 = apply_sales_terms(_요청(), 규칙, forecast_fn=_시세({"current_price": 0}))

    assert 얹은.preferred_unit_price_krw is None


def test_고정값이면_ML_을_안_읽는다() -> None:
    """★ 읽을 이유가 없다 — 읽으면 ML DB 가 죽은 날 고정가 판매도 같이 죽는다."""

    def 부르면_터진다(item: str, as_of: date) -> SourcedInput:
        raise AssertionError("고정 단가인데 ML 시세를 읽었다")

    얹은 = apply_sales_terms(_요청(), 조건규칙, forecast_fn=부르면_터진다)

    assert 얹은.preferred_unit_price_krw == 고정단가


# ══════════════════════════════════════════════════════════════════════
#  ④ 🔴 **`source_ref` 가 실행 규칙에서 왔다고 말한다**
# ══════════════════════════════════════════════════════════════════════


def test_source_ref_가_실행_규칙을_가리킨다() -> None:
    """🔴 **사람이 말한 것처럼 보이면 안 된다.**

    ★ 조건은 `sim_runs.config_json` 의 규칙에서 왔고, 그 행은 되짚을 수 있다.
    """
    판매 = _하루(sales_terms=조건규칙)

    ref = 판매.requests[0].source_ref
    assert ref is not None
    assert 실행 in ref, f"어느 실행의 규칙인지가 안 보인다: {ref}"
    assert "sim_runs" in ref, f"되짚을 표가 안 보인다: {ref}"


def test_source_ref_가_단가를_어디서_가져왔는지_말한다() -> None:
    """🔴 **안 적으면 같은 규칙 파일로 돈 두 실행이 구별되지 않는다** — *"시세로
    팔았나 고정값으로 팔았나"* 를 못 되짚는다.
    """
    고정 = rules_source_ref(실행, FIXED_UNIT_PRICE)
    엠엘 = rules_source_ref(실행, ML_CURRENT_PRICE)

    assert FIXED_UNIT_PRICE in 고정
    assert ML_CURRENT_PRICE in 엠엘
    assert 고정 != 엠엘


# ══════════════════════════════════════════════════════════════════════
#  ⑤ 🔴 **값이 코드에 안 박혀 있다**
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("파일", [_조건, _스케줄러, _걷기])
def test_거래처_id_와_지급조건이_코드에_없다(파일: Path) -> None:
    """🔴 **업무의 값을 코드에 박으면 바꾸는 날 diff 가 아니라 배포가 된다.**

    ★ `--opening-usage-scope` 를 문에 안 박은 것과 같은 자리다.
    """
    코드 = _NFC(_코드만(파일.read_text(encoding="utf-8")))

    for 낱말 in ("KIMCHI", "SINGLE", "INSTALLMENT"):
        assert 낱말 not in 코드, f"{파일.name} 이 업무의 값을 코드에 박았다: {낱말}"


def test_조건에_기본값이_하나도_없다() -> None:
    """🔴 **기본값은 곧 업무 규칙이다.**

    ★ 네 칸 중 하나라도 기본값을 두면 *"안 적어도 돈다"* 가 되고, 그러면 아무도
      안 정한 조건으로 판매가 선다. 🟢 단가 고정값(`unit_price_krw`)만 예외인데,
      그것은 `ML_CURRENT_PRICE` 일 때 **없는 것이 맞기** 때문이다.
    """
    기본값이_있는_칸 = {
        칸.name
        for 칸 in dataclasses.fields(SalesTermsRule)
        if 칸.default is not dataclasses.MISSING
        or 칸.default_factory is not dataclasses.MISSING  # type: ignore[misc]
    }

    assert 기본값이_있는_칸 == {"unit_price_krw"}, (
        f"조건 칸에 코드 기본값이 생겼다: {sorted(기본값이_있는_칸)}"
    )


# ══════════════════════════════════════════════════════════════════════
#  ⑥ 🔴 **규칙 파일을 읽는 자리 — 빈 칸은 기본값이 아니라 오류다**
# ══════════════════════════════════════════════════════════════════════


def _설정(**over: Any) -> dict[str, Any]:
    조건: dict[str, Any] = {
        "partner_id": 거래처,
        "payment_terms_type": 지급조건,
        "payment_days": 지급일수,
        "unit_price_source": FIXED_UNIT_PRICE,
        "unit_price_krw": int(고정단가),
    }
    조건.update(over)
    return {"backfill": {"sales_terms": 조건}}


def test_규칙_파일에_적은_조건을_그대로_읽는다() -> None:
    읽은 = read_rules(_설정()).sales_terms

    assert 읽은 == 조건규칙


def test_조건_칸을_안_적으면_None_이고_그것이_정상이다() -> None:
    """★ *"안 적었다"* 는 사고가 아니라 의도일 수 있다."""
    매입만 = {"rule": "ALWAYS_BASE", "scenario_label": "x"}
    rules = read_rules({"backfill": {"procurement": 매입만}})

    assert rules.sales_terms is None


@pytest.mark.parametrize("빈칸", ["partner_id", "payment_terms_type", "unit_price_source"])
def test_한_칸이라도_비면_터진다(빈칸: str) -> None:
    """🔴 **여기서 기본값을 지어내지 않는다.**"""
    with pytest.raises(BackfillRuleMissing):
        read_rules(_설정(**{빈칸: "  "}))


def test_지급일수가_참거짓이면_터진다() -> None:
    """🔴 **파이썬에서 `True` 는 `int` 다** — 그냥 두면 하루 유예가 된다."""
    with pytest.raises(BackfillRuleMissing):
        read_rules(_설정(payment_days=True))


def test_모르는_단가_출처는_터진다() -> None:
    with pytest.raises(BackfillRuleMissing):
        read_rules(_설정(unit_price_source="WHATEVER"))


def test_고정값인데_단가를_안_적으면_터진다() -> None:
    설정 = _설정()
    del 설정["backfill"]["sales_terms"]["unit_price_krw"]

    with pytest.raises(BackfillRuleMissing):
        read_rules(설정)


def test_ML_인데_고정단가도_적으면_터진다() -> None:
    """🔴 **단가가 둘이면 어느 쪽을 썼는지가 `source_ref` 와 갈린다.**"""
    with pytest.raises(BackfillRuleMissing):
        read_rules(_설정(unit_price_source=ML_CURRENT_PRICE))


def test_조건만_적은_규칙_파일도_옛_평면_모양으로_안_읽힌다() -> None:
    """🟢 **자기 생존 검사.** 아는 칸 목록에 조건이 빠지면 여기서 갈린다."""
    assert read_rules(_설정()).procurement is None


def test_조건이_없는_실행은_조용히_None_이다() -> None:
    """🔴 **규칙 파일 없이 연 실행이 기본값이다** — 매번 사유를 남기면 진짜 사유가
    안 읽힌다.
    """

    def 규칙없음(sim_run_id: str) -> BackfillRules:
        raise BackfillRuleMissing("backfill 칸이 없다")

    assert read_run_sales_terms(실행, rules_fn=규칙없음) is None


def test_설정을_못_읽어도_하루를_안_세운다() -> None:
    """★ 조건 없이 도는 것은 종전 동작이다 — 못 읽었다고 걷기가 멈추지 않는다."""

    def 터진다(sim_run_id: str) -> BackfillRules:
        raise RuntimeError("DB 가 죽었다")

    assert read_run_sales_terms(실행, rules_fn=터진다) is None


# ══════════════════════════════════════════════════════════════════════
#  ⑦ 🔴 **걷기가 조건을 읽어 하루에 넘긴다**
# ══════════════════════════════════════════════════════════════════════


class _하루기록:
    def __init__(self) -> None:
        self.받은것: list[dict[str, Any]] = []

    def __call__(self, action, **kwargs: Any) -> DayRunOutcome:
        self.받은것.append(kwargs)
        return DayRunOutcome(as_of=action.as_of, action=action.action, reason=action.reason)


def _걷는다(하루: _하루기록, **over: Any) -> None:
    인자: dict[str, Any] = {
        "sim_run_id": 실행,
        "start": AS_OF,
        "end": AS_OF,
        "now": datetime(2026, 9, 11, 10, 35, tzinfo=SEOUL),
        "calendar": lambda: _달력(),
        "readiness": _준비,
        "run_day_fn": 하루,
        "ticks": lambda: 0.0,
        "terms_of": lambda sim_run_id: 조건규칙,
    }
    인자.update(over)
    walk(**인자)


def test_걷기가_읽은_조건을_하루에_넘긴다() -> None:
    하루 = _하루기록()

    _걷는다(하루)

    assert 하루.받은것[0]["sales_terms"] == 조건규칙


def test_승인을_안_켜도_조건은_실린다() -> None:
    """🔴 **승인 스위치 뒤에 두지 않는다.**

    ★ 두면 *"규칙은 적었는데 왜 또 재무가 판정을 못 내나"* 가 생기고, 그 답이
      승인 스위치라는 것은 아무 데도 안 적혀 있다.
    """
    하루 = _하루기록()

    _걷는다(하루)

    assert 하루.받은것[0]["auto_approve"] is False
    assert 하루.받은것[0]["sales_terms"] == 조건규칙


def test_조건은_걷기당_한_번만_읽는다() -> None:
    """★ 규칙은 실행에 속하지 날에 속하지 않는다."""
    하루 = _하루기록()
    부른것: list[str] = []

    def 조건읽기(sim_run_id: str) -> SalesTermsRule:
        부른것.append(sim_run_id)
        return 조건규칙

    _걷는다(하루, end=AS_OF + timedelta(days=4), terms_of=조건읽기)

    assert 부른것 == [실행], f"걷기가 조건을 {len(부른것)}번 읽었다"
    assert len(하루.받은것) > 1, "하루를 한 번만 걸어서는 이 검사가 아무것도 안 잰다"
