"""가락 등급별 당일 경락가를 ``auction_prices_daily`` 에서 읽는다 (#70).

**시세는 매입 자기 도메인이다** (정의서 §4.1). 마스터 봉투로 받지 않고 우리가 직접 읽는다.
읽기 경로는 ``db.py`` 하나뿐이고 그 모듈에는 쓰기 헬퍼가 없다 (규칙 2).

## 좌표 여섯을 왜 여기서 안 정하는가

전부 ``constraints.yaml`` 의 ``market_quotes`` 절에서 읽는다 (규칙 7). 이 모듈은 그 좌표로
쿼리를 만들 뿐이다. ML 이 2026-08-27 에 규격을 한 번 바꿨고, 바뀌면 고칠 자리가 한 곳이어야
한다.

## 품종을 고르지 않는다

``subclass_code`` · ``subclass_name`` 은 ``WHERE`` 에도 ``GROUP BY`` 에도 **쓰지 않는다**
(8/28 지환님 결정). 물량가중이 품종 축을 자동으로 정리하고, ML 수집·학습표와 같은 식이라
축이 일치한다. 하나라도 쓰는 순간 "고르지 않는다"가 깨지므로 계약 테스트가 이 파일의
쿼리 문자열을 검사한다.

⚠️ 다만 품종 정리는 **규격을 잠근 뒤에야** 성립한다. 2025-12-31 배추 그물망 10kg 은
김장(가을)배추 단일인데, 규격을 풀면 쌈배추(12,484원/kg)가 같은 가중에 들어와 축이 무너진다.

## 이 시리즈는 ML ``current_price`` 와 같지 않다

같은 좌표(서울가락 · 특 · 그물망·파렛트 10kg)로 계산해도 2025-12-31 배추가
**우리 933원 vs ML 812원** 이다. 후행 1·2·3·5·7·10 영업일 물량가중 어느 창으로도 재현되지
않았다 (2026-08-31 실측). ML 내부 시리즈라 원자료만으로 복원할 수 없다는 뜻이고,
그래서 ``rise_rate`` 분모는 **ML 이 예측과 같은 행에 동봉한 ``current_price`` 를 그대로
쓴다** (#57). 두 시리즈를 섞으면 배추 기준 rise_rate 가 12.2%p 갈린다.
"""

from collections.abc import Callable, Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from psycopg import sql

from app.purchase_agent import db
from app.purchase_agent.config import load_constraints

#: 시세 공급자. ``ports.get_market_quotes`` 가 이 모양의 콜러블을 주입받는다.
#: mock 과 DB 를 **명시 주입**으로 고른다 — 환경변수 스위치를 두지 않는다.
QuoteSource = Callable[[str, date], list[dict[str, Any]]]

#: DB 조회 함수. 테스트가 가짜 행을 꽂을 수 있게 인자로 빼둔다.
Fetch = Callable[..., list[dict[str, Any]]]


#: 이 모듈이 **실제로 구현한** 좌표. 선언과 다르면 조회를 시작하지 않는다.
#:
#: 🔴 선언만 있고 아무도 안 읽는 값은 **단일 소스인 척하는 주석**이다 (Codex 교차검증
#:   2026-08-31). ``weighting`` 을 ``simple`` 로 바꿔도 계산이 그대로면, YAML 은 사실을
#:   말하는 게 아니라 사실처럼 보이는 글자다. 여기서 대조해 선언이 실행에 닿게 한다.
_IMPLEMENTED = {
    # 이 테이블 전체가 경락이다 — 가격종류 컬럼이 없어 필터로 못 쓰고, 선언으로만 남는다.
    # 그래서 더더욱 대조가 필요하다: WHSL/RTL 로 바꿔 적어도 조회가 그대로 돌아버린다.
    "price_kind": "AUC",
    # 거래대금(원) ÷ 거래중량(kg) 이므로 단위는 원/kg 하나뿐이다.
    "unit": "원/kg",
    # ``krw_per_kg`` 이 구현한 유일한 가중 방식.
    "weighting": "volume",
}


