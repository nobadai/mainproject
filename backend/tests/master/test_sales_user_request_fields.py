"""판매 `SalesUserRequest` 의 칸 — **마스터가 나르는 것과 안 나르는 것으로 가른다.**

🔴 **못 나르는 칸이 조용히 잊히는 것을 막는 자리다** (2026-09-07).

판매 `SalesUserRequest` 는 구조화된 칸을 여럿 갖는데 마스터 `SalesRunRequest` 에는
자유 문장(`user_request: str | None`)뿐이었다. 그래서 수량을 줄 방법이 없었고, 판매는
`raw_text` 를 해석해 수량을 뽑지 않는다 — 실 어댑터가 `PROPOSAL_QUANTITY_REQUIRED` 로
답한 그 자리다.

```text
넓혔다     requested_quantity_kg          판매가 실제로 요구했다
넓혔다     preferred_delivery_date         🔴 승인 경로가 요구했다 (2026-09-08)
           preferred_payment_days
넓혔다     preferred_unit_price_krw        🔴 재무 검증이 요구했다 (2026-09-11)
           preferred_payment_terms_type
           source_ref
안 넓혔다  preferred_contract_term_days    요구한 caller 가 아직 없다
           allow_additional_sourcing
```

🔴 **셋이 더 옮겨 왔다** (2026-09-11 · 걷기 실측).

자동 걷기가 판매 판정까지 갔는데 재무가 `SALES_INPUT_INCOMPLETE` 로 판정을 못 냈다 —
`missing_fields` 가 `partner_id` · `unit_price_krw` · `reported_sales_amount_krw` ·
`payment_terms_type` · `source_ref` 다섯이었다. 앞의 `partner_id` 는 이미 자리가
있었고, `reported_sales_amount_krw` 는 **판매가 수량 × 단가로 자기 안에서 세는
파생**이다. 마스터가 실을 수 있는 자리는 나머지 셋이고 **요구한 caller 가 생겼다.**

★ **`source_ref` 의 이유가 뒤집힌 자리가 여기다.** 종전에는 *"마스터에게 채울
  권위 있는 값이 없다"* 였다 — 사람이 화면에 친 문장에는 되짚을 행이 없기 때문이다.
  **자동 걷기는 다르다.** 조건이 `sim_runs.config_json` 의 실행 규칙에서 왔고, 그것은
  되짚을 수 있는 행이다. 그래서 **값이 있는 caller 만 채워 보낸다.**

🔴 **두 칸이 "안 나르는 쪽" 에서 "나르는 쪽" 으로 옮겨 왔다** (2026-09-08).

후보 판정이 `delivery_date` · `payment_days` 를 **승인 가능한 후보의 필수조건**으로
잡았고 (`sales_flow.CandidateVerdict.missing_terms`), 판매는 그 둘을
`preferred_*` 에서만 만든다 (`app/sales/proposal.py` `_baseline`). 즉 **마스터가 안
실어 보내서** 실측의 후보 3안이 전부 `delivery_date=None` 이었다 — 판매가 값을 못
만든 것이 아니다. 요구한 caller 가 생겼으므로 자리를 만든다.

★ **없는 필요를 API 표면에 미리 만들지 않는다.** 칸을 다 열면 화면이 안 쓰는 칸을
  채우기 시작하고, 그 값이 어디서 왔는지 아무도 모른 채 제안에 실린다.

⚠️ **대신 안 나르는 칸이 잊히면 안 된다.** 여기서 판매 모델을 읽어 대조하므로
  **판매가 칸을 하나 더하면 그 칸은 어느 쪽에도 안 들어가 이 검사가 빨개진다** —
  *"이건 나르는 쪽이냐 안 나르는 쪽이냐"* 를 그때 정하게 된다.

🔴 **목록을 손으로 적어 두면 이 검사가 아무 일도 안 한다.** 판매 칸을
  `{"raw_text", "item", ...}` 로 베껴 두면 판매가 칸을 더해도 초록이다 — 그래서
  판매 모델(`model_fields`)을 읽고, 나르는 쪽은 **실제로 전선에 실린 키**로 센다.

★ **마스터 운영 코드는 `app.sales.schemas` 를 import 하지 않는다.** 검사에서만 읽는다
  (`test_sales_flow.py` 의 `Capability` 대조와 같은 자리).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.master import wiring
from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
from app.master.schemas import SalesRunRequest
from app.master.service import run_sales
from app.sales.schemas import SalesUserRequest
from tests.master.logistics_pre_sales import PRE_SALES_PAYLOAD

평일 = date(2026, 9, 10)

안_나르는_칸: dict[str, str] = {
    "preferred_contract_term_days": "요구한 caller 가 없다",
    "allow_additional_sourcing": (
        "매입 라우팅(`ADDITIONAL_SUPPLY_CONTEXT`)이 아직 안 열렸다 — "
        "허용해도 부를 대상이 없어 참을 실으면 안 여는 문을 연 것처럼 읽힌다"
    ),
}
"""**안 나르는 칸과 그 이유.** 이유 없이 이름만 늘리지 않는다 — 이유가 없으면
나르지 않을 근거도 없다는 뜻이다."""


# ── 가짜 부서 ────────────────────────────────────────────────────────────────


def _port(payload: dict[str, Any], 잡은_것: list[tuple[str, dict[str, Any]]]):
    def port(request: AgentRequest) -> tuple[AgentReply, ExecutionMetadata]:
        잡은_것.append((request.agent, dict(request.payload)))
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=f"{request.agent.upper()}-{request.call_seq}",
            runtime_status="READY",
            business_status="ok",
            payload=payload,
        )
        meta = ExecutionMetadata(
            run_id=reply.run_id, request_id=request.context.request_id, agent=request.agent
        )
        return reply, meta

    return port


def _나르는_칸() -> set[str]:
    """**실제로 전선에 실린 키.** 마스터가 줄 수 있는 것을 다 채워 한 번 돌려 본다.

    ★ 코드에서 세지 않고 **보낸 것에서 센다.** `_sales_user_request` 를 직접 읽으면
      진입점이 그 함수를 안 부르게 되는 날에도 이 검사가 초록이다.
    """
    잡은_것: list[tuple[str, dict[str, Any]]] = []
    wiring.reset()
    wiring.register("inventory", _port(PRE_SALES_PAYLOAD, 잡은_것))
    wiring.register("sales", _port({"scenarios": []}, 잡은_것))
    wiring.register("finance", _port({"verdict": "ok"}, 잡은_것))

    run_sales(
        SalesRunRequest(
            as_of=평일,
            policy_version="v1.3",
            business_mode="SPOT_SALES",
            item="배추",
            partner_id="P-1",
            user_request="배추 2톤 다음 주에",
            requested_quantity_kg=Decimal(2000),
            preferred_delivery_date=date(2026, 9, 17),
            preferred_payment_days=30,
            preferred_unit_price_krw=Decimal(2000),
            preferred_payment_terms_type="SINGLE",
            source_ref="sim_runs/TEST#sales_terms/FIXED",
        )
    )

    보낸 = next(payload for agent, payload in 잡은_것 if agent == "sales")
    return set(보낸.get("user_request") or {})


def test_판매_칸은_나르는_것과_안_나르는_것_둘_중_하나다():
    """🔴 **판매가 칸을 더하면 여기가 묻는다** — *"이건 어느 쪽이냐."*

    어느 쪽도 아닌 칸이 생기면 그 칸은 **아무도 안 본 채로** 남는다. 마스터가 안
    나르기로 정한 것과, 있는 줄도 몰라 안 나르는 것은 다르다.
    """
    판매_칸 = set(SalesUserRequest.model_fields)
    나르는 = _나르는_칸()

    미분류 = 판매_칸 - 나르는 - set(안_나르는_칸)
    assert not 미분류, (
        f"판매 `SalesUserRequest` 에 마스터가 모르는 칸이 생겼다: {sorted(미분류)}. "
        "나르기로 정했으면 `SalesRunRequest` 에 자리를 만들고, 안 나르기로 정했으면 "
        "`안_나르는_칸` 에 **이유와 함께** 적는다"
    )


def test_안_나르기로_한_칸이_실제로_안_나간다():
    """★ 목록이 사실과 갈리면 목록이 거짓말을 한다.

    이유를 적어 놓고 실제로는 실어 보내면, 나중에 읽는 사람이 *"저 칸은 안 간다"* 로
    읽은 채 화면을 만든다.
    """
    나르는 = _나르는_칸()

    겹침 = 나르는 & set(안_나르는_칸)
    assert not 겹침, f"안 나르기로 적어 둔 칸을 실제로는 싣고 있다: {sorted(겹침)}"


def test_마스터가_판매에_없는_칸을_지어내지_않는다():
    """🔴 `SalesUserRequest` 는 `extra="forbid"` 다 — 없는 칸 하나면 **요청 전체**가
    문 앞에서 거부된다.
    """
    판매_칸 = set(SalesUserRequest.model_fields)
    나르는 = _나르는_칸()

    assert 나르는 <= 판매_칸, f"판매 모델에 없는 칸을 실었다: {sorted(나르는 - 판매_칸)}"


def test_상업조건_둘은_나르는_쪽이다():
    """🔴 **승인 경로가 요구한 칸이다** (2026-09-08 계약).

    후보 판정이 `delivery_date` · `payment_days` 를 필수로 잡았는데 마스터가 그 둘을
    안 실으면, 판매는 값을 만들 출처가 없어 전 후보가 *"납품일이 없다"* 로 떨어진다 —
    **제시가 통째로 막힌다.**
    """
    나르는 = _나르는_칸()

    assert "preferred_delivery_date" in 나르는
    assert "preferred_payment_days" in 나르는


def test_수량은_나르는_쪽이다():
    """🔴 **판매가 실제로 요구한 칸이다** — 실측에서 `PROPOSAL_QUANTITY_REQUIRED` 로
    되돌아온 그 자리다. 여기 없으면 후보가 0건이라 재무까지 못 간다.
    """
    assert "requested_quantity_kg" in _나르는_칸()


def test_재무가_요구한_셋도_나르는_쪽이다():
    """🔴 **재무 검증이 요구한 칸이다** (2026-09-11 실측).

    재무가 `SALES_INPUT_INCOMPLETE` 로 판정을 못 낸 `missing_fields` 다섯 중,
    마스터가 실을 수 있는 자리가 이 셋이다. 여기 없으면 후보가 서도
    `SL6_VALIDATION_UNRESOLVED` 에서 멈춘다.
    """
    나르는 = _나르는_칸()

    assert "preferred_unit_price_krw" in 나르는
    assert "preferred_payment_terms_type" in 나르는
    assert "source_ref" in 나르는


def test_파생값은_마스터가_안_만든다():
    """🔴 **`reported_sales_amount_krw` 는 판매가 자기 안에서 센다.**

    재무가 요구한 다섯 중 그것만 **수량 × 단가**인 파생이다. 마스터가 같이 실으면
    같은 사실의 주인이 둘이 되고, 판매가 반올림을 바꾸는 날 두 숫자가 조용히 갈린다.

    ★ **판매 요청 모델에 그 칸이 아예 없다**는 것이 그 사실의 증거다.
    """
    assert "reported_sales_amount_krw" not in set(SalesUserRequest.model_fields)
    assert "reported_sales_amount_krw" not in set(SalesRunRequest.model_fields)
