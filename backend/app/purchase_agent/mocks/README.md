# mock 데이터 (백로그 E1-1 · E1-2)

**전부 시뮬레이션 값이다.** 실측이 아니고, 실제 KREI·기상청 발간물도 아니다.
정의서 §7.3 기준으로 `SIM_FIXED` / `ASSUMED` 등급이며, 하드 제약 계산에 쓰일 때는
그 등급을 그대로 실어 보내야 한다.

## 왜 있는가

장식용 샘플이 아니라 **노드 단위 테스트 4종의 입력**이다 (CLAUDE.md "작업 방식").
`tests/test_purchase_agent/test_mocks.py`가 아래 조건을 데이터에 직접 검증한다 —
누가 숫자를 만져 시나리오 이름과 내용이 어긋나면 그 순간 빨간불이 뜬다.

| as_of (앵커일) | 시나리오 | forecast | quotes | 기대 산출 | 데이터가 만족하는 조건 |
|---|---|---|---|---|---|
| **2026-08-21** | `mock_rising` | rising | normal | 선매입(D 큰 안) 등장 | `ci_width` 0.060 < 0.08 · `rise_rate_2w` +14% ≥ 0.10 |
| **2026-08-28** | `mock_falling` | falling | normal | 최소 매입(D=2) | `ci_width` 0.060 < 0.08 · `rise_rate_2w` −12% |
| **2026-09-04** | `mock_uncertain` | uncertain | normal | 보수·기본 2안만 | `ci_width` 0.120 ≥ 0.08 |
| **2026-09-11** | `grade_spread_wide` | rising | wide | 중품 비중 상승 | 상-중 스프레드 21.2% = 평시 12.1% × 1.75 |

임계 출처: `ci_width_threshold` 0.08 · `pre_purchase_rise_rate` 0.10 ·
`grade_spread_widening_ratio` 0.50 — 전부 `constraints.yaml`.

## as_of가 시나리오 키인 이유

포트 시그니처는 계약으로 확정돼 있어 `scenario` 인자를 넣을 수 없다
(`get_forecast(item, as_of)`). 전역 스위치나 환경변수 대신 **날짜에 시나리오를 배정**했다:

- 시그니처를 안 건드린다. 테스트는 `as_of`만 바꾸면 시나리오가 바뀐다
- **상태가 없다** — 테스트 간 오염이 없고 read-only 원칙(규칙 2)과도 맞는다
- 실제로도 날짜마다 다른 예측이 온다. mock → 스냅샷 → DB로 갈아끼울 때 호출부가 그대로다

앵커일이 아닌 날짜로 부르면 `KeyError`가 난다. 빈 값을 돌려주면 노드가 0으로 계산한다.

## 2026-08-21이 특별한 이유 — 문서 예시가 모이는 날

IO명세·상세설계의 예시들이 흩어져 보이지만 **한 날의 스냅샷**이다:

- 재고 예시 로트: `stocked_at 8/17` + `shelf_life_days 10` → as_of 8/21이면 **잔여 6일**
- 픽스처 `risks`: *"중품 1,500kg은 잔여신선도 **6일** 내 소진 필요"* ← 같은 6
- 주문 예시: `8/24 12,000kg` / `8/29 6,000kg`
- 상세설계 §4-⑤: *"**8/24 납품 12,000kg**에는 배정 가능, **8/29 납품분**은 상품으로"* ← 그대로
- 시세 예시: 특 1,850 / 상 1,650 / 중 1,450 ← 픽스처 `sourcing_plan` 단가가 여기 실재

그래서 이 날의 mock은 문서 예시를 **글자 그대로** 재현한다. 테스트가 그 일치를 지킨다.

## 파일

| 파일 | 내용 |
|---|---|
| `scenarios.json` | as_of → {forecast, quotes} 매핑 (위 표) |
| `forecast_{rising,falling,uncertain}.json` | 4품목 × D+1~D+18 |
| `quotes_{normal,wide}.json` | 4품목 특/상/중 (market="가락" 고정) |
| `inventory.json` · `orders.json` | 품목별 고정값, 날짜는 as_of 상대 오프셋 |
| `cash.json` | horizon_days → projected_cash_min (`base_` 계열) |
| `documents.json` | 관측월보 스타일 4건, `published_at` 필수 |
| `_load.py` | 위 JSON을 IO명세 §1 반환 형태로 materialize |

`_`로 시작하는 JSON 키(`_scenario` · `_설명` 등)는 **설명용이며 포트 반환값에 실리지 않는다.**

## 날짜를 오프셋으로 저장하는 이유

리터럴 날짜를 쓰면 9/11 시나리오에서 "8/24 납품"이 3주 전이 되어 등급-신선도 매칭이
무의미해진다. `_load.py`가 `as_of`를 더해 실날짜를 만든다 — 그래서 mock 어디에도
`date.today()`가 없다 (규칙 1).

## 값을 고칠 때 지켜야 할 것

- **수량은 전부 정수 kg.** ton 값이 섞이면 타입은 멀쩡한데 숫자만 1000배 틀려서 조용히
  통과한다. 테스트가 모든 `*_kg` 필드의 **자릿수 범위**(1,000 ~ 1,000,000)를 검사한다
- **가격은 전부 정수 원/kg** (100 ~ 20,000 범위 검사)
- **미결 파라미터(`inbound_lead_days` · `purchase_payment_days`)를 mock에 넣지 않는다.**
  0으로 슬쩍 들어오는 경로를 원천 차단한다 — 규칙 3. 테스트가 부재를 검사한다
- 예측 궤적은 손으로 고치지 말고 `_scenario`의 `rise_rate_2w` · `ci_band` · `shape`가
  뜻하는 규칙과 함께 바꾼다. 규칙과 숫자가 어긋나면 시나리오 테스트가 잡는다

## `ci_width` 판정 기준일 — D+14 단일 (확정)

상세설계 §4-①이 판정 기준일을 **D+14 단일**로 확정했다 (`constraints.yaml`의
`situation.ci_judgment_day: 14`). `daily`가 D+1부터 시작하므로 **index 13**이다.

이 mock은 `ci_width`를 18일 **전 구간에서 일정하게** 유지한다. 기준일이 미정이던 시기의
우회책이었지만, 확정 후에도 그대로 둔다 — 판정일 하나만 맞춰두면 "그날만 우연히 통과하는"
데이터가 되어 기준일이 바뀌는 순간 시나리오가 조용히 뒤집힌다. 밴드를 고르게 두면
`max`/`min` 검사가 성립하고, 어느 날로 바꿔도 시나리오 의미가 유지된다.

다만 **D+14가 실제 판정에 쓰이는 값이라는 사실**은 테스트가 별도로 못 박는다
(`test_judgment_day_row_is_the_fourteenth_calendar_day` — 날짜로 되짚어 index 매핑을 검증).
