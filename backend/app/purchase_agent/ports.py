"""매입 에이전트의 외부 입력 경계 — ports 6개 (상세설계 v1.1 §2).

**전 함수 as_of 필수** (CLAUDE.md 규칙 1 · 상세설계 §10). 백테스트 look-ahead 방어의
생명선이다. 과거 시점으로 돌릴 때 미래를 훔쳐보면 성적 자체가 무효가 된다.

**read-only** (규칙 2). 이 모듈은 읽기만 한다 — 어떤 함수도 DB에 쓰지 않는다.

**호출 위치는 T0 고정** (IO명세 §1 각주 · 계약서 §8.1). ports는 T0 스냅샷 생성 시점에만
호출되고, T1 이후 노드는 스냅샷에서 읽는다. as_of 필터는 미래 정보를 막지만 T0~T2 동일
시점 내의 값 변동은 못 막으므로, 스냅샷 단일 소스로 일관성을 보장한다. 추상화 자체는
유지한다 — mock ↔ 스냅샷 ↔ DB 교체가 이 함수들의 내부 수정만으로 끝난다.

현재 단계: **시그니처만 확정**. mock 구현은 백로그 E1-3에서 채운다.
"""

from datetime import date

_NOT_YET = "E1-3(mock 구현)에서 채운다 — 지금은 시그니처만 확정된 상태다"


def get_forecast(item: str, as_of: date) -> dict:
    """가락 경락가 예측. ``generated_at <= as_of`` 최신 배치, 21일치. (공급자: ML)

    반환 형태 (IO명세 §1-①)::

        {"generated_at": ..., "item": "배추", "unit": "원/kg", "current_price": 780,
         "horizon_days": 21,
         "daily": [{"date": "...", "predicted": 795, "lower": 760, "upper": 830}],
         "model_version": "mock-v0"}

    에이전트가 여기서 파생 계산: ``ci_width = (upper - lower) / predicted``,
    ``rise_rate_2w``, ``peak_date``, 궤적 형태(지속상승/단봉/하락).
    """
    raise NotImplementedError(_NOT_YET)


def get_market_quotes(item: str, as_of: date) -> list[dict]:
    """가락시장 등급별 **당일 실측** 시세(경락가). 예측이 아니라 관측값이다.

    반환 형태 (IO명세 §1-②)::

        [{"market": "가락", "grade": "특", "price": 920},
         {"market": "가락", "grade": "상", "price": 850},
         {"market": "가락", "grade": "중", "price": 720}]

    ⑤ 등급 배분 스코어링의 원천. 최소 등급 2개 이상이어야 배분 판단이 성립한다.
    ``market``은 확장 여지로 남긴 필드이며 현재는 "가락" 고정 (8/20 결정 — 지방시장 제외).
    """
    raise NotImplementedError(_NOT_YET)


def get_inventory(item: str, as_of: date) -> dict:
    """로트 단위 재고. (쓰기 주인: 물류)

    반환 형태 (IO명세 §1-③)::

        {"as_of": ..., "item": "배추",
         "lots": [{"lot_id": 12, "grade": "상", "stocked_at": "...",
                   "remaining_ton": 3.0, "shelf_life_days": 10}],
         "warehouse_free_ton": 12.0, "rental_cap_ton": 3.6}

    에이전트가 파생 계산: 로트별 잔여신선도 = ``shelf_life_days - (as_of - stocked_at)``,
    납품 소요일 내 소진가능량, "신규 매입 시 기존 로트가 밀리는가".
    """
    raise NotImplementedError(_NOT_YET)


def get_confirmed_orders(item: str, as_of: date, days: int = 14) -> dict:
    """확정 주문 (``status='confirmed'``, 향후 ``days``일). (쓰기 주인: 영업)

    반환 형태 (IO명세 §1-④)::

        {"as_of": ..., "item": "배추",
         "orders": [{"sale_id": 7, "quantity_ton": 12, "due_date": "..."}],
         "total_ton": 18}

    용도: 기본 수요 산정(총량 + 안전재고), 그리고 **due_date별 분포 → 등급-신선도 매칭**
    (가까운 납품분은 중품 가능, 먼 납품분은 불가 등).
    """
    raise NotImplementedError(_NOT_YET)


def get_projected_cash_min(as_of: date, horizon_days: int) -> int:
    """향후 ``horizon_days``일 **최저 예상 현금**. (산출: 정산)

    ⚠️ 잔고가 아니다. 급여·원리금 등 확정 유출을 차감한 뒤의 최저점이다 — 잔고만 보면
    어느 날 갑자기 파산하는 운전자본 갭을 사전에 차단하지 못한다 (IO명세 §1-⑤).
    ``horizon_days``는 회수주기 이상이어야 의미가 있다.

    ⚠️ 2종이 존재한다. **우리(사이클 A T1)는 ``base_projected_cash_min``을 참조**한다
    (H1 승인 후 재계산분인 ``post_h1_``이 아니다).
    """
    raise NotImplementedError(_NOT_YET)


def get_context_docs(item: str, as_of: date, doc_types: list[str]) -> list[dict]:
    """문서 컨텍스트 로드. 검색이 아니라 **선택 로드**다 (② 노드, uncertain일 때만 호출).

    ``doc_types``: ["관측월보", "기상", "작년동기"] 중 요청분.

    ⚠️ ``published_at <= as_of`` 필터 필수 — look-ahead 방어의 생명선. 발행일 없는 문서는
    적재 자체를 거부한다 (IO명세 §1-⑥).

    코퍼스가 작으므로 벡터 검색 없이 전문을 통째로 주입한다 (상세설계 §2).
    """
    raise NotImplementedError(_NOT_YET)
