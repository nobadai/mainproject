"""매입 에이전트의 외부 입력 경계 — ports 6개 (상세설계 v1.1 §2).

**전 함수 as_of 필수** (CLAUDE.md 규칙 1 · 상세설계 §10). 백테스트 look-ahead 방어의
생명선이다. 과거 시점으로 돌릴 때 미래를 훔쳐보면 성적 자체가 무효가 된다.

**read-only** (규칙 2). 이 모듈은 읽기만 한다 — 어떤 함수도 DB에 쓰지 않는다.

**호출 위치는 ①~⑤가 T0 고정** (IO명세 §0 · 계약서 §8.1). 이 다섯은 T0 스냅샷 생성
시점에만 호출되고, T1 이후 노드는 스냅샷에서 읽는다. as_of 필터는 미래 정보를 막지만
T0~T2 동일 시점 내의 값 변동은 못 막으므로, 스냅샷 단일 소스로 일관성을 보장한다.
추상화 자체는 유지한다 — mock ↔ 스냅샷 ↔ DB 교체가 이 함수들의 내부 수정만으로 끝난다.

**⑥ get_context_docs만 런타임 호출 예외**다 (정의서 §3.1.1 · 팀 확인 2026-08-25).
② collect_context가 uncertain일 때 선택 로드한다 — 상세설계 §4-②. 근거와 잠정 상태는
그 함수 docstring에 적어두었다.

**수량 단위는 kg로 통일**(IO명세 v1.1 단위 통일 항목). 팀 표준(밴드·overlay·UI)이 kg이고,
``qty_kg × grade_unit_price(원/kg) = 원``으로 금액 변환 계수도 사라진다. mock을 채울 때
ton으로 되돌리지 않는다 — 숫자만 1000배 어긋나고 타입은 멀쩡해서 조용히 통과한다.

현재 단계: **mock 구현 완료**(백로그 E1-3). 각 함수는 ``mocks/``의 JSON을 IO명세 §1의
반환 형태로 materialize해 돌려준다. 시나리오는 ``as_of``로 고른다 —
``mocks/scenarios.json``의 앵커일 4개가 단위 테스트 4종에 대응한다.
DB/스냅샷으로 갈아끼울 때 바뀌는 건 이 파일의 본문뿐이고, 호출부는 그대로다.
"""

from collections.abc import Mapping
from datetime import date

from app.purchase_agent import mocks
from app.purchase_agent.quotes import QuoteSource
from app.purchase_agent.schemas import FIXED_MARKET


def get_forecast(item: str, as_of: date) -> dict:
    """가락 경락가 예측. ``generated_at <= as_of`` 최신 배치, **18일치**. (공급자: ML)

    지평이 18일인 건 D+18이 커버일수 D의 상한이기 때문이다 (상세설계 §7 · IO명세 §1-①).

    반환 형태 (IO명세 §1-①)::

        {"generated_at": "2026-08-21T06:00:00+09:00", "item": "배추", "unit": "원/kg",
         "current_price": 1650, "horizon_days": 18,
         "daily": [{"date": "2026-08-22", "predicted": 1666, "lower": 1616, "upper": 1716}],
         "model_version": "mock-v0"}

    ``daily``는 **D+1 ~ D+18 총 18건**이며 등급 차원이 없다 — 등급별 단가는 ⑤ 노드가
    ``get_market_quotes``와 결합해 만든다.

    에이전트가 여기서 파생 계산: ``ci_width = (upper - lower) / predicted``,
    ``rise_rate_2w``, ``peak_date``, 궤적 형태(지속상승/단봉/하락).
    """
    return mocks.load_forecast(item, as_of)


