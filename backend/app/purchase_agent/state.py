"""그래프가 들고 다니는 상태와 그 초기값 (상세설계 v1.1 §3).

**필드는 §3 State 정의를 그대로 옮긴 것이다.** 임의로 늘리지 않는다 — 이 State는 노드끼리만
쓰는 내부 구조가 아니라 설계 문서가 규정한 계약이고, 필드가 늘면 어느 노드가 무엇을 읽는지
문서로 추적할 수 없게 된다.

**노드는 State 전체가 아니라 바꿀 키만 담은 dict를 반환한다** — LangGraph 런타임이 병합한다.
그래서 이 TypedDict가 ``total=True``인데도 부분 갱신이 성립한다.

**포트 ①~⑤는 여기서 한 번만 부른다.** ``ports.py`` docstring이 약속한 "T0 스냅샷 생성
시점에만 호출되고 T1 이후 노드는 스냅샷에서 읽는다"가 이 모듈이다. 노드가 직접 ports를 부르면
같은 사이클 안에서 값이 달라질 수 있고(T0~T2 시점 차), 그러면 사중 일치가 무너진다.

**⑥ 문서 포트만 예외다** — ② collect_context가 런타임에 호출한다 (정의서 §3.1.1 ·
팀 확인 2026-08-25 · IO명세 §0). 문서는 상태 데이터가 아니라 ``published_at <= as_of``로
고정된 불변 발행물이라 사이클 중 값이 변하지 않는다.
"""

from datetime import date
from typing import Literal, NotRequired, TypedDict

from app.purchase_agent import ports
from app.purchase_agent.config import load_constraints
from app.purchase_agent.quotes import QuoteSource