def validate_coordinates(cfg: Mapping[str, Any]) -> None:
    """선언된 좌표가 이 모듈이 구현한 것과 같은지 본다. 다르면 **조회하지 않는다.**

    조용히 무시하면 "설정을 바꿨는데 왜 그대로지"가 되고, 최악은 바꾼 줄 알고 쓰는 것이다.
    """
    wrong = {
        key: cfg.get(key) for key, value in _IMPLEMENTED.items() if cfg.get(key) != value
    }
    if wrong:
        raise ValueError(
            f"market_quotes 좌표 선언이 구현과 다르다: {wrong} — 구현값 {_IMPLEMENTED}. "
            f"선언만 바꾼다고 계산이 따라 바뀌지 않으므로 조회를 진행하지 않는다"
        )


def spec_for_item(item: str, constraints: Mapping[str, Any]) -> dict[str, Any] | None:
    """그 품목의 조회 규격. **미확정이면 None 이다 — 임의 규격으로 채우지 않는다** (규칙 3).

    피마늘이 그 경우다. ML 에 피마늘 AUC 예측이 없고, 원천 테이블의 품목명은 "마늘"이며
    품종 목록에 "피마늘"이 없다. 어느 이름·규격으로 조회할지가 미결이라 조회하지 않는다.

    ⚠️ **키가 없는 것과 값이 null 인 것을 구분한다.** null 은 "미결이라 안 읽는다"는
      **결정**이고, 키 부재는 그냥 빠뜨린 것이다. 둘을 같이 처리하면 실수로 지운 품목이
      피마늘과 똑같이 조용히 넘어간다 — 그 품목은 그날부터 영원히 시세 없이 돈다.

    규격이 반쯤 적힌 상태도 여기서 막는다. ``packages`` 만 있고 ``unit_weight_kg`` 이 없으면
    조회 시점에 ``KeyError`` 로 죽는데, 그때는 사유를 낼 자리가 이미 지나갔다.
    """
    spec_by_item = constraints["market_quotes"]["spec_by_item"]
    if item not in spec_by_item:
        raise KeyError(
            f"market_quotes.spec_by_item 에 {item!r} 항목이 없다 — 규격 미결이면 "
            f"키를 지우지 말고 값을 null 로 둔다 (규칙 3: 빠뜨린 것과 미결은 다르다)"
        )
    spec = spec_by_item[item]
    if spec is None:
        return None
    if not isinstance(spec, Mapping):
        raise TypeError(f"spec_by_item[{item!r}] must be a mapping or null, got {spec!r}")
    return _checked_spec(item, spec)


