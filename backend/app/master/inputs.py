"""마스터가 실어 주는 입력 3종 — **어디서 왔는지를 값과 함께 들고 다닌다.**

정의서 §3.2.5 의 명시적 예외다. 이 셋은 *"해당 에이전트에게 요청"* 이 성립하지 않아
마스터가 직접 싣는다.

```text
forecast          ML 은 호출 구조 밖 독립 실행이라 부를 대상이 없다
confirmed_orders  1차 판매는 에이전트가 아니라 마스터 관할 Rule 이다
policy_values     정책 테이블 — 운반 주체 미결 (M-19)
```

★ **값만 싣지 않고 `SourcedInput` 으로 싣는다.** 같은 `forecast` 라도 오늘 실제 DB 에서
  읽은 것과 mock 파일에서 온 것은 **의사결정의 무게가 다르다.** 값만 넘기면 그 차이가
  사라지고, 리포트를 읽는 사람은 전부 실측으로 읽는다. §3.7.6("못 한 것을 한 척하지
  않는다")이 검증 커버리지에 대해 말하는 것과 같은 이야기다.

★ **비어 있으면 지어내지 않는다.** 못 읽으면 `MISSING` 으로 두고 매입이
  `missing_data` 로 답하게 한다 — 0 이나 평균값으로 메우면 **그럴듯하게 틀린 계획**이
  나온다.

🟢 **`MOCK` 다리를 걷었다** (2026-09-03).

  앵커가 어긋나 있던 동안(`M-24`) `forecast` 가 mock 으로 떨어졌다. `D-2` 가
  `2025-12-31` 로 확정되면서 세 품목 전부 실 예측이 선다 — 실측으로 확인했다.

  .. code-block:: text

      배추 · 무 · 양파   grade=MEASURED   v_ml_price_forecast(as_of=2025-12-31, AUC)

🔴 **그리고 다시 놓지 않는다.** ML DB 가 죽었는데 mock 으로 돌면 **장애가 정상으로
  보인다.** 그 갈래가 마스터 실측을 두 번 오염시켰다 (2026-08-31 · 09-03 피마늘).

  이제 못 읽으면 `MISSING` 이고, 매입이 `missing_data: ["forecast"]` 로
  `RUNTIME_NOT_READY` 를 낸다. **못 한 것이 한 것으로 안 보인다.**

★ `MOCK` 은 어휘에 남긴다. 만드는 곳이 지금은 없지만, 새 다리가 생기면 그것이
  스스로 `MOCK` 이라고 말할 자리가 있어야 하고 그때 `ProcurementFlow` 가 세운다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from psycopg import sql

from app.contracts.core import ITEMS
from app.finance.db import fetch_all, fetch_one, get_db_schema

#: 값 하나의 출처 등급. **리포트에 그대로 나간다.**
Grade = Literal[
    "MEASURED",  # 실제 운영 DB 에서 그대로 읽었다
    "DERIVED",  # 실제 DB 값에서 규칙으로 파생했다 — 원식을 함께 남긴다
    "MOCK",  # 🔴 mock 파일에서 왔다 — 한시 조치
    "MISSING",  # 못 구했다. 지어내지 않고 비운다
]

#: 🔴 **요청 본문이 직접 준 값**의 등급 (매입 실측 2026-09-07).
#:
#: `Grade` 에 넣지 않는다 — `Grade` 는 *"적재층이 어디서 읽었는가"* 이고, 이것은
#: **적재층을 아예 안 탔다**는 사실이라 같은 축이 아니다. `SourcedInput` 이 생기지도
#: 않는 자리라 등급을 붙일 대상이 없다.
#:
#: ★ 그래도 출처표(`input_sources`)에는 같은 `등급:소스` 모양으로 나간다 — 화면과
#:   부서 payload 가 한 표를 읽기 때문이다.
REQUEST_GRADE = "REQUEST"

#: 🔴 **요청이 안 줘서 마스터 기본값으로 떨어진 값**의 등급 (`#531` 후속 · 2026-09-10).
#:
#: `REQUEST_GRADE` 와 같은 이유로 `Grade` 에 안 넣는다 — 적재층을 아예 안 탔다.
#: 다른 점은 **누가 값을 정했나** 하나다.
#:
#: ```text
#: REQUEST:<key>   요청 본문이 줬다
#: DEFAULT:<이름>  요청이 안 줘서 **마스터가 자기 기본값을 썼다**
#: ```
#:
#: 🔴 **왜 적나.** 기본값을 조용히 두면 *"말 안 하고 번인에 쌓는"* 길이 그대로
#: 남는다. `sim_run_id` 가 정확히 그 자리다 — 안 주면 번인 상수로 앉는데, 그 사실이
#: 아무 데도 안 적히면 나중에 읽는 사람이 **누가 그 실행을 골랐는지** 알 수 없다.
#:
#: ★ **키를 빼는 것으로 대신하지 않는다.** 빼면 「없다」와 「모른다」가 섞인다 —
#:   `_input_sources` 가 봉투에 대해 이미 같은 결론을 냈다 (`MISSING:-`).
DEFAULT_GRADE = "DEFAULT"


def injected_keys(sources: Any) -> tuple[str, ...]:
    """출처표에서 **주입분만** 추린다 — 화면 문구가 읽는 자리.

    ★ `ProcurementRunResponse` 에 칸을 새로 만들지 않는다. 같은 사실의 주인은
      `input_sources` 하나이고, 화면은 그것을 읽어 문장으로 옮기기만 한다.

    ★ **`mocked_inputs` 와 섞지 않는다.** 그것은 `grade == "MOCK"` 만 세고 실행을
      세우는 데 쓴다. 주입은 세울 일이 아니라 적을 일이다.
    """
    prefix = f"{REQUEST_GRADE}:"
    return tuple(key for key, source in (sources or {}).items() if str(source).startswith(prefix))


#: 확정 주문을 내다볼 기간. 매입 ③이 `total_kg ÷ order_window_days` 로 일수요를 낸다.
_ORDER_WINDOW_DAYS = 14


# ── 시세 계열 ───────────────────────────────────────────────────────────
#
# 🔴 **매입과 판매가 같은 시세를 보고 있었다** (2026-09-11 · 걷기 실측).
#
#   `load_forecast` 하나를 두 경로가 같이 쓰는데 조회에 계열이 박혀 있어서
#   **경매가로 사서 경매가로 팔았다.** 완주한 걷기 `SIM-CHAIN-V3`(1~3월)에서
#   `SALES_MARGIN_BELOW_MINIMUM` 이 511건 중 483건이고, 팔린 일곱 건이 전부 무였다.
#
#   ```text
#   품목    AUC(경매)   WHSL(중도매)   차이     단위
#   배추      643        1,152        +79%    둘 다 원/kg
#   무        558          854        +53%
#   양파      834        1,022        +23%
#   ```
#
# ★ **어휘의 주인은 ML 이다** (`app.ml.schemas.TargetKind` — `AUC` · `WHSL` · `RTL`).
#   마스터는 **고르기만 하고 새로 만들지 않는다.**
#
# ⚠️ **`RTL`(소매)은 안 쓴다.** 단위가 `원/단위` 이고 `unit_weight_kg` 가 전부 비어
#   있어 kg 로 못 바꾼다. 사람이 그 사실을 보고 중도매로 정했다.

#: 매입이 읽는 계열. **경매에서 산다.**
PROCUREMENT_TARGET_KIND = "AUC"

#: 판매가 읽는 계열. **중도매로 판다 — 고객이 김치공장이다.**
#:
#: 🔴 같은 값을 여기 말고 다른 데 적지 않는다. 리터럴을 흩뿌리면 **왜 그 값인지**가
#:   사라지고, 한 자리만 고친 날 매입과 판매가 다시 같은 시세를 본다.
SALES_TARGET_KIND = "WHSL"


@dataclass(frozen=True)
class SourcedInput:
    """값 + 출처. **둘을 떼어 놓지 않는다.**"""

    key: str
    payload: Any | None
    grade: Grade
    source: str
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.payload is not None and self.grade != "MISSING"

    def line(self) -> str:
        tail = f" — {self.note}" if self.note else ""
        return f"{self.key} [{self.grade}] {self.source}{tail}"


@dataclass(frozen=True)
class MasterInputs:
    """한 실행이 실어 주는 것 전부."""

    forecast: SourcedInput
    confirmed_orders: SourcedInput
    policy_values: SourcedInput

    def all(self) -> tuple[SourcedInput, ...]:
        return (self.forecast, self.confirmed_orders, self.policy_values)

    def sources(self) -> dict[str, str]:
        """리포트·응답에 싣는 출처표."""
        return {s.key: f"{s.grade}:{s.source}" for s in self.all()}

    @property
    def mocked(self) -> tuple[str, ...]:
        """🔴 mock 에서 온 것. **감추지 않고 위로 올린다.**"""
        return tuple(s.key for s in self.all() if s.grade == "MOCK")


def collect_inputs(item: str, as_of: date, *, sim_run_id: str) -> MasterInputs:
    """세 입력을 모은다. **하나가 실패해도 나머지는 싣는다.**

    🔴 **`sim_run_id` 에 기본값을 두지 않는다** (2026-09-13). 확정 주문은 실행마다
      다른 사실이다. 안 넘긴 자리는 조용히 번인이나 전 실행을 읽지 말고 **여기서 터진다.**

    ★ **매입 전용이다.** 부르는 자리는 `service._inputs_for` 하나이고, 그래서
      시세 계열도 매입 것(`PROCUREMENT_TARGET_KIND`)으로 정해서 넘긴다. 판매는
      셋을 안 모으고 예측 하나만 따로 읽는다 (`service._sales_forecast`).
    """
    return MasterInputs(
        forecast=load_forecast(item, as_of, target_kind=PROCUREMENT_TARGET_KIND),
        confirmed_orders=load_confirmed_orders(item, as_of, sim_run_id=sim_run_id),
        policy_values=load_policy_values(item, as_of),
    )


# ── forecast ────────────────────────────────────────────────────────────


def load_forecast(item: str, as_of: date, *, target_kind: str) -> SourcedInput:
    """ML 예측. **`as_of` 당일 배치만 쓴다.**

    🔴 **`target_kind` 에 기본값을 두지 않는다** (2026-09-11).

      기본값은 곧 업무 규칙이고, **안 넘긴 자리가 조용히 경매가로 답한다** — 전 판이
      정확히 그 상태였다. 조회에 `'AUC'` 가 박혀 있어서 판매도 경매가를 읽었고,
      그래서 **경매가로 사서 경매가로 팔았다.**

      `revalidation.revalidate_scenario` 가 `as_of` 에 대해 같은 결론을 냈다 —
      안 넘기면 터져야 한다.

    ★ 부르는 자리가 **자기 계열을 골라서 넘긴다.**

      .. code-block:: text

          매입   PROCUREMENT_TARGET_KIND   경매에서 산다
          판매   SALES_TARGET_KIND         중도매로 판다

    ★ 미래 배치를 집으면 백테스트 성적이 통째로 무효가 된다 (look-ahead).
      뷰가 `as_of` 컬럼을 갖고 있으므로 **그 이하만** 고른다 — 이 조건은 그대로다.

    🔴 **그런데 상한이 없었다** (2026-09-04).

      전 판의 docstring 은 *"`generated_at <= as_of` 인 최신 배치만 본다"* 라고만
      적어 두어, **얼마나 옛것이어도 되는지**가 안 보였다. 실제로는 그날 배치가
      없으면 조용히 옛 배치를 집고 `MEASURED` 로 실어 보냈다. 실측(배추):

      .. code-block:: text

          as_of        집은 배치      지연     등급
          2026-09-04   2026-09-04     0일      MEASURED   ← 정상
          2026-08-25   2026-01-27     210일    MEASURED   ← 🔴 결함
          2026-08-01   2026-01-27     186일    MEASURED   ← 🔴 결함
          2026-06-01   2026-01-27     125일    MEASURED   ← 🔴 결함

      **210일 전 예측으로 오늘 매입안을 만들었다.** `#227` 이 *"ML DB 가 죽으면
      선다"* 를 만들었는데, **배치가 없는 날은 죽은 것으로 안 쳤다.**

    ★ 이제 집은 행의 `as_of` 가 요청 `as_of` 와 **같을 때만** `MEASURED` 다.
      하루만 밀려도 안 쓴다. 다르면 `MISSING` 이고, `#227` 이 낸 기존 경로
      (MISSING → 매입 `RUNTIME_NOT_READY` → `E4_NOT_STARTED`)를 그대로 탄다.

    ★ **공휴일 달력을 심지 않는다.** ML 배치 유무가 곧 개장 여부다 — 배치일
      실측에서 평일은 `base_dt == 그날` 로 배치가 있고, 2026-01-01(신정) 에만
      배치가 없어 간격이 2일로 벌어졌다. 달력을 따로 두면 그 달력이 틀리는 날이
      온다.
    """
    try:
        row = _forecast_from_db(item, as_of, target_kind)
    except Exception as error:  # noqa: BLE001 — 적재 실패가 Flow 를 죽이면 안 된다
        return _forecast_missing(f"DB 조회 실패 ({error})")
    if row is None:
        return _forecast_missing(f"{as_of} 이전 예측 배치가 없다")
    if row["as_of"] != as_of:
        return _forecast_missing(_stale_batch_why(as_of, row["as_of"]))
    return SourcedInput(
        key="forecast",
        payload=_forecast_payload(row),
        grade="MEASURED",
        # ★★ **이 한 줄이 계열을 나른다.** 나중에 *"이 단가가 경매였나 중도매였나"*
        #   를 되짚을 수 있는 자리는 여기뿐이다.
        #
        #   🟢 모양은 그대로 두었다. 다만 **그 값이 이제 실제로 갈린다** — 지금까지는
        #     매입도 판매도 늘 경매라 이 칸이 잠들어 있었다.
        source=f"v_ml_price_forecast(as_of={row['as_of']}, {row['target_kind']})",
        note=str(row.get("quality_note") or ""),
    )


def _forecast_from_db(item: str, as_of: date, target_kind: str) -> dict[str, Any] | None:
    """`as_of` **이하** 최신 배치 한 행. **당일인지는 여기서 안 본다.**

    🔴 **계열도 여기서 안 정한다** (2026-09-11). 전 판은 조회에 `'AUC'` 가 박혀
      있어서 부르는 자리가 무엇이든 **경매가가 올라왔다.** 이제 받아서 넘기기만 한다.

    ★ `as_of <= %s` 를 걷으면 미래 배치를 집는다 (look-ahead). 그러면 백테스트
      성적이 통째로 무효가 되므로 이 조건은 남는다.

    🔴 **그러나 이 조회에는 지연 상한이 없다.** 그날 배치가 없으면 210일 전
      배치가 그대로 올라온다 (2026-08-25 → 2026-01-27, 실측 2026-09-04).
      **당일인지를 거르는 것은 `load_forecast` 의 몫이다** — 조회는 후보를
      집어 오고, 쓸지 말지는 부르는 쪽이 정한다.
    """
    query = sql.SQL("""
        SELECT * FROM {}.v_ml_price_forecast
         WHERE item = %s AND as_of <= %s AND target_kind = %s
         ORDER BY as_of DESC
         LIMIT 1
    """).format(sql.Identifier(get_db_schema()))
    row = fetch_one(query, (item, as_of, target_kind))
    return dict(row) if row else None


def _stale_batch_why(as_of: date, batch_as_of: date) -> str:
    """당일 배치가 아니라는 사유. **셋을 다 적는다** — 요청일 · 최신 배치일 · 지연일수.

    사람이 읽고 *"언제 것을 집을 뻔했는지"* 를 알아야 한다. 지연일수가 없으면
    두 날짜를 눈으로 빼야 하고, 210일과 1일이 같은 문장으로 보인다.

    ⚠️ **원인을 단정하지 않는다.** 당일 배치가 없는 이유는 공휴일일 수도, ML 이
      안 돈 것일 수도, 적재가 늦은 것일 수도 있는데 **마스터는 그 셋을 구분할
      수단이 없다.** 뷰에는 "배치가 있다/없다" 만 있고 "왜 없다" 가 없다.
      사유가 원인을 단정하면 다음 사람이 엉뚱한 데를 판다 — 사실만 적는다.
    """
    delay = (as_of - batch_as_of).days
    return f"{as_of} 당일 예측 배치가 없다 (가장 최신 배치 {batch_as_of} · {delay}일 전)"


def _forecast_payload(row: dict[str, Any]) -> dict[str, Any]:
    """뷰 행을 매입이 받는 형태로. **키를 고르기만 하고 값은 손대지 않는다.**

    🔴 **`use_recommended` 를 더했다** (2026-09-03 · 매입 `#192`).

      ML 이 신뢰도 플래그 셋을 붙여 보내는데 매입이 하나도 안 읽고 있었다.
      매입은 *"payload 에 칸이 없어서 못 읽는다"* 로 진단했는데 **절반만 맞았다.**

      ```text
      is_filled · is_gated   행별   뷰가 daily[] 안에 넣어 이미 간다
      use_recommended        조합별  여기서 버리고 있었다
      ```

    ★ **`daily` 안의 둘은 손대지 않는다.** 뷰가 `jsonb_build_object` 로 넣은
      그대로 나른다 — 마스터가 풀어 다시 조립하면 ML 이 준 모양이 바뀐다.

    🔴 **`as_of` · `target_kind` 를 더했다** (2026-09-11 · 걷기 실측).

      ML 계약(`app/ml/schemas.py Forecast`)이 **필수**로 두는 칸인데 여기서 버리고
      있었다. 매입은 안 읽어서 안 아팠고, 판매는 그 모델을 그대로 쓰므로 봉투가
      통째로 거부됐다 — **206일에서 537건.** `use_recommended` 때와 같은 모양이다.

    ⚠️ 아직 안 나르는 것이 셋 있다 — `has_filled_rows` · `filled_count` ·
      `quality_note`. 앞 둘은 `daily` 에서 셀 수 있는 파생이고, `quality_note` 는
      사람이 읽는 문장이라 `SourcedInput.note` 로 이미 화면에 간다.
      **읽겠다는 파트가 생기면 그때 더한다.**
    """
    return {
        # 🔴 **`as_of` 와 `target_kind` 는 ML 계약의 필수 칸이다** (2026-09-11).
        #    뷰가 주는데 여기서 버리고 있었다 — `use_recommended` 때와 같은 모양이다.
        #
        #    ★★ 매입은 이 둘을 안 읽어서 안 아팠고, 판매는 ML 모델(`app.ml.schemas
        #      .Forecast`)을 그대로 쓰므로 **없으면 봉투가 통째로 거부된다.**
        #      걷기 206일에서 판매 537건이 이 자리에서 죽었다.
        "as_of": row["as_of"],
        "target_kind": row["target_kind"],
        "generated_at": row["generated_at"],
        "item": row["item"],
        "unit": row["unit"],
        "current_price": _plain(row["current_price"]),
        "horizon_days": _plain(row["horizon_days"]),
        "daily": row["daily"],
        "model_version": row["model_version"],
        "use_recommended": row.get("use_recommended"),
    }


def _forecast_missing(why: str) -> SourcedInput:
    """🔴 **못 읽으면 비운다. mock 으로 메우지 않는다** (2026-09-03).

    전에는 여기서 `app.purchase_agent.mocks` 를 집어 왔다. 그러면 ML DB 장애가
    **정상 실행처럼** 보인다 — 매입이 안을 만들고 세 부서가 판정하고 `E1_APPROVED`
    까지 간다. 사람이 `input_sources` 를 읽지 않으면 아무도 모른다.

    ★ 비우면 매입이 `missing_data: ["forecast"]` 로 `RUNTIME_NOT_READY` 를 낸다.
      **없는 것과 못 만든 것을 가르는 것**이 이 프로젝트의 §1.2-10 이다.
    """
    return SourcedInput(key="forecast", payload=None, grade="MISSING", source="-", note=why)


# ── confirmed_orders ────────────────────────────────────────────────────


def load_confirmed_orders(item: str, as_of: date, *, sim_run_id: str) -> SourcedInput:
    """향후 납품 예정. **실제 주문이 있으면 그것을, 없으면 파트너 수요에서 파생한다.**

    🔴 **실제 주문은 이 실행의 것만 읽는다** (2026-09-13). 축 없이 읽으면 앞 걷기가
      구간 끝에 남긴 `CONFIRMED` 주문이 다음 걷기의 확정 수요로 샌다 — 같은 코드로
      다시 걸 때마다 무 매입이 판마다 11~14kg 늘었다.

    ★ **파생 경로(`_orders_from_demand`)에는 축을 붙이지 않는다.** `partner_item_demands`
      는 실행 축 없는 기준표이고 실행마다 같아야 맞다.

    🔴 **파생분을 "확정 주문" 이라 부르지 않는다.** `sales` 에 앞으로 납품할 건이
      0건이라(전부 `DELIVERED`) 파트너 일수요로 메우는데, 그건 **예상 수요이지 확정이
      아니다.** 등급을 `DERIVED` 로 두고 파생식을 `note` 에 적어 리포트에 내보낸다 —
      값만 넘기면 매입도 사람도 확정으로 읽는다.

    🔴 **조회가 터지면 파생으로 넘어가지 않는다** (`#651` · 2026-09-14).

      전 판은 예외를 `booked = None` 으로 받아 *"확정 건이 없다"* 와 같은 길로 보냈다.
      그러면 DB 장애가 **파트너 명목 수요로 사는 정상 걷기**처럼 보인다.
      `load_forecast` 가 조회 실패에 대해 이미 낸 결론과 같다 — 비운다.

      .. code-block:: text

          조회 성공 · 0건   DERIVED    파트너 일수요 × 기간 (그대로)
          조회 성공 · N건   MEASURED   이 실행의 확정 주문 (그대로)
          조회 실패         MISSING    메우지 않는다 → 매입 missing_data

    ⚠️ 사유는 **원인을 단정하지 않는다.** 예외 클래스와 메시지만 적는다.
    """
    try:
        booked = _orders_from_db(item, as_of, sim_run_id=sim_run_id)
    except Exception as error:  # noqa: BLE001 — 적재 실패가 Flow 를 죽이면 안 된다
        return SourcedInput(
            key="confirmed_orders",
            payload=None,
            grade="MISSING",
            source="-",
            note=f"확정 주문 조회 실패 ({type(error).__name__}: {error})",
        )
    why = "앞으로 납품할 확정 건이 없다"

    if booked:
        return SourcedInput(
            key="confirmed_orders",
            payload=booked,
            grade="MEASURED",
            source="sales + sale_items",
            note=f"{as_of} 이후 {_ORDER_WINDOW_DAYS}일 납품 예정",
        )

    try:
        return _orders_from_demand(item, as_of, why)
    except Exception as error:  # noqa: BLE001
        return SourcedInput(
            key="confirmed_orders",
            payload=None,
            grade="MISSING",
            source="-",
            note=f"{why} · 파생도 실패 ({error})",
        )


def _orders_from_db(item: str, as_of: date, *, sim_run_id: str) -> dict[str, Any] | None:
    query = sql.SQL("""
        SELECT s.sale_id, s.sale_date, si.quantity_kg
          FROM {sch}.sales s
          JOIN {sch}.sale_items si ON si.sale_id = s.sale_id
          JOIN {sch}.items i ON i.item_id = si.item_id
         WHERE i.item_name = %s
           AND s.sale_date > %s
           AND s.sale_date <= %s
           AND s.order_status IN ('CONFIRMED', 'READY')
           AND s.sim_run_id = %s
         ORDER BY s.sale_date
    """).format(sch=sql.Identifier(get_db_schema()))
    rows = fetch_all(query, (item, as_of, as_of + timedelta(days=_ORDER_WINDOW_DAYS), sim_run_id))
    if not rows:
        return None
    orders = [
        {
            "sale_id": r["sale_id"],
            "qty_kg": _plain(r["quantity_kg"]),
            "due_date": r["sale_date"].isoformat(),
        }
        for r in rows
    ]
    return {
        "as_of": as_of.isoformat(),
        "item": item,
        "orders": orders,
        "total_kg": sum(o["qty_kg"] for o in orders),
    }


@dataclass(frozen=True)
class _DemandRow:
    """거래처 한 곳의 품목 일수요 한 행. **언제부터 유효한지를 같이 든다** (2026-09-14)."""

    partner_id: str
    cycle: int
    active_from: date
    effective_from: date
    daily: Any
    basis: str
    provisional: bool


def _demand_rows(item: str, as_of: date, until: date) -> list[_DemandRow]:
    """`as_of` 에 이미 유입된 거래처의, `until` 까지 한 번이라도 유효해지는 그 품목 일수요.

    ★ SQL 은 거래처를 `as_of` 로, 수요를 **창 끝**으로만 거른다. 날짜별 판정은
      `_in_force` 가 한다 — 창 안에서 유효해지는 수요는 SQL 한 줄로 표현이 안 된다.
      거래처 조건은 부르는 자리(`_orders_from_demand`)에서 한 번 더 건다.

    ★ 순서는 `active_from` · `partner_id` 다. 먼저 유효해진 거래처가 먼저 온다.
    """
    schema = sql.Identifier(get_db_schema())
    rows = fetch_all(
        sql.SQL("""
            SELECT p.partner_id, p.order_cycle_days, p.active_from,
                   d.effective_from, d.daily_demand_kg, d.demand_basis, d.provisional
              FROM {sch}.partner_item_demands d
              JOIN {sch}.items i ON i.item_id = d.item_id
              JOIN {sch}.partners p ON p.partner_id = d.partner_id
             WHERE i.item_name = %s
               AND p.active = true
               AND p.active_from <= %s
               AND d.effective_from <= %s
             ORDER BY p.active_from, p.partner_id, d.effective_from
        """).format(sch=schema),
        (item, as_of, until),
    )
    return [
        _DemandRow(
            partner_id=r["partner_id"],
            cycle=max(1, int(r["order_cycle_days"] or 1)),
            active_from=r["active_from"],
            effective_from=r["effective_from"],
            daily=_plain(r["daily_demand_kg"]),
            basis=r["demand_basis"],
            provisional=bool(r["provisional"]),
        )
        for r in rows
    ]


def _in_force(rows: list[_DemandRow], day: date) -> _DemandRow | None:
    """그날 유효한 행 하나. **거래처가 유효해졌고 그 수요도 유효해진 것만.**

    🔴 **이 한 줄이 「유효해지기 전 날에는 그 거래처가 안 보인다」 의 전부다.**
      빼면 창 안의 모든 날이 아직 유효하지 않은 거래처 수요를 더한다.

    ★ 같은 거래처에 행이 여럿이면 `effective_from` 이 가장 늦은 것이 이긴다.
    """
    live = [r for r in rows if r.active_from <= day and r.effective_from <= day]
    return max(live, key=lambda r: r.effective_from) if live else None


def _orders_from_demand(item: str, as_of: date, why: str) -> SourcedInput:
    """파트너 일수요 × 기간. **주문 주기 간격으로 쪼갠다.**

    ⑤ 노드가 `due_date` 별 분포로 등급-신선도를 맞추므로 총량 한 덩어리로 주면
    "전량을 첫날 납품" 으로 읽힌다. 주기(`order_cycle_days`)를 그대로 쓴다.

    🔴 **거래처가 날짜를 갖는다** (2026-09-14 신규 거래처).

      .. code-block:: text

          거래처          as_of 에 이미 유입된 것만 (active_from <= as_of)
          창의 날 d 마다   active_from <= d 이고 effective_from <= d 인 거래처의 일수요 합
          주기            거래처마다 제 order_cycle_days (전 판: 뷰 LIMIT 1)
          창 길이         _ORDER_WINDOW_DAYS 그대로. 주기는 창 길이를 안 정한다

      ★ 거래처마다 제 주기로 주문을 쪼갠다. 한 주문은 그 주기 구간의 날들 중
        **유효한 날만** 더한다. 한 날도 유효하지 않은 구간은 주문을 안 낸다.

      🔴 거래처 1곳 · 기본 날짜면 **payload 가 전 판과 같다.** 구간 안의 일수요가
        하나뿐이면 `일수요 × 주기` 한 번의 곱이라 반올림 전 값까지 같다.
    """
    until = as_of + timedelta(days=_ORDER_WINDOW_DAYS)
    fetched = _demand_rows(item, as_of, until)
    # 🔴 **거래처는 `as_of` 에 이미 유입돼 있어야 창에 들어온다** (2026-09-14 결정).
    #    9/14 에 갑자기 생긴 거래처라, 9/13 까지의 매입 입력은 그 거래처를 모른다.
    #    들어온 뒤로는 창 안의 날짜별 적용일(`_in_force`)을 따른다.
    rows = [r for r in fetched if r.active_from <= as_of]
    if not rows:
        raise LookupError(f"{item} 파트너 일수요가 없다")

    by_partner: dict[str, list[_DemandRow]] = {}
    for row in rows:
        by_partner.setdefault(row.partner_id, []).append(row)

    orders: list[dict[str, Any]] = []
    fragments: list[str] = []
    for partner_rows in by_partner.values():
        cycle = partner_rows[0].cycle
        contributed: _DemandRow | None = None
        for offset in range(cycle, _ORDER_WINDOW_DAYS + 1, cycle):
            # ★ 같은 일수요 행이 며칠 유효한지를 센다 — 행마다 곱 한 번이다.
            segments: dict[int, tuple[_DemandRow, int]] = {}
            for back in range(cycle - 1, -1, -1):
                row = _in_force(partner_rows, as_of + timedelta(days=offset - back))
                if row is None:
                    continue
                held = segments.get(id(row))
                segments[id(row)] = (row, (held[1] if held else 0) + 1)
                contributed = row
            if not segments:
                continue
            orders.append(
                {
                    "sale_id": None,  # 실제 주문이 아니다 — id 를 지어내지 않는다
                    "qty_kg": round(sum(r.daily * n for r, n in segments.values()), 1),
                    "due_date": (as_of + timedelta(days=offset)).isoformat(),
                }
            )
        if contributed is None:
            continue
        starts = max(contributed.active_from, contributed.effective_from)
        fragments.append(
            f"일수요 {contributed.daily}kg × {_ORDER_WINDOW_DAYS}일, 주기 {cycle}일로 분할 "
            f"({contributed.basis}"
            f"{', 잠정값' if contributed.provisional else ''}"
            f"{f', {starts} 부터' if starts > as_of + timedelta(days=1) else ''})"
        )

    # ★ 날짜순. 같은 날이면 먼저 유효해진 거래처가 앞이다 (정렬이 안정적이다).
    orders.sort(key=lambda o: o["due_date"])
    return SourcedInput(
        key="confirmed_orders",
        payload={
            "as_of": as_of.isoformat(),
            "item": item,
            "orders": orders,
            "total_kg": round(sum(o["qty_kg"] for o in orders), 1),
        },
        grade="DERIVED",
        source="partner_item_demands · partners",
        note=(
            f"{why} → {' + '.join(fragments)} · 거래처 {len(fragments)}곳 합산"
            " · 확정 주문이 아니다"
        ),
    )


# ── policy_values ───────────────────────────────────────────────────────


def load_policy_values(item: str, as_of: date) -> SourcedInput:
    """매입이 쓰는 정책값.

    ★ `item_mix_ratio` 는 **파트너 일수요에서 파생**한다. 정책 테이블에 그 키가 없고,
      쓰임이 *"한 품목이 임계 이상을 차지하면 mix 축을 닫는다"* 라 **품목별 수요
      비중**이 바로 그 뜻이다. 파생식을 `note` 에 남긴다.

    ⚠️ `contract_price_krw` 는 **비운다.** 계약 단가가 DB 에 없고, 매입 계약상
      필수가 아니다 — 없으면 `margin_warning` 이 `null` 로 나가는 것이 정상 경로다.
      **평균값으로 메우면 마진 경고가 조용히 틀린다.**
    """
    try:
        # ★ 비중은 **그날(`as_of`) 유효한 거래처**의 일수요로 낸다 (2026-09-14).
        #   정책은 여전히 현재 유효분 하나다 — 버전 축은 policy_version 이 갖는다.
        ratios = _mix_ratio_from_demand(as_of)
    except Exception as error:  # noqa: BLE001
        return SourcedInput(
            key="policy_values", payload=None, grade="MISSING", source="-", note=str(error)
        )
    if not ratios:
        return SourcedInput(
            key="policy_values",
            payload=None,
            grade="MISSING",
            source="partner_item_demands",
            note="품목별 일수요가 없어 비중을 낼 수 없다",
        )

    payload: dict[str, Any] = {"item_mix_ratio": ratios}
    return SourcedInput(
        key="policy_values",
        payload=payload,
        grade="DERIVED",
        source="partner_item_demands",
        note=(
            f"item_mix_ratio = 품목 일수요 ÷ 전체 일수요 "
            f"({item} {ratios.get(item, 0):.3f}) · contract_price 는 DB 에 없어 비움"
        ),
    )


def _mix_ratio_from_demand(as_of: date | None) -> dict[str, float]:
    """품목 비중 — **분모는 계약 품목만이다** (`#286`).

    🔴 계약 밖 품목이 분모에 들면 비중이 눌린다 (매입 실측 2026-09-10 · 배추 0.7643 vs 0.8096).

    ★ **「보일 때 거르기」로는 안 된다** — 비중은 이미 눌린 값이라 받는 쪽에서 되돌릴 수 없다.
      되돌릴 수 없는 것은 원천에서 막는다.

    ⚠️ DB 행은 안 고친다. 지난 기록을 고쳐 쓰면 기록이 거짓이 된다 — 읽을 때만 거른다.

    ★ **품목 이름을 여기 다시 적지 않는다.** 정본은 `app.contracts.core.ITEMS` 하나다 —
      두 벌을 두면 계약이 늘거나 줄 때 한쪽만 바뀐다.

    🔴 **거래처가 둘이면 품목별로 더한다** (2026-09-14). 전 판은 행마다 표에 넣어
      같은 품목의 둘째 행이 첫째를 덮고 분모에는 둘 다 들었다.

    ★ `as_of` 에 유효한 거래처(`active_from <= as_of`)와 수요(`effective_from <= as_of`)
      만 센다. 날짜를 안 주면(`None`) 날짜로 거르지 않는다 — 전 판과 같은 모양이다.
      같은 거래처·품목에 행이 여럿이면 `effective_from` 이 가장 늦은 것 하나만 센다.
    """
    schema = sql.Identifier(get_db_schema())
    rows = fetch_all(
        sql.SQL("""
            SELECT i.item_name, d.partner_id, p.active_from, d.effective_from,
                   d.daily_demand_kg
              FROM {sch}.partner_item_demands d
              JOIN {sch}.items i ON i.item_id = d.item_id
              JOIN {sch}.partners p ON p.partner_id = d.partner_id
             WHERE i.item_name = ANY(%s)
               AND p.active = true
        """).format(sch=schema),
        (list(ITEMS),),
    )
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if as_of is not None and not (r["active_from"] <= as_of and r["effective_from"] <= as_of):
            continue
        key = (r["item_name"], r["partner_id"])
        held = latest.get(key)
        if held is None or r["effective_from"] > held["effective_from"]:
            latest[key] = r
    by_item: dict[str, list[Any]] = {}
    for r in latest.values():
        by_item.setdefault(r["item_name"], []).append(r["daily_demand_kg"])
    # ★ 한 품목에 한 값이면 그 값 그대로다 (`0 + x` 가 `x` 라 전 판과 같은 수가 나온다).
    sums = {name: _plain(sum(values)) for name, values in by_item.items()}
    total = sum(sums.values())
    if not total:
        return {}
    return {name: round(value / total, 4) for name, value in sums.items()}


# ── 값 정리 ─────────────────────────────────────────────────────────────


def _plain(value: Any) -> Any:
    """`Decimal` 을 파이썬 수로. **정수는 정수로 남긴다.**

    매입 계약이 `qty_kg` 를 정수로 받는 자리가 있어, 무조건 `float` 로 바꾸면
    소수/정수 불일치가 거기서 터진다.
    """
    if isinstance(value, Decimal):
        as_float = float(value)
        return int(as_float) if as_float.is_integer() else as_float
    return value