class PurchaseAgentState(TypedDict):
    """상세설계 §3. 주석의 ★는 문서 원문 표기를 그대로 옮긴 것이다."""

    # ── 입력 (T0 스냅샷에서 주입) ───────────────────────────────────────────
    date: str  # as_of. 노드는 이 값만 보고, 벽시계를 읽지 않는다 (규칙 1)
    item: Literal["배추", "무", "피마늘", "양파"]
    forecast: dict  # 경락가 예측 (daily는 D+1 ~ D+18)
    market_quotes: list[dict]  # 가락 등급별 당일 경락가
    inventory: dict
    confirmed_orders: dict
    item_mix_ratio: dict  # ★ 품목 비중 (mix 축 게이팅용)
    # ★ 계약단가 (참조값 — 마진 표시용, 컷 아님). **미수령이면 None**이고, 그때
    # margin_warning·expected_margin_rate가 함께 null로 나간다 (IO명세 §2 동기화 규칙).
    contract_price: float | None
    margin_defense_floor_rate: float  # ★ 구간별 방어선 (참조값)
    projected_cash_min: int  # ★ 향후 N일 최저 현금 (재무 base_projected_cash_min)
    feedback: dict | None  # 오케스트레이터 재조정 요청 (§6, 전부 기각 시만)

    # ── 재무 수신값 (어댑터 경로에만 실린다) ────────────────────────────────
    # **넷 다 선택 필드다.** ``build_initial_state``(mock 경로)는 채우지 않으므로
    # ``.get()``이 None을 돌려주고 노드는 종전 경로로 간다 — 949건이 그대로 도는 근거다.
    # 어댑터 경로에서만 값이 실리고, 그때 계산이 달라진다 (IO명세 §2-B).
    #
    # ``finance_cap_amount_krw``: 재무가 낸 **최종 매입 상한(원)**. 이 값이 오면
    # ``cash.max_purchase_ratio``를 곱하지 않는다 — 같은 목적으로 두 번 조이면
    # "왜 이만큼밖에 못 사나"의 근거가 흐려진다 (재무 회신 v2.2.1 · B6).
    finance_cap_amount_krw: NotRequired[int | None]
    # ``purchase_payment_days``: N5. 7 확정 (8/27 재무 · calendar day · 영업일 보정 없음).
    # mock 경로는 여전히 None이라 지급일 계산이 보류된다 (규칙 3).
    purchase_payment_days: NotRequired[int | None]
    # ``inbound_lead_days``: N4. 입고 리드타임(일). **도착일 = 회차일 + N4**이고, 도착일이
    # 없으면 ⑥의 회차별 ``cap_by_date`` 검사가 성립하지 않는다 (#58).
    #
    # 물류가 ``constraints.inventory`` 안에 담아 보내므로 ``absorb_inventory``의 통째 복사로
    # ``state["inventory"]``에는 이미 들어와 있었다. 그런데 ``pending_value``는 **State
    # 최상위**를 보므로, 값이 와 있어도 못 찾아 "N4 미확정"을 고지하고 있었다 —
    # "수신값이 설정값을 이긴다"는 규칙이 이 키에서만 작동하지 않던 자리다.
    inbound_lead_days: NotRequired[int | None]
    # ``critical_payment_dates``: 지급 집중일. **겹침 경고에만 쓴다** — 날짜별 잔액
    # 재계산은 재무 SCENARIO_VALIDATION 소관이다 (도메인 침범 + 이중 계산).
    critical_payment_dates: NotRequired[list[str] | None]

    # ── 마스터 되먹임 수신값 (어댑터 경로 · 2회차부터) ──────────────────────
    # 재무 수신값 넷과 **같은 방식**이다 — 선택 필드라 ``build_initial_state``(mock 경로)는
    # 채우지 않고, 어댑터 경로에서만 값이 실린다.
    #
    # 🔴 **``feedback`` 슬롯과 섞지 않는다** (되먹임 계약 v0.2 §2). 셋이 다르다::
    #
    #     수명   ``feedback``(사용자 조건)은 실행 단위 · 되먹임은 회차 단위
    #     모양   자연어 하나 vs 구조화 배열
    #     권위   사람 → 제안자 vs 조언자 → 제안자
    #
    #   v0.1 은 한 슬롯에 ``source`` 로 갈랐는데, **payload 의 타입이 source 값에 딸려
    #   가서** 계약이 아니라 관례가 됐다. 마스터가 두 칸으로 나눠 보내므로
    #   (``flow.py`` ``_purchase_input``) 받는 쪽도 두 칸으로 받는다.
    #
    # ⚠️ **지금은 받기만 한다.** 어느 노드도 이 값을 읽어 수량을 바꾸지 않는다 —
    #   ``target_value`` 가 *"이 값으로 바꿔라"* 인지 *"이 값을 넘지 마라"* 인지가
    #   미확정이라 반영 규칙을 만들 수 없다. **받았는데 안 쓴다는 사실은 ⑥이 risks 에,
    #   건수는 ⑦이 meta 에 적는다** — 값을 받고 조용히 버리면 보내는 쪽은 알 수 없다.
    #
    # ``adjustments``: 부서 조정안 표준형(``SuggestedAdjustment``)을 편 dict 목록.
    adjustments: NotRequired[list[dict] | None]
    # ``feedback_context``: 그 회차의 방아쇠 — attempt · reason · findings ·
    # verdicts · verdict_reasons. 마스터가 1회차 산출물에서만 만든다.
    feedback_context: NotRequired[dict | None]

    # ── 중간 산출 ───────────────────────────────────────────────────────────
    situation: Literal["stable", "uncertain"]
    context_docs: list[dict]  # 주입된 문서 (published_at <= as_of)
    context_loop_count: int  # max 3

    #: 🟢 **문서를 못 읽은 사유.** 비어 있으면 정상이다 (2026-09-04 · 마스터 결정).
    #:
    #:   `context_docs == []` 는 두 뜻이 될 수 있다.
    #:
    #:     그날 그 유형의 문서가 없다        정상 — 무·양파가 그렇다 (이 칸 안 참)
    #:     읽으려다 못 읽었다                실 소스 없어 mock 이 막힘 (이 칸이 참)
    #:
    #: ★ **둘 다 안을 막지 않는다.** 마스터가 *"문서 없으면 없이 진행"* 으로 정했다.
    #:   이 칸이 차면 ⑦ `self_check` 이 각 안의 risks 에 **고지만** 붙인다 (컷 아님).
    context_unavailable: NotRequired[str]
    allowed_axes: list[str]  # ★ 그날 허용 strategy_type (규칙 계산)
    coverage_days: int  # ★ 커버일수 D
    base_plan: dict  # 수량·타이밍 초안
    split_plan: list[dict] | None  # 분할 계획 (timing 축)
    sourcing_plan: list[dict]  # 등급 배분

    # ── 출력 ────────────────────────────────────────────────────────────────
    scenarios_final: list[dict]
    confidence: Literal["high", "medium", "low"]
    rejected_reasons: list[dict]  # {label, reason} — 출력 스키마와 동일 형 (v1.1 정정)
    proposal: dict | None  # ⑦이 조립·재검증한 최종 산출물


