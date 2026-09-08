"""
bootstrap.py — **등록소를 채우는 유일한 자리다. 진입점이 둘이라 여기 있다.**

```text
app/main.py                     임포트 시점에 부른다 (FastAPI 앱)
app/master/backtest_runner.py   main() 이 걷기 전에 부른다 (CLI 범위 걷기)
```

🔴 **전에는 이 등록이 `app/main.py` 모듈 수준에만 있었다.** CLI 는 그 모듈을 안
   거치므로 에이전트 레지스트리와 등록소 다섯이 **전부 빈 채로** 걸었다 —
   걷는 날마다 *"하루 넘김 미등록: finance, logistics"* 로 돌아서서 5일이 5일 다
   사고였다 (2026-09-09 실측).

   ★ **러너가 틀린 것이 아니었다.** 러너는 미등록을 이름까지 정확히 말했다.
     틀린 것은 조립 자리였다.

★ **몇 번을 불러도 같다.** 여섯 등록이 전부 키 덮어쓰기라 재등록이 거부되지 않는다.
  *"이미 했다"* 깃발을 두지 않는 이유가 그것이다 — 깃발을 두면 `wiring.reset()` 으로
  비운 뒤 이 함수로 다시 채우는 경로가 막히고, `importlib.reload(app.main)` 로
  배선을 다시 세우는 검사(`tests/logistics/test_logistics_day_open.py` ⑩)도 막힌다.

⚠️ **여기서 대상을 늘리거나 줄이지 않는다.** 옮기기 전에 `app/main.py` 가 등록하던
  것과 한 줄도 다르지 않아야 한다. 무엇을 등록할지의 근거는 각 줄 위에 그대로 있다.
"""

from __future__ import annotations

from functools import partial

from app.finance.adapter import finance_port
from app.finance.day_open import FinanceDayOpening
from app.finance.transition import FinanceTransitionAdapter
from app.logistics.adapter import logistics_port
from app.logistics.cancellation import LogisticsCancellationAdapter
from app.logistics.day_open import LogisticsDayOpening
from app.logistics.inbound_execution import LogisticsInboundExecution
from app.logistics.simulated_inspection import ScenarioSimulatedInspectionProvider
from app.logistics.transition import LogisticsTransitionAdapter
from app.master.cancellation import register_cancellation
from app.master.collection import register_collection
from app.master.day_open import register_day_opening
from app.master.finance_cancellation import FinanceCancellationAdapter
from app.master.finance_collection import FinanceCollectionAdapter
from app.master.inbound import register_inbound
from app.master.ledger_repository import BURN_IN_SIM_RUN_ID
from app.master.transition import register_transition
from app.master.wiring import register as register_agent
from app.purchase_agent.adapter import purchase_port
from app.purchase_agent.quotes import auction_quote_source
from app.sales.adapter import sales_port

__all__ = ["wire_registries"]