def get_market_quotes(
    item: str, as_of: date, *, source: QuoteSource | None = None
) -> list[dict]:
    """가락시장 등급별 **당일 실측** 시세(경락가). 예측이 아니라 관측값이다.

    반환 형태 (IO명세 §1-②)::

        [{"market": "가락", "grade": "특", "price": 1850},
         {"market": "가락", "grade": "상", "price": 1650},
         {"market": "가락", "grade": "중", "price": 1450}]

    ⑤ 등급 배분 스코어링의 원천. 최소 등급 2개 이상이어야 배분 판단이 성립한다.
    ``market``은 확장 여지로 남긴 필드이며 현재는 "가락" 고정 (8/20 결정 — 지방시장 제외).

    ★ **시세는 매입 자기 도메인이라 우리가 직접 읽는다** (정의서 §4.1 · #70). 실데이터
      공급자는 ``quotes.auction_quote_source()`` 이고, ``source`` 로 **명시 주입**한다 —
      환경변수 스위치를 두지 않는다. ``.env`` 가 결과를 좌우하면 로컬만 빨간 스위트가
      만들어지고, 그 상태는 이미 한 번 겪었다 (2026-08-31).

      ``None`` 이면 mock 이다. 회귀 테스트 전량이 그 길을 밟으므로 스위트가 DB 에 묶이지
      않는다. 실데이터 경로는 ``auction_quote_source()`` 를 꽂은 호출만 탄다.

    ⚠️ 실데이터 공급자는 **빈 목록을 돌려줄 수 있다** — 휴장일이거나 그날 그 규격의 낙찰이
      없는 날이다. mock 은 빈 목록을 돌려주지 않는다(모르는 품목이면 멈춘다). 빈 목록을
      받은 쪽은 죽지 않고 사유를 남긴다 (③ ``draft_plan``).
    """
    if source is not None:
        return _checked_quotes(source(item, as_of), item)
    return mocks.load_quotes(item, as_of)


def _checked_quotes(quotes: list[dict], item: str) -> list[dict]:
    """주입된 공급자가 **계약대로 돌려줬는지** 본다 (Codex 교차검증 2026-08-31).

    주입 지점은 배선 실수가 나기 좋은 자리인데, 그 실수가 조용히 지나간다:

    * ``market`` 키 누락 → ③에서 ``KeyError``. 어느 공급자가 범인인지 안 나온다
    * ``market="부산"`` → 하류 필터가 전부 떨어뜨려 **"가락 휴장"** 으로 보고된다.
      계약 위반이 날씨 얘기로 둔갑하는 셈이다
    * ``price`` 가 0·음수·실수 → 출력 경계(``grade_unit_price: int, gt=0``)에서 **제안
      전체**가 죽는다. 그때는 어느 줄 때문인지 알 수 없다

    ``check_prices_exist`` 는 이걸 못 잡는다 — **주입 원본과 대조**하기 때문에 원본이
    틀리면 같이 틀린 채 통과한다. 그래서 경계가 여기여야 한다.

    등급 어휘는 **보지 않는다.** 어느 등급을 쓸지는 #69 소관이고, ``schemas.py`` 도
    "DB 담당과 표준화 진행 중이라 Literal로 굳히지 않는다"고 열어둔 자리다.
    """
    for index, quote in enumerate(quotes):
        where = f"주입 시세[{index}] ({item})"
        if not isinstance(quote, Mapping):
            raise TypeError(f"{where}: 매핑이어야 한다, got {quote!r}")
        missing = [key for key in ("market", "grade", "price") if key not in quote]
        if missing:
            raise ValueError(f"{where}: 필수 키 없음 {missing} — IO명세 §1-② 형태여야 한다")
        if quote["market"] != FIXED_MARKET:
            raise ValueError(
                f"{where}: market={quote['market']!r} — {FIXED_MARKET} 고정이다. "
                f"다른 시장을 그대로 실으면 하류 필터가 전부 떨어뜨려 '휴장'으로 보고된다"
            )
        price = quote["price"]
        if isinstance(price, bool) or not isinstance(price, int) or price <= 0:
            raise ValueError(f"{where}: price={price!r} — 양의 정수 원/kg 이어야 한다")
    return quotes


def get_inventory(item: str, as_of: date) -> dict:
    """로트 단위 재고. (쓰기 주인: 물류)

    반환 형태 (IO명세 §1-③)::

        {"as_of": ..., "item": "배추",
         "lots": [{"lot_id": 12, "grade": "상", "stocked_at": "...",
                   "remaining_kg": 3000, "shelf_life_days": 10}],
         "warehouse_free_kg": 12000, "rental_cap_kg": 3600}

    에이전트가 파생 계산: 로트별 잔여신선도 = ``shelf_life_days - (as_of - stocked_at)``,
    납품 소요일 내 소진가능량, "신규 매입 시 기존 로트가 밀리는가".
    """
    return mocks.load_inventory(item, as_of)