def build_initial_state(
    item: str,
    as_of: date,
    *,
    feedback: dict | None = None,
    quotes: QuoteSource | None = None,
) -> PurchaseAgentState:
    """T0 스냅샷을 만든다 — **포트 ①~⑤를 한 번씩 호출한다 (T0 only)**.

    ⑥ ``get_context_docs``는 여기서 부르지 않는다. 문서 포트만 ② collect_context가
    **런타임에 호출하는 예외**다 (정의서 §3.1.1 · 팀 확인 2026-08-25 · IO명세 §0).
    예외가 안전한 이유는 ``ports.get_context_docs`` docstring에 적어두었다.

``item_mix_ratio`` · ``contract_price`` · ``margin_defense_floor_rate``는 IO명세 §1의
    계약 포트 6개에 없다. 그래도 **외부 입력이므로 ports를 거친다**(규칙 2) —
    ``get_snapshot_extras``가 그 잠정 경계이고, 스냅샷 형식이 확정되면 거기서만 바뀐다.

    중간 산출 필드는 채우지 않는다 — 각 노드가 자기 몫을 반환한다. 다만 ``context_docs``와
    ``context_loop_count``는 stable 경로에서 ② 노드를 건너뛰므로 빈 값으로 시작해야 하고,
    ``rejected_reasons``는 어느 노드든 append할 수 있어야 하므로 빈 목록으로 둔다.
    ``coverage_days``·``situation`` 같은 값은 **0이나 빈 문자열로 채우지 않는다** — 미결과
    확정된 값을 구분해야 하기 때문이다 (규칙 3).

    ``quotes``는 등급별 시세 공급자다 (#70). ``None``이면 mock 이고, 실데이터로 돌리려면
    ``quotes.auction_quote_source()``를 넘긴다 — **환경변수가 아니라 명시 주입**이다.
    시세만 주입 지점을 여는 이유: 나머지 다섯은 마스터 봉투가 실어 보내거나(어댑터 경로)
    mock 이고, **시세만 매입 자기 도메인이라 우리가 직접 읽는다** (정의서 §4.1).
    """
    constraints = load_constraints()
    # 창·지평을 파라미터로 받지 않는다. 여기서 쓴 창과 ③이 나눌 창이 달라지면 수량이
    # 조용히 틀어지므로, 양쪽 모두 constraints.yaml 한 곳에서 읽는다 (규칙 7).
    order_days = constraints["demand"]["order_window_days"]
    cash_horizon_days = constraints["cash"]["horizon_days"]
    extras = ports.get_snapshot_extras(item, as_of)
    return {  # type: ignore[return-value]  # 중간·출력 필드는 노드가 채운다
        "date": as_of.isoformat(),
        "item": item,
        "forecast": ports.get_forecast(item, as_of),
        "market_quotes": ports.get_market_quotes(item, as_of, source=quotes),
        "inventory": ports.get_inventory(item, as_of),
        "confirmed_orders": ports.get_confirmed_orders(item, as_of, days=order_days),
        "item_mix_ratio": extras["item_mix_ratio"],
        "contract_price": extras["contract_price"],
        "margin_defense_floor_rate": extras["margin_defense_floor_rate"],
        "projected_cash_min": ports.get_projected_cash_min(as_of, cash_horizon_days),
        "feedback": feedback,
        "context_docs": [],
        "context_loop_count": 0,
        "rejected_reasons": [],
        "proposal": None,
    }
