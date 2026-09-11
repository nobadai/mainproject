"""**매입은 경매가로 사고 판매는 중도매가로 판다.** 한 계열을 같이 보면 안 된다.

🔴 **같은 `load_forecast` 를 매입과 판매가 같이 썼고, 조회에 `AUC` 가 박혀 있었다.**

  .. code-block:: text

      # app/master/inputs.py (전 판)
      WHERE item = %s AND as_of <= %s AND target_kind = 'AUC'

  그래서 **경매가로 사서 경매가로 팔았다.** 완주한 걷기 `SIM-CHAIN-V3`(1~3월) 실측:

  .. code-block:: text

      SALES_MARGIN_BELOW_MINIMUM   483 / 511
      무    SL1 7 · SL3 49    팔린 일곱 건이 전부 무
      배추  SL1 0 · SL3 56    한 건도 못 팖
      양파  SL1 0 · SL3 56    한 건도 못 팖

  실 DB 기간 평균으로 두 계열이 이만큼 갈린다 (단위는 둘 다 `원/kg` 로 같다).

  .. code-block:: text

      품목    AUC(경매)   WHSL(중도매)   차이
      배추      643        1,152        +79%
      무        558          854        +53%
      양파      834        1,022        +23%

⚠️ **`RTL`(소매)은 안 쓴다.** 단위가 `원/단위` 이고 `unit_weight_kg` 가 전부 비어
  있어 kg 로 못 바꾼다. 사람이 그 사실을 보고 중도매로 정했다.

★ 어휘 자체(`AUC` · `WHSL` · `RTL`)는 **ML 것**이다 (`app/ml/schemas.py TargetKind`).
  우리는 고르기만 하고 새로 만들지 않는다.

이 파일이 잠그는 넷:

.. code-block:: text

    1. 매입은 AUC · 판매는 WHSL 을 읽는다 — 같은 날 같은 품목으로 조회 인자가 갈린다
    2. 계열은 기본값 없는 키워드다 — 안 넘기면 TypeError
    3. source_ref 에 그 계열 이름이 그대로 들어간다
    4. `inputs.py` 안에 `'AUC'` 리터럴이 **상수 정의 한 자리에만** 있다
"""

from __future__ import annotations

import ast
import inspect
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from psycopg import sql

from app.master import forecast_gate, inputs, sales_terms
from app.master import service as service_mod
from app.master.backfill import ML_CURRENT_PRICE, SalesTermsRule
from app.master.schemas import SalesRunRequest

AS_OF = date(2025, 12, 31)
ITEM = "배추"

#: 뷰 한 행. ★ `as_of` 가 요청일과 같아야 `MEASURED` 다 (당일 배치 규칙).
_ROW: dict[str, Any] = {
    "as_of": AS_OF,
    "item": ITEM,
    "target_kind": "AUC",
    "generated_at": "2025-12-31T06:00:00+09:00",
    "unit": "원/kg",
    "current_price": 645,
    "horizon_days": 18,
    "daily": [{"date": "2026-01-01", "predicted": 645}],
    "model_version": "ops_auc",
    "quality_note": "",
}

_INPUTS_PY = Path(inputs.__file__)


def _요청() -> SalesRunRequest:
    return SalesRunRequest(
        as_of=AS_OF, policy_version="v1.3", business_mode="SPOT_SALES", item=ITEM
    )


def _sql_text(query: Any) -> str:
    return query.as_string(None) if isinstance(query, sql.Composable) else str(query)