def get_confirmed_orders(item: str, as_of: date, days: int = 14) -> dict:
    """확정 주문 (``status='confirmed'``, 향후 ``days``일). (쓰기 주인: 영업)

    반환 형태 (IO명세 §1-④)::

        {"as_of": ..., "item": "배추",
         "orders": [{"sale_id": 7, "qty_kg": 12000, "due_date": "..."}],
         "total_kg": 18000}

    용도: 기본 수요 산정(총량 + 안전재고), 그리고 **due_date별 분포 → 등급-신선도 매칭**
    (가까운 납품분은 중품 가능, 먼 납품분은 불가 등).
    """
    return mocks.load_orders(item, as_of, days)


def get_projected_cash_min(as_of: date, horizon_days: int) -> int:
    """향후 ``horizon_days``일 **최저 예상 현금**. (산출: 정산)

    ⚠️ 잔고가 아니다. 급여·원리금 등 확정 유출을 차감한 뒤의 최저점이다 — 잔고만 보면
    어느 날 갑자기 파산하는 운전자본 갭을 사전에 차단하지 못한다 (IO명세 §1-⑤).
    ``horizon_days``는 회수주기 이상이어야 의미가 있다.

    ⚠️ 2종이 존재한다. **우리(사이클 A T1)는 ``base_projected_cash_min``을 참조**한다
    (H1 승인 후 재계산분인 ``post_h1_``이 아니다).
    """
    return mocks.load_cash(as_of, horizon_days)


def get_snapshot_extras(item: str, as_of: date) -> dict:
    """T0 스냅샷 중 위 6개 포트로 오지 않는 입력 3종 (상세설계 §3 State).

    ``item_mix_ratio`` · ``contract_price`` · ``margin_defense_floor_rate``.

    ⚠️ **IO명세 §1의 계약 포트가 아니다.** §1은 6개를 규정하고 이 함수는 거기 없다.
    상세설계 §11 선행확인이 "T0 스냅샷에 item_mix_ratio·contract_price·방어선 2값 포함
    (현서님 협의)"를 **미완료**로 두고 있어, 형식이 정해지기 전까지의 잠정 경계다.

    그럼에도 ports에 두는 이유: 이것도 **외부 입력**이라 규칙 2("외부 데이터는 ports 함수로만
    받는다")가 적용된다. state.py가 mocks를 직접 읽으면 mock → 스냅샷 → DB 교체가 ports
    안쪽에서 끝나지 않는다. 스냅샷 형식이 확정되면 이 함수가 §1의 7번째 항목이 되거나,
    기존 포트에 흡수되어 사라진다.
    """
    return mocks.load_snapshot_extras(item, as_of)


def get_context_docs(item: str, as_of: date, doc_types: list[str]) -> list[dict]:
    """문서 컨텍스트 로드. 검색이 아니라 **선택 로드**다 (② 노드, uncertain일 때만 호출).

    ⚠️ **T0가 아니라 ② collect_context의 런타임 호출이다 — 이 포트만의 예외**
    (정의서 §3.1.1 · 팀 확인 2026-08-25 · IO명세 §0). 나머지 ①~⑤는 T0 only이고
    ``state.build_initial_state``가 한 번씩 부른다. 오케스트레이터 스냅샷에 문서는 없다.

    예외가 안전한 이유:

    1. ``published_at <= as_of``로 고정된 **불변 발행물**이라 사이클 중 값이 변하는
       상태 데이터가 아니다 — T0~T2 시점 차로 숫자가 달라질 여지 자체가 없다
    2. 읽는 쪽이 **매입 하나뿐**이라(계약서 §0) 에이전트 간 숫자 불일치가 생길 수 없다
    3. look-ahead는 ``as_of`` 필터가 막는다 (아래 참조)

    ⚠️ **상태: 잠정.** 정의서 §3.1.1의 "여기 없는 값을 개별 조회하면 §1.2-9 위반" 문구에
    형식상 걸려서, documents 예외 명문화가 **팀 안건으로 진행 중**이다(현서님 발의).
    확정되면 이 표시를 지운다.

    ``doc_types``: ["관측월보", "기상", "작년동기"] 중 요청분.

    ⚠️ ``published_at <= as_of`` 필터 필수 — look-ahead 방어의 생명선. 발행일 없는 문서는
    적재 자체를 거부한다 (IO명세 §1-⑥).

    코퍼스가 작으므로 벡터 검색 없이 전문을 통째로 주입한다 (상세설계 §2).
    """
    return mocks.load_documents(item, as_of, doc_types)