def wire_registries() -> None:
    """에이전트 레지스트리와 등록소 다섯을 채운다. **두 진입점이 이것을 부른다.**"""
    # 마스터가 부를 수 있는 대상은 **런타임에 등록된 것뿐**이다 (wiring).
    #
    # ★ 미등록은 오류가 아니라 "오늘 그 부서가 돌지 않는다"와 같은 상태다 —
    #   마스터가 E4_NOT_STARTED + missing_adapters 를 낸다 (정의서 §5.3).
    #
    # ★ 세 파트가 다 등록됐다 (2026-08-27). 이제 미등록으로 멈추지 않고, 각 부서가
    #   **무엇이 없어서 못 도는지**를 자기 missing_data 로 답한다. 둘은 다르다 —
    #   앞은 배선 문제이고 뒤는 그날의 사실이다.
    register_agent("finance", finance_port)
    register_agent("inventory", logistics_port)
    # 🔴 **실 경락가를 꽂는다** (2026-09-03). 기본값 mock 으로 두면 매입이 안을 못 낸다.
    #
    #   mock 단가는 실 ML 예측에서 나온 상한을 못 넘는다 — 두 값의 출처가 달라서다.
    #
    #       max_price          실 ML 예측 q90       배추 992 · 무 795
    #       grade_unit_price   mock                 배추 1,650 · 무 1,100
    #
    #   그래서 self_check 가 전부 컷하고 `no_proposal_reason` 만 남았다. 실측으로
    #   같은 payload 를 두 시세로 돌려 확인했다.
    #
    #       mock      배추 0안 · 무 0안   business=skipped
    #       실 경락가  배추 2안 · 무 2안   business=ok
    #
    # ★ **mock 이 틀린 값이라서가 아니라 섞이면 안 되는 값이라서다.** 예측은 실데이터인데
    #   단가만 연습값이면 그 둘을 비교한 판정은 아무 뜻이 없다.
    #
    # ⚠️ ML `current_price` 와 매입 물량가중 시리즈가 일치하지 않는 것은 여전히 미결이다
    #   (2026-08-31 실측 · 배추 812 vs 933). 다만 그것은 **두 값을 어떻게 병기해 보여줄지**의
    #   문제이고, 매입단가로 무엇을 쓸지는 아니다 — 매입단가는 실제로 살 때 낼 돈이다.
    register_agent("purchase", partial(purchase_port, quotes=auction_quote_source()))
    # 🔴 **넷째가 붙었다 — 판매 어댑터가 `#364` 로 들어왔다** (2026-09-07).
    #
    #   그전까지 이 줄이 없어서 `POST /master/sales/run` 이 **부르기 전에** 섰다.
    #   `REQUIRED_FOR_SALES = ("sales", "finance")` 를 문 앞에서 보는데 `sales` 가 없어
    #   매번 `SL4_NOT_STARTED` 로 돌아섰다 — 판매 Flow 는 다 서 있었고 **배선 한 줄만**
    #   없었다.
    #
    # ★ **미등록과 후보 0건은 다른 사실이다.** 앞은 아무도 못 부른 상태이고 뒤는
    #   판매가 답을 낸 결과다. 이 줄이 그 둘을 가른다 —
    #   `tests/master/test_sales_registration.py` 가 등록과 그 경로를 같이 잰다.
    register_agent("sales", sales_port)

    # ── 승인 → 장부 상태전이 (C 형태 ⑦) ────────────────────────────────────
    #
    # 🔴 **호출이 0건이었다.** 어댑터는 재무·물류 양쪽에 다 섰는데 등록하는 줄이 없어
    #    `apply_approval` 이 매 승인마다 *"상태전이 미등록"* 으로 돌아섰다 — 사람이
    #    승인해도 장부가 안 바뀌었다. 배선은 마스터 몫이고 그 자리가 여기다.
    #
    # ★ **위의 `register_agent` 와 다른 등록소다.** 저쪽은 **부를 대상**(에이전트)을,
    #   이쪽은 **장부를 바꿀 방법**을 담는다. 한 사전에 섞으면 "어댑터가 없다" 와
    #   "전이가 없다" 가 같은 문장으로 나가는데, 둘은 다른 사실이다.
    #
    # 🔴 **`sim_run_id` 는 마스터가 정한다.** `persist_inventory` 의 WHERE 가 그 값을
    #    쓰지만 *"어느 실행의 장부인가"* 는 물류 사실이 아니다. 물류 모듈에 상수로
    #    박으면 실행이 둘이 되는 날 물류 코드를 고쳐야 하므로 여기서 눈에 보이게 준다.
    #    값의 주인은 `ledger_repository.BURN_IN_SIM_RUN_ID` 하나이고, 매입 원장
    #    (`ledger.sim_run_id_for`)도 같은 상수를 가리킨다 — 새로 만들지 않는다.
    register_transition("finance", FinanceTransitionAdapter())
    register_transition("logistics", LogisticsTransitionAdapter(sim_run_id=BURN_IN_SIM_RUN_ID))

    # ── 하루 넘김 (day_open) ────────────────────────────────────────────────
    #
    # 🔴 **또 하나의 다른 등록소다.** 위 `register_transition` 은 **승인이 장부를 바꾸는
    #    방법**을 담고 이쪽은 **하루가 넘어가는 방법**을 담는다. 한 사전에 섞으면 전이가
    #    없는 것과 하루 넘김이 없는 것이 같은 문장으로 나가고, 둘은 다른 사실이다.
    #
    # 🔴 **`sim_run_id` 를 여기서도 준다 (`#324`).** 종전에는 *"하루 넘김은 그 값을 정하지
    #    않고 전날 행에서 물려받으니 마스터가 줄 값이 없다"* 고 적혀 있었다. 그 말은
    #    **새 행에 쓰는 값**에 대해서는 지금도 맞다 — carry-forward 는 `base.sim_run_id` 를
    #    그대로 옮기고 여기서 준 값을 칸에 적지 않는다.
    #
    #    ⚠️ **틀렸던 것은 "어느 전날 행을 물려받을지" 였다.** `uq_log_runtime_fixture` 가
    #       `(sim_run_id, as_of, usage_scope)` 라 다른 실행의 행이 같은 날에 공존할 수
    #       있고, 안 좁히면 하루 넘김이 **남의 실행 행을 보고 "열렸다"** 고 답한다. 그건
    #       물려받는 값이 아니라 **읽기 전에 알고 있어야 하는 실행 정체성**이라 위 두
    #       어댑터와 같은 자리에서 받는다.
    #
    # ★ **새 값을 만들지 않는다.** `register_transition` · `register_cancellation` 이 이미
    #   쓰는 그 상수 하나를 그대로 넘긴다 — 셋이 다른 실행에 앉으면 승인이 갱신하는 행과
    #   하루 넘김이 세우는 행이 갈린다.
    #
    # 🔴 **재무를 먼저 켤 수 없었던 이유가 DB 였다 (2026-09-05).** `#285` 가 일별 상태를
    #    `ON CONFLICT (sim_run_id, financing_mode, state_date)` 로 누적하는데, 실 DB 에 그
    #    UNIQUE 가 없어 승인 전이가 그 자리에서 터졌다 (실측:
    #    *"there is no unique or exclusion constraint matching the ON CONFLICT
    #    specification"*). `database/finance/finance_state_daily_unique.sql` 을 적용한 뒤
    #    켰다 — **마이그레이션과 이 두 줄은 짝이다.**
    register_day_opening("logistics", LogisticsDayOpening(sim_run_id=BURN_IN_SIM_RUN_ID))
    register_day_opening("finance", FinanceDayOpening())


    # ── 승인 취소 (undo_approval) ───────────────────────────────────────────
    #
    # 🔴 **세 번째 등록소다.** 전이는 *"승인이 장부를 바꾸는 방법"*, 하루 넘김은 *"하루가
    #    넘어가는 방법"*, 여기는 *"승인을 물리는 방법"* 이다. 한 사전에 섞으면
    #    **전이는 되는데 취소는 안 되는 상태**를 표현할 수 없고, 실제로 어제까지가 그
    #    상태였다 (재무만 `#302` 로 섰고 물류는 없었다).
    #
    # ⚠️ **어댑터 둘 다 마스터가 임시로 얹은 것이다** (`#280` 전례).
    #
    #    ```text
    #    app/master/finance_cancellation.py    재무 함수(#302)를 Protocol 에 잇기만 한다
    #    app/logistics/cancellation.py          걷는 규칙까지 마스터가 썼다 (물류 통보 · 이견 없음)
    #    ```
    #
    #    각 부서가 자기 구현을 올리면 **그 파일을 지우고 이 두 줄만 바꾸면 된다.**
    #
    # 🔴 **DB 어휘가 아직 없다.** `master_decisions.decision` 에 `CANCEL` 이,
    #    `payables.status` 에 `CANCELLED` 가 없어 실 DB 에서는 이 경로가 CHECK 로 막힌다 —
    #    `database/master/master_decision_cancel.sql` 과
    #    `database/finance/payable_cancellation.sql` 을 **한 번에** 적용하는 날 열린다.
    #    등록을 먼저 해 두는 이유는 `apply_approval` 때와 같다: 배선이 없는 것과 어휘가
    #    없는 것은 다른 사실이고, 둘을 같은 문장으로 접으면 무엇을 고칠지가 사라진다.
    register_cancellation("finance", FinanceCancellationAdapter())
    register_cancellation("logistics", LogisticsCancellationAdapter(sim_run_id=BURN_IN_SIM_RUN_ID))


    # ── 입고 실행 (receive_arrivals) ────────────────────────────────────────
    #
    # 🔴 **네 번째 등록소다.** 전이는 *"승인이 장부를 바꾸는 방법"*, 하루 넘김은 *"하루가
    #    넘어가는 방법"*, 취소는 *"승인을 물리는 방법"*, 여기는 *"도착분을 받는 방법"* 이다.
    #    한 사전에 섞으면 **전이는 되는데 입고는 안 되는 상태**를 표현할 수 없다.
    #
    # 🔴 **구현은 `#329` 로 섰는데 이 줄이 없었다.** 경계(`#316`)와 구현이 다 있는데
    #    등록이 없어 `receive_arrivals` 가 매일 *"입고 실행 미등록"* 으로 돌아섰다 —
    #    `apply_approval` 이 승인마다 *"상태전이 미등록"* 으로 돌아서던 것과 **같은
    #    모양**이다. 배선은 마스터 몫이고 그 자리가 여기다.
    #
    # ⚠️ **`inspection_provider` 에 기본값이 없다. 물류가 일부러 안 뒀다.**
    #
    #    > 자동 검수 규칙(합격률·등급별 판정·수량 배분)을 정한 문서도 코드도 씨앗
    #    > 데이터도 없다. 여기에 기본 구현을 놓으면 **아무도 정한 적 없는 비율이 곧
    #    > 업무 사실이 되어** 원가·폐기·판매 판단으로 흘러간다.
    #
    #    그래서 **배선 자리에서 눈에 보이게 고른다.** 클래스가 저장소에 있다고 해서
    #    그것이 기본값이 되지는 않는다 — `ScenarioSimulatedInspectionProvider` 의
    #    docstring 이 그렇게 적어 뒀다.
    #
    # 🔴 **`ScenarioSimulatedInspectionProvider` 는 품질 모델이 아니다** (물류 `#336`).
    #
    #    *"실제 농산물이 늘 100% 정상"* 이라는 주장이 아니라 **이번 MVP 가 품질손실 축을
    #    아직 쓰지 않는다**는 명시적 가정이다. 물류가 그 이유를 자기 파일에 적어 뒀고,
    #    **그 판단의 주인이 물류다.**
    #
    # ★ **마스터가 잠깐 들고 있던 자리였다.** `#337` 을 처음 낼 때는 물류에 구현이 없어
    #   `app/master/inbound_inspection.py` 의 `NoInspectionSource`(항상 `None` → 매일
    #   `BLOCKED`)로 배선했다 — *"검수 규칙의 주인은 마스터가 안 정한다"* 를 지키려고
    #   **부재를 부재로 적은 것**이다. 물류가 `#336` 으로 주인 노릇을 하자 그 파일을
    #   지우고 이 한 줄을 바꿨다. `FinanceCancellationAdapter` 와 같은 전례이고,
    #   **그 임시 파일이 하루 만에 설계대로 사라졌다.**
    #
    #   ```text
    #   배선 안 함        "입고 실행 미등록"     배선 문제 — 사실이 아니다, 구현은 섰다
    #   NoInspectionSource "검수 사실을 모른다"   그날의 사실 — **주인이 없던 동안**
    #   ScenarioSimulated  전량 PASS              **주인이 정한 MVP 가정**  ← 지금
    #   ```
    register_inbound(
        "logistics",
        LogisticsInboundExecution(
            sim_run_id=BURN_IN_SIM_RUN_ID,
            inspection_provider=ScenarioSimulatedInspectionProvider(),
        ),
    )


    # ── 수금 (collect_receipts) ─────────────────────────────────────────────
    #
    # 🔴 **다섯 번째 등록소가 생겼다** (`app/master/collection.py` · 2026-09-07). 전이는
    #    *"승인이 장부를 바꾸는 방법"*, 하루 넘김은 *"하루가 넘어가는 방법"*, 취소는
    #    *"승인을 물리는 방법"*, 입고는 *"도착분을 받는 방법"*, 수금은 *"채권이 돈으로
    #    들어오는 방법"* 이다. 한 사전에 섞으면 **입고는 되는데 수금은 안 되는 상태**를
    #    표현할 수 없고, 지금이 정확히 그 상태다.
    #
    # 🟢 **어제까지 이 자리에 `register_collection` 이 없었다. 그 이유가 없어졌다.**
    #
    #    전에는 여기에 *"일부러 없다"* 라고 적혀 있었다 — 그때는 이랬다.
    #
    #    ```text
    #    2026-09-07   경계    app/master/collection.py            🟢 섰다 (여기 등록소)
    #                 구현    app/finance/collection.py       🟢 섰다 (apply_explicit_collection)
    #                 어댑터  CollectionSource 를 만족하는 재무 구현체   🔴 **아직 없었다**
    #                 배선    register_collection("finance", ...)  🔴 그래서 못 적었다
    #
    #    2026-09-08   어댑터  app/finance/collection_source.py    🟢 `#404` 로 섰다
    #                 배선    아래 한 줄                           🟢 그래서 적는다
    #    ```
    #
    #    ★ **`register_inbound` 가 `#337` 로 붙던 것과 같은 모양이다.** 경계와 구현이 다
    #      섰는데 배선이 없어 `collect_receipts` 가 매일 `NOTHING_DUE` +
    #      `missing=["finance"]` 로 돌아섰다. 배선은 마스터 몫이고 그 자리가 여기다.
    #
    # 🔴 **`sim_run_id` 는 마스터가 정하고 `financing_mode` 는 마스터가 안 고른다.**
    #
    #    ```text
    #    sim_run_id       🟢 **마스터가 정한다** — 위 네 등록소가 쓰는 그 상수 하나다
    #    financing_mode   🔴 **마스터 축이 아니다** — 재무 축의 것
    #                        (sim_run_id, as_of, financing_mode)
    #    ```
    #
    #    ⚠️ 실측으로 `finance_states` 에 `LOAN_BASELINE` 252행과 `BASE_NO_LOAN` 2행이
    #      **공존한다.** 여기에 하나를 상수로 박으면 *"무차입 상태가 대출 baseline 자리에
    #      조용히 들어온다"* — `app/finance/db.py` 의 `get_finance_runtime_axis` 가 그
    #      문장을 이미 적어 뒀다.
    #
    #    ★ **그래서 어댑터가 `collect()` 안에서 재무에게 축을 물어본다.** 임포트 시점에
    #      DB 를 읽지 않는 것은 위 네 등록소와 같다. 재무 축의 `sim_run_id` 가 마스터가
    #      준 것과 다르면 **막는다(fail-closed)** — 조용히 남의 실행 장부에 수금을 적으면
    #      안 된다.
    #
    # ⚠️ **어댑터는 마스터가 얹은 얇은 배선이다** (`#280` · `FinanceCancellationAdapter`
    #    전례). 재무가 축 둘을 직접 들고 오는 구현을 올리면 **그 파일을 지우고 이 한 줄만
    #    바꾸면 된다.**
    #
    # 🔴 **사건은 이 줄이 아니라 표에서 온다** (`master_collection_events` · 2026-09-08).
    #
    #    전에는 여기서 `DeterministicCollectionFixtureSource()` 를 만들어 넘겼다. 그러면
    #    **사건 목록이 배선 시점에 고정**돼, 표에 한 줄 넣어도 앱을 다시 띄우기 전까지는
    #    아무 일도 안 일어난다. 지금은 어댑터가 `collect()` **안에서** 그 축의 사건을
    #    읽는다 — 축 조회를 임포트 시점에 안 하는 것과 같은 이유다.
    #
    # 🔴 **그 표가 비어 있다** (2026-09-08 실측 0행). 그래서 지금도 매일 `NOTHING_DUE` 다.
    #
    #    ```text
    #    전   등록 안 됨      → missing() 에 뜨고 경로가 안 돈다
    #    후   NOTHING_DUE     → **확인했고 낼 것이 없다**
    #    ```
    #
    #    ⚠️ **이 배선은 자리를 만들 뿐 사건을 만들지 않는다.** 무엇을 사실로 둘지는 팀
    #      결정이고, 재무가 *"due_date 경과를 수금으로 읽지 않는다"* 로 그은 선이 그
    #      이유다. **낸 것과 도는 것은 다르다.**
    #
    #    ★ **그래도 값이 있다.** 어제까지는 **사건을 넣을 자리조차 없었다.** 표가 섰으니
    #      한 줄 INSERT 로 시연이 되고, `NOTHING_DUE` 와 「등록 안 됨」이 갈린다.
    register_collection(
        "finance",
        FinanceCollectionAdapter(sim_run_id=BURN_IN_SIM_RUN_ID),
    )