def _checked_spec(item: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    """규격 세 값이 조회에 쓸 수 있는 모양인지 확인한다."""
    packages = spec.get("packages")
    weight = spec.get("unit_weight_kg")
    label = spec.get("label")
    # 문자열을 넘기면 ``list("그물망")`` 이 글자 목록이 되어 **조용히 0건**이 된다.
    if not isinstance(packages, list) or not packages:
        raise ValueError(
            f"spec_by_item[{item!r}].packages must be a non-empty list, got {packages!r}"
        )
    if not all(isinstance(name, str) and name for name in packages):
        raise ValueError(f"spec_by_item[{item!r}].packages must hold names, got {packages!r}")
    if isinstance(weight, bool) or not isinstance(weight, int | float) or weight <= 0:
        raise ValueError(f"spec_by_item[{item!r}].unit_weight_kg must be positive, got {weight!r}")
    if not isinstance(label, str) or not label:
        # label 은 사유 문장에 그대로 실린다 — 비면 "그날 무엇을 봤는지"가 사라진다.
        raise ValueError(f"spec_by_item[{item!r}].label must be a non-empty string, got {label!r}")
    _check_before(item, spec.get("before"))
    return dict(spec)


def _check_before(item: str, before: Any) -> None:
    """규격 전환 블록(``before``)도 같은 검사를 받는다.

    ⚠️ 여기서 안 보면 ``date`` 누락은 쿼리 파라미터를 채울 때 **늦게 ``KeyError``** 로 터지고
      (그때는 사유를 낼 자리가 지나갔다), 음수 중량은 조용히 0행 → "거래 기록이 없다"는
      **틀린 사유**로 이어진다 (Codex 2차 지적).
    """
    if before is None:
        return
    if not isinstance(before, Mapping):
        raise TypeError(
            f"spec_by_item[{item!r}].before must be a mapping or absent, got {before!r}"
        )
    if not isinstance(before.get("date"), date):
        raise TypeError(
            f"spec_by_item[{item!r}].before.date must be a date, got {before.get('date')!r}"
        )
    weight = before.get("unit_weight_kg")
    if isinstance(weight, bool) or not isinstance(weight, int | float) or weight <= 0:
        raise ValueError(
            f"spec_by_item[{item!r}].before.unit_weight_kg must be positive, got {weight!r}"
        )
    label = before.get("label")
    if not isinstance(label, str) or not label:
        raise ValueError(
            f"spec_by_item[{item!r}].before.label must be a non-empty string, got {label!r}"
        )


def krw_per_kg(amount_krw: Decimal, volume_kg: Decimal) -> Decimal:
    """물량가중 단가 = 거래대금 합 ÷ 거래중량 합. **단순평균이 아니다.**

    2026-08-03 배추 특(가락, 규격 무필터) 물량가중 938.5 vs 단순평균 2,009.9 — 2.1배다.
    ``grade_unit_price`` 는 사중 일치 금액 축에 직접 걸리므로 식이 틀리면 금액이 통째로
    어긋난다.

    ``Decimal`` 로 받는 이유: 두 컬럼이 ``numeric`` 이라 psycopg 가 Decimal 을 돌려준다.
    float 로 옮기면 합계 자리에서 오차가 생기고, 그 오차가 반올림 경계를 넘길 수 있다.
    """
    if volume_kg <= 0:
        raise ValueError(f"거래중량이 {volume_kg} 이라 물량가중 단가를 낼 수 없다")
    return amount_krw / volume_kg


def to_price(unit_price: Decimal) -> int:
    """계약이 요구하는 정수 원/kg (``schemas.SourcingLine.grade_unit_price: int``).

    ⚠️ **내장 ``round()`` 를 쓰지 않는다.** 파이썬은 은행가 반올림이라 ``round(938.5)`` 가
      **938** 이다. DoD 재현치가 하필 ``.5`` 로 끝나는 값이라(938.5) 이 차이가 그대로
      드러나고, 그런 값은 실데이터에서 드물지 않다. 통상적인 금액 반올림(사사오입)으로
      고정한다.
    """
    return int(unit_price.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _amount(value: Any) -> Decimal | None:
    """집계 값 하나를 Decimal 로. **읽을 수 없으면 None 이다 — 0이 아니다** (규칙 3).

    ⚠️ **``_query`` 의 행 필터와 이중이다. 왜 둘 다 두는가.**

    둘은 **다른 것**을 막는다. ``_query`` 는 *어느 행을 합칠지*를 정하고 — 그게 유일하게
    올바른 자리다(합쳐진 뒤에는 복구가 불가능하다) — 이 함수는 *``_materialize`` 가 받은
    값이 숫자인지*를 본다. ``_materialize`` 는 쿼리 결과만 받는 게 아니다: 테스트가 가짜
    ``fetch`` 를 꽂고, 나중에 쿼리가 바뀔 수도 있다. 입력을 안 보는 순수 함수는 그때
    ``Decimal(None)`` 으로 죽는데, **죽는 쪽은 사유를 못 낸다** (결정 d).

    그러니 이 함수를 지우려면 ``_materialize`` 가 ``_query`` 전용이라는 보장이 먼저 있어야
    한다. 지금은 없다.

    2026-08-31 실측으로 가락 164,046행에 NULL·음수·0중량이 **하나도 없다** — 두 방어 모두
    현재 데이터에서는 한 번도 발동하지 않는다. 그래도 두는 이유는 이 값들이 우리가 만드는
    게 아니라 적재되는 것이기 때문이다.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    converted = Decimal(value)
    return converted if converted.is_finite() and converted >= 0 else None


def _weight_condition(spec: Mapping[str, Any]) -> sql.Composable:
    """규격 중량 조건. **날짜에 따라 바뀌는 품목이 있다.**

    무는 2018-01-01 이전이 18kg 이다 (ML ``spec_desc``: *"상자·파렛트 20kg (2018년 이전
    18kg)"*). 우리 IO명세도 *"무 규격 2018 전 18kg(백테스트 주의)"* 로 적어두고 있었는데
    코드에는 없었다 — 20kg 로 고정하면 2017년 244 거래일 중 **64일만** 잡힌다(18kg 는
    227일). 에러 없이 다른 시리즈가 된다.

    ``CASE`` 로 쓰는 이유: 두 중량을 ``IN`` 으로 합치면 전환 전후가 한 날에 섞일 수 있다.
    2017년에 18kg 과 20kg 이 **둘 다 존재**해서 실제로 섞인다.
    """
    before = spec.get("before")
    if before is None:
        return sql.SQL("unit_weight_kg = %(unit_weight_kg)s")
    return sql.SQL(
        "unit_weight_kg = CASE WHEN auction_date < %(spec_switch_date)s "
        "THEN %(unit_weight_before)s ELSE %(unit_weight_kg)s END"
    )


def _query(schema: str, table: str, weight_condition: sql.Composable) -> sql.Composed:
    """**as_of 이전 최신 거래일 하루**의 등급별 물량가중 집계.

    🔴 ``auction_date < as_of`` 다 — 당일이 아니다 (2026-08-31 확인).
      우리는 **아침에 돈다**(상세설계 §128 · 역할계약서 §45 · 백로그 §12 · CLAUDE.md).
      그 시각엔 당일 경매가 끝나지 않았고, 적재는 더 늦다 — 2026-08-31 실측으로
      ``haetdeul`` 최신이 08-26(5일 지연), 원천도 08-29(2일 지연)였다.
      ``= as_of`` 로 두면 실운영에서 **매일 0행**이고, 그때마다 "휴장이거나 그 규격 거래가
      없다"는 **틀린 사유**가 나간다. 12-31 관통이 돌았던 건 그날이 9개월 전 과거라
      이미 적재돼 있었기 때문이지, 실운영 가능성을 보인 게 아니다.

    🔴 **하루를 통째로 고른다 — 등급별로 각자 거슬러 올라가지 않는다.**
      등급마다 "그 등급의 최신 거래일"을 잡으면 2025-12-31 기준으로 이렇게 된다::

          배추 특  2025-12-30 (1일 전)
          배추 상  2025-11-12 (1.5개월 전)
          배추 하  2023-02-23 (2.8년 전)

      스프레드가 **서로 다른 시점의 두 가격**을 비교하게 되고, 규칙 4의 "당일 시세에
      실재하는 값"이 무너진다. ``max(auction_date)`` 를 먼저 정하고 그날 것만 쓴다.

    🔴 ``subclass_*`` 가 **없다** — WHERE 에도 GROUP BY 에도. 품종을 고르지 않는다는
      결정이 코드에서 지켜지는 자리이고, 계약 테스트가 이 문자열을 검사한다.

    🔴 **행 단위로 거른다 — 집계 뒤에 거르면 늦다** (Codex 교차검증 2026-08-31).
      ``sum()`` 은 NULL 을 **건너뛰는데** 다른 컬럼의 합계에는 그 행의 값이 그대로 들어간다.
      그래서 집계 결과만 보고 막으면 분자·분모가 서로 다른 행 집합에서 나온다::

          (1000, 10) + (NULL, 10)  →  sum 1000 / 20 =  50   (정답 100)
          (1000, 10) + (1000,  0)  →  sum 2000 / 10 = 200   (정답 100)

      두 경우 다 총중량이 양수라 ``HAVING`` 을 통과하고, **에러 없이 단가가 2배 틀린다.**
      한번 합쳐진 뒤에는 복구할 방법이 없으므로 필터가 여기 있어야 한다.

      ``trade_volume_kg > 0`` 은 NULL 도 함께 떨어뜨린다(NULL 비교는 참이 아니다).
      금액만 ``IS NOT NULL`` 을 따로 적는다 — 0원 낙찰은 있을 수 있어 ``> 0`` 이 아니다.

    ``HAVING`` 을 따로 두지 않는다. 남은 행이 전부 양수 중량이라 그룹 합계도 양수이고,
    같은 뜻의 검사를 두 곳에 두면 한쪽만 바뀐다.
    """
    return sql.SQL("""
        WITH usable AS (
          SELECT auction_date, grade_name, trade_amount_krw, trade_volume_kg
            FROM {}.{}
           WHERE auction_date     <  %(as_of)s
             AND market_category  =  %(market_category)s
             AND item_name        =  %(item)s
             AND grade_name       =  ANY(%(grades)s)
             AND package_name     =  ANY(%(packages)s)
             AND {}
             AND trade_volume_kg  >  0
             AND trade_amount_krw IS NOT NULL
             AND trade_amount_krw >= 0
        )
        SELECT auction_date AS observed_date,
               grade_name   AS grade,
               sum(trade_amount_krw) AS amount_krw,
               sum(trade_volume_kg)  AS volume_kg
          FROM usable
         WHERE auction_date = (SELECT max(auction_date) FROM usable)
         GROUP BY auction_date, grade_name
    """).format(sql.Identifier(schema), sql.Identifier(table), weight_condition)


def auction_quote_source(*, fetch: Fetch | None = None, schema: str | None = None) -> QuoteSource:
    """``ports.get_market_quotes`` 에 꽂을 DB 공급자를 만든다.

    ``fetch`` · ``schema`` 를 인자로 뺀 이유는 테스트다 — 가짜 행을 꽂으면 쿼리 결과를
    시세 형태로 옮기는 부분(가중·반올림·정렬·등급 어휘)이 DB 없이 전부 시험된다.
    실제 조회가 필요한 테스트만 ``db`` 마커로 분리한다.
    """
    do_fetch = fetch if fetch is not None else db.fetch_all
    schema_name = schema

    def load(item: str, as_of: date) -> list[dict[str, Any]]:
        # 좌표는 매 호출마다 다시 읽는다 — ``load_constraints`` 가 캐시하지 않는 이유와 같다
        # (feedback 이 임계를 덮어쓸 수 있어 공유 dict 를 들고 있으면 오염이 샌다).
        constraints = load_constraints()
        cfg = constraints["market_quotes"]
        validate_coordinates(cfg)
        spec = spec_for_item(item, constraints)
        if spec is None:
            # 규격 미확정 품목(피마늘)은 **조회하지 않는다**. 아무 규격으로나 물어보면
            # 값이 오고, 그 값은 우리가 뜻한 시리즈가 아니다 (규칙 3).
            return []
        params: dict[str, Any] = {
            "as_of": as_of,
            "market_category": cfg["market_category"],
            "item": item,
            "grades": list(cfg["grades"]),
            "packages": list(spec["packages"]),
            "unit_weight_kg": spec["unit_weight_kg"],
        }
        before = spec.get("before")
        if before is not None:
            params["spec_switch_date"] = before["date"]
            params["unit_weight_before"] = before["unit_weight_kg"]
        rows = do_fetch(
            _query(
                schema_name or db.get_db_schema(), cfg["table"], _weight_condition(spec)
            ),
            params,
        )
        return _materialize(rows, cfg, spec)

    return load


def _materialize(
    rows: list[dict[str, Any]], cfg: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """집계 행을 IO명세 §1-② 의 시세 형태로 옮긴다.

    ``spec`` 을 한 줄씩 얹는다. 사유 문장이 **"그날 그 규격에 그 등급이 없었다"** 를 말하려면
    무엇을 보고 있었는지가 데이터에서 나와야 한다 — constraints 에서 다시 읽으면 mock 경로에
    없는 규격을 있는 것처럼 적게 된다 (같은 계열의 거짓 사유를 이미 두 번 냈다).

    ``check_prices_exist`` 는 ``(market, grade, price)`` 세 키만 보므로 이 키는 대조를
    바꾸지 않고, ``materialize_sourcing`` 이 계약 4필드만 투영하므로 **출력에도 새지 않는다.**
    """
    # ★ **등급별로 합산한다 — 행 하나를 집지 않는다.** 지금 쿼리는 ``GROUP BY grade_name``
    #   이라 등급당 한 행이지만, ``{등급: 행}`` 으로 받으면 행이 둘 이상 오는 순간 **마지막
    #   것만 조용히 남는다**. 관통일 배추가 정확히 그 모양이다(그물망 10kg + 파렛트 10kg):
    #   합산이면 933원, 마지막 행만 집으면 970원 — 에러 없이 4% 어긋난다.
    #   여기서 합쳐두면 나중에 GROUP BY 축이 늘어도 물량가중이 유일한 정답으로 남는다.
    observed = _single_observed_date(rows)
    label = _spec_label_on(spec, observed)
    totals: dict[str, list[Decimal]] = {}
    for row in rows:
        amount = _amount(row["amount_krw"])
        volume = _amount(row["volume_kg"])
        if amount is None or volume is None:
            # 잴 수 없는 행. ``HAVING sum(trade_volume_kg) > 0`` 과 같은 처분이다 — 0으로
            # 채우면 "그 등급이 0원"이라는 없는 사실이 만들어지고, 죽으면 오케스트레이터가
            # 원인을 못 받는다 (규칙 3 · 결정 d). 등급이 전부 빠지면 0건 사유로 이어진다.
            continue
        carried = totals.setdefault(str(row["grade"]), [Decimal(0), Decimal(0)])
        totals[str(row["grade"])] = [carried[0] + amount, carried[1] + volume]
    quotes = []
    # 선언한 등급 순서대로 낸다. ⑥의 근거 문장이 ``market_quotes[0]`` 을 대표값으로 읽으므로
    # 순서가 흔들리면 같은 날 근거 문구가 달라진다.
    for grade in cfg["grades"]:
        summed = totals.get(grade)
        if summed is None or summed[1] <= 0:
            continue
        price = to_price(krw_per_kg(summed[0], summed[1]))
        if price <= 0:
            # ``grade_unit_price`` 는 스키마가 ``gt=0`` 이라, 0 이하가 한 줄이라도 섞이면
            # **제안 전체**가 출력 경계에서 죽는다. 그 등급 하나를 빼는 쪽이 맞다.
            continue
        quotes.append(
            {
                "market": cfg["market_category"],
                "grade": grade,
                "price": price,
                "spec": label,
                # ★ **as_of 가 아니라 실제 관측일이다.** 12-30 값을 12-31 시세라고 적으면
                #   그것도 거짓이다 — 사유·근거·ref_id 가 전부 이 값을 가져간다.
                "observed_date": observed,
            }
        )
    return quotes


def _single_observed_date(rows: list[dict[str, Any]]) -> str | None:
    """쿼리가 고른 **하루**. 두 날짜가 섞여 오면 멈춘다.

    쿼리가 ``max(auction_date)`` 하나로 좁히므로 정상 경로에서는 항상 한 날이다. 그런데도
    검사하는 이유: 등급별로 각자 거슬러 올라가는 형태로 쿼리가 바뀌면 **스프레드가 서로 다른
    시점의 두 가격을 비교**하게 되고, 그건 에러가 아니라 조용히 틀린 판단이 된다.
    """
    # 🔴 **전 행에 있어야 한다.** ``if row.get(...)`` 로 걸러 읽으면, 한 행만 날짜가 있고
    #   나머지는 없을 때 "단일 날짜"로 인정해 **함께 합산**한다 (Codex 2차 지적).
    missing = [row for row in rows if not row.get("observed_date")]
    if rows and missing:
        raise ValueError(
            f"관측일 없는 행이 {len(missing)}건 섞였다 — 어느 날 값인지 모르는 행을 "
            f"합산하면 물량가중이 서로 다른 시점을 섞는다"
        )
    dates = {str(row["observed_date"]) for row in rows}
    if not dates:
        return None
    if len(dates) > 1:
        raise ValueError(
            f"관측일이 하루가 아니다: {sorted(dates)} — 등급별로 다른 날을 집으면 "
            f"스프레드가 다른 시점의 두 가격을 비교하게 된다 (규칙 4)"
        )
    return dates.pop()


def _spec_label_on(spec: Mapping[str, Any], observed: str | None) -> str:
    """그날 유효했던 규격 이름. 전환일 이전이면 옛 규격 이름을 쓴다.

    라벨은 사유 문장에 그대로 실린다 — 2017년 값을 보면서 "상자·파렛트 20kg"이라고 적으면
    **무엇을 봤는지가 거짓**이 된다.
    """
    before = spec.get("before")
    if before is None or observed is None:
        return str(spec["label"])
    return str(before["label"] if observed < str(before["date"]) else spec["label"])


#: 실측 시세임을 나타내는 표시 두 개. **한 묶음이다** — 반쪽만 있으면 계약 위반이다.
#: mock 은 둘 다 없고, DB 공급자는 둘 다 싣는다.
PROVENANCE_KEYS = ("spec", "observed_date")


def provenance_problem(
    quotes: list[dict[str, Any]], as_of: str, constraints: Mapping[str, Any]
) -> str | None:
    """관측 표기가 계약대로인가. 어긋나면 **사유 문장**을 돌려준다 (예외가 아니다).

    🔴 이 검사는 **주입 경로에도 걸려야 한다.** 전에는 ``_materialize`` 안에만 있어서
      DB 경로만 지켰고, 주입 시세는 관측일이 미래여도·섞여도·아예 없어도 그대로 통과했다
      (Codex 2차 지적, 전부 재현됨).

    ⚠️ **왜 ``ports`` 가 아니라 여기인가.** 포트는 T0(``build_initial_state``)에서 도는데,
      거기서 예외를 던지면 그래프가 시작조차 못 하고 **어느 노드도 사유를 쓸 자리가 없다**
      — 결정 d 가 막으려던 그것이다. 그리고 mock 은 관측 표기가 정당하게 없어서 포트에서
      일률적으로 요구할 수도 없다. 그래서 판정은 ③·⑤ 가 공유하는 이 함수가 하고, 포트는
      구조 계약(market·grade·price)만 본다.

    검사 다섯. 앞의 것이 걸리면 뒤는 보지 않는다 — 원인을 하나로 말해야 읽는 사람이 그 자리를 본다.
    """
    marked = [q for q in quotes if any(q.get(key) for key in PROVENANCE_KEYS)]
    if not marked:
        return None  # mock — 표기가 없는 것이 정상이다

    half = [q for q in quotes if not all(q.get(key) for key in PROVENANCE_KEYS)]
    if half:
        return (
            f"시세 {len(half)}건에 관측 표기가 반쪽만 있다 (규격·관측일은 한 묶음이다) "
            f"— 관측일 없이 실측으로 분류하면 as_of 를 관측일인 것처럼 적게 된다"
        )

    dates = sorted({str(q["observed_date"]) for q in quotes})
    if len(dates) > 1:
        return (
            f"관측일이 하루가 아니다: {dates} — 등급마다 다른 날의 가격을 쓰면 "
            f"스프레드가 서로 다른 시점의 두 가격을 비교하게 된다 (규칙 4)"
        )

    try:
        observed = date.fromisoformat(dates[0])
        today = date.fromisoformat(as_of)
    except ValueError:
        # 죽지 않는다 (결정 d) — 파싱 실패도 "오늘 시세를 모른다"는 상태의 하나다.
        return f"관측일 {dates[0]!r} 을 날짜로 읽을 수 없어 언제 값인지 알 수 없다"

    if observed >= today:
        return (
            f"관측일 {dates[0]} 이 as_of 이후다 (as_of {as_of}) — 우리는 아침에 돌아서 "
            f"그 시각엔 그 값이 존재하지 않는다. look-ahead 라 쓰지 않았다"
        )

    limit = constraints["market_quotes"]["max_staleness_days"]
    gap = (today - observed).days
    if gap > limit:
        return (
            f"최신 경락 기록이 {dates[0]}로 {gap}일 전이다 (허용 {limit}일). "
            f"시장이 쉬었거나 적재가 밀린 것인데 어느 쪽이든 그만큼 오래된 값이라, "
            f"오늘 시세로 쓰지 않고 안을 만들지 않았다"
        )
    return None


def observed_date(quotes: list[dict[str, Any]]) -> str | None:
    """받은 시세의 관측일. mock 처럼 표기가 없으면 None."""
    dates = {str(q["observed_date"]) for q in quotes if q.get("observed_date")}
    return max(dates) if dates else None


def staleness_days(quotes: list[dict[str, Any]], as_of: str) -> int | None:
    """관측일이 as_of 로부터 며칠 전인가. 관측일 표기가 없으면 None (= 재지 않는다).

    mock 은 표기가 없어 항상 None 이다 — 회귀 경로가 이 검사를 만나지 않는다.
    """
    observed = observed_date(quotes)
    if observed is None:
        return None
    return (date.fromisoformat(as_of) - date.fromisoformat(observed)).days


def stale_quote_reason(
    quotes: list[dict[str, Any]], as_of: str, constraints: Mapping[str, Any]
) -> str | None:
    """너무 오래된 시세면 사유를 돌려준다 — ``provenance_problem`` 의 staleness 부분.

    🔴 **오래된 값을 당일인 척 쓰지 않는다** (규칙 3). 시장이 쉰 것이든 적재가 밀린 것이든,
      우리가 손에 든 값이 N일 된 값이라는 사실은 같다 — 둘을 구분할 방법도 없다. 그래서
      원인을 단정하지 않고 **며칠 된 값인지**만 말한다.

    임계는 ``constraints.market_quotes.max_staleness_days`` 다 (규칙 7).
    """
    return provenance_problem(quotes, as_of, constraints)


def observed_spec(quotes: list[dict[str, Any]]) -> str | None:
    """받은 시세가 어느 규격에서 온 것인지. mock 처럼 규격 표기가 없으면 None.

    **데이터가 말하게 한다** — 사유 문장이 여기서 규격을 가져간다.
    """
    labels = sorted({str(q["spec"]) for q in quotes if q.get("spec")})
    return " · ".join(labels) if labels else None


def quote_block_reason(
    quotes: list[dict[str, Any]],
    item: str,
    as_of: str,
    constraints: Mapping[str, Any],
) -> str | None:
    """오늘 시세를 쓸 수 없는 사유. 쓸 수 있으면 None.

    막히는 길이 둘이라 한 함수로 모은다 — ③과 ⑤가 **같은 판정**을 봐야 하기 때문이다.
    각자 판단하면 한쪽만 바뀌고, 그러면 ③은 안을 안 만들었는데 ⑤는 배분을 만드는(또는
    그 반대) 상태가 조용히 생긴다.

    1. 한 건도 못 받았다 — 휴장 · 그 규격 미거래 · 기록 판독 불가 · 규격 미확정
    2. 받았는데 너무 오래됐다 — ``max_staleness_days`` 초과
    """
    market = constraints["market_quotes"]["market_category"]
    usable = [quote for quote in quotes if quote["market"] == market]
    if not usable:
        return missing_quote_reason(item, as_of, constraints)
    return provenance_problem(usable, as_of, constraints)


def missing_quote_reason(item: str, as_of: str, constraints: Mapping[str, Any]) -> str:
    """시세를 한 건도 못 받은 날의 사유. **오케스트레이터가 원인을 알 수 있어야 한다.**

    ⚠️ 이 문장은 **DB 경로에서만 나온다.** mock 은 어느 앵커·품목에서도 빈 목록을 돌려주지
      않는다(모르는 품목이면 KeyError 로 멈춘다). 그래서 여기서 규격을 이름으로 말해도
      "안 쓴 규격을 썼다고 적는" 일이 생기지 않는다 — 계약 테스트가 그 전제를 잠근다.

    🔴 **"그날 휴장"이라고 말하지 않는다.** 쿼리가 ``auction_date < as_of`` 로 **전 기간**을
      훑어 최신일을 고르므로, 빈 결과는 그날 하루의 사정이 아니라 *"as_of 이전 어느 날에도
      그 좌표로 쓸 수 있는 기록이 없다"* 는 뜻이다. 쿼리를 바꾸면서 사유를 안 바꿔
      한동안 틀린 말을 하고 있었다 (Codex 2차 지적) — 이번 수정이 없애려던 바로 그 종류다.
    """
    market = constraints["market_quotes"]["market_category"]
    spec = spec_for_item(item, constraints)
    if spec is None:
        # 조사를 붙이지 않는다 — "피마늘는"이 실제로 나갔다 (2026-08-31 관통). 품목명이
        # 받침으로 끝나는지에 따라 은/는이 갈리는데, 그걸 코드가 판정하게 만들 이유가 없다.
        return (
            f"{item} 조회 규격이 아직 정해지지 않아 등급별 경락가를 받지 못했다 "
            f"— 시세 없이 매입 수량을 정할 수 없어 안을 만들지 않았다"
        )
    # 규격 이름도 **as_of 에 맞는 것**을 고른다. 2017년 무를 18kg 로 조회해 놓고
    # "20kg 규격에서"라고 적으면 무엇을 봤는지가 거짓이 된다 (Codex 2차 지적).
    label = _spec_label_on(spec, as_of)
    return (
        f"{as_of} 이전 기간에 {market} {label} 규격으로 쓸 수 있는 낙찰 기록이 없다 "
        f"— 그 좌표의 거래 자체가 없었거나, 있던 기록의 금액·중량을 읽을 수 없었다는 뜻이고, "
        f"보유 재고와는 무관하다. "
        f"시세 없이 매입 수량을 정할 수 없어 안을 만들지 않았다"
    )