@pytest.fixture
def 본_조회(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """예측 뷰를 친 조회의 **인자만** 모은다. ⚠️ DB 를 타지 않는다.

    ★ 행을 안 돌려준다 — 이 검사가 묻는 것은 *"무엇으로 물었나"* 이지 *"무엇이
      돌아왔나"* 가 아니다. 못 읽으면 `MISSING` 이고 그 갈래는 다른 파일이 이미 잠근다.
    """
    본것: list[tuple[Any, ...]] = []

    def fetch_one(query: Any, params: Any = ()) -> None:
        if "v_ml_price_forecast" in _sql_text(query):
            본것.append(tuple(params))

    monkeypatch.setattr(inputs, "fetch_one", fetch_one)
    monkeypatch.setattr(inputs, "fetch_all", lambda *a: [])
    monkeypatch.setattr(inputs, "get_db_schema", lambda: "haetdeul")
    return 본것


@pytest.fixture
def 판매_적재를_되살린다(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest 가 꺼 둔 `service.load_forecast` 를 이 파일에서만 실물로 되돌린다.

    ⚠️ DB 는 그대로 안 탄다 — `본_조회` 가 `fetch_one` 을 갈아 끼운다.
    """
    monkeypatch.setattr(service_mod, "load_forecast", inputs.load_forecast)


# ── 잠금 ① 매입 AUC · 판매 WHSL ─────────────────────────────────────────


def test_매입과_판매가_다른_계열을_읽는다(본_조회, 판매_적재를_되살린다):
    """★★ **이 판의 핵심.** 같은 날 같은 품목인데 조회 인자가 갈려야 한다.

    🔴 갈리지 않으면 경매가로 사서 경매가로 판다 — 마진이 설 자리가 없다.
    """
    inputs.collect_inputs(ITEM, AS_OF)
    매입 = 본_조회[-1]

    service_mod._sales_forecast(_요청())
    판매 = 본_조회[-1]

    assert 매입 == (ITEM, AS_OF, inputs.PROCUREMENT_TARGET_KIND) == (ITEM, AS_OF, "AUC")
    assert 판매 == (ITEM, AS_OF, inputs.SALES_TARGET_KIND) == (ITEM, AS_OF, "WHSL")
    assert 매입 != 판매, "매입과 판매가 같은 시세를 본다 — 경매가로 사서 경매가로 판다"


def test_판매_단가_규칙도_중도매를_읽는다(본_조회):
    """⚠️ **파는 단가가 실제로 서는 자리가 여기다** (`ML_CURRENT_PRICE`).

    `sales_terms` 가 `preferred_unit_price_krw` 를 여기서 채운다 — 이 자리가
    경매가를 읽으면 `_sales_forecast` 를 고쳐도 **파는 값은 그대로 경매가다.**
    """
    규칙 = SalesTermsRule(
        partner_id="P-1",
        payment_terms_type="NET",
        payment_days=30,
        unit_price_source=ML_CURRENT_PRICE,
    )
    sales_terms.apply_sales_terms(_요청(), 규칙)

    assert 본_조회 == [(ITEM, AS_OF, "WHSL")], "판매 단가가 경매 계열을 읽는다"


def test_매입_예측_관문은_경매_그대로다(본_조회):
    """🟢 **안 바뀌는 것을 잠근다.** 매입 등급 사다리가 이 관문 위에 선다.

    판매를 중도매로 옮기면서 매입 관문까지 끌려가면 **매입이 조용히 다른 시세로
    산다.** 그 사고는 값이 아니라 등급으로 나타나 눈에 안 띈다.
    """
    forecast_gate.check_forecast_gate(ITEM, AS_OF)

    assert 본_조회 == [(ITEM, AS_OF, "AUC")], "매입 관문이 경매를 안 읽는다"


# ── 잠금 ② 기본값 없는 키워드 ────────────────────────────────────────────


def test_계열은_기본값_없는_키워드다():
    """🔴 **기본값은 곧 업무 규칙이다.**

    기본값을 두면 안 넘긴 자리가 **조용히 경매가로 답한다** — 지금까지가 정확히
    그 상태였다. `revalidation.revalidate_scenario` 가 `as_of` 에 대해 같은 결론을
    냈다: 안 넘기면 터져야 한다.
    """
    param = inspect.signature(inputs.load_forecast).parameters["target_kind"]
    왜 = "기본값이 생겼다 — 안 넘긴 자리가 조용히 경매가로 답한다"

    assert param.kind is inspect.Parameter.KEYWORD_ONLY, "위치 인자면 순서로 섞인다"
    assert param.default is inspect.Parameter.empty, 왜


def test_계열을_안_넘기면_터진다(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(inputs, "fetch_one", lambda *a: None)
    monkeypatch.setattr(inputs, "get_db_schema", lambda: "haetdeul")

    with pytest.raises(TypeError):
        inputs.load_forecast(ITEM, AS_OF)  # type: ignore[call-arg]


# ── 잠금 ③ 출처가 계열을 나른다 ──────────────────────────────────────────


@pytest.mark.parametrize("계열", ["AUC", "WHSL"])
def test_출처에_계열_이름이_그대로_남는다(monkeypatch: pytest.MonkeyPatch, 계열: str):
    """★ 나중에 *"이 단가가 경매였나 중도매였나"* 를 되짚는 유일한 자리다.

    지금까지는 늘 경매라 이 칸이 잠들어 있었다 — **이제 실제로 갈린다.**
    """
    monkeypatch.setattr(inputs, "fetch_one", lambda *a: {**_ROW, "target_kind": 계열})
    monkeypatch.setattr(inputs, "get_db_schema", lambda: "haetdeul")

    got = inputs.load_forecast(ITEM, AS_OF, target_kind=계열)

    assert got.grade == "MEASURED"
    assert got.source == f"v_ml_price_forecast(as_of={AS_OF}, {계열})"
    assert got.payload["target_kind"] == 계열


# ── 잠금 ④ 리터럴이 한 자리에만 있다 ─────────────────────────────────────


def _문자열_상수(tree: ast.AST) -> list[ast.Constant]:
    """docstring 을 뺀 문자열 상수 전부.

    ★ docstring 은 사람이 읽는 설명이라 계열 이름을 적어도 된다. **코드가 읽는
      문자열**만 센다 — SQL 본문도 여기 들어온다.
    """
    문서: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        head = body[0]
        if (
            isinstance(head, ast.Expr)
            and isinstance(head.value, ast.Constant)
            and isinstance(head.value.value, str)
        ):
            문서.add(id(head.value))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in 문서
    ]


def test_AUC_리터럴이_상수_정의_한_자리에만_있다():
    """🔴 **리터럴을 흩뿌리면 왜 그 값인지가 사라진다.**

    ★ 「0건을 세면 그것도 막는다」 — 세는 방법이 깨져 0이 나오면 이 검사는 아무것도
      안 막으면서 통과한다. 그래서 0도 실패로 둔다.
    """
    tree = ast.parse(_INPUTS_PY.read_text(encoding="utf-8"))
    쓰인곳 = [
        unicodedata.normalize("NFC", node.value)
        for node in _문자열_상수(tree)
        if "AUC" in unicodedata.normalize("NFC", node.value)
    ]

    assert 쓰인곳, "'AUC' 를 한 건도 못 셌다 — 세는 방법이 깨졌으면 이 검사는 아무것도 안 막는다"
    assert len(쓰인곳) == 1, f"'AUC' 가 {len(쓰인곳)} 자리에 있다: {쓰인곳}"
    assert 쓰인곳[0] == "AUC", f"조회나 다른 문자열에 계열이 박혔다: {쓰인곳[0]!r}"


def test_두_계열이_이름_붙은_상수로_선다():
    """★ **왜 그 값인지**를 한 자리에만 적는다. 부르는 자리는 이름만 읽는다."""
    tree = ast.parse(_INPUTS_PY.read_text(encoding="utf-8"))
    선언: dict[str, Any] = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert 선언.get("PROCUREMENT_TARGET_KIND") == "AUC"
    assert 선언.get("SALES_TARGET_KIND") == "WHSL"
    assert inputs.PROCUREMENT_TARGET_KIND != inputs.SALES_TARGET_KIND
