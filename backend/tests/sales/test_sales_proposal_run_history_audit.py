"""최종 `/proposal` 실행이력 — **지금은 왜 저장하면 안 되는가.**

★ 이 파일은 두 가지를 나눠서 담는다.

    ① 오래 가는 계약 — 실행 identity 를 **지어내지 않는다**
       오늘 날짜·납기일·예측 생성일·물류 조회일을 실행 기준일로 승격하지 않고,
       가짜 스냅샷 id 를 만들지 않는다. 이건 계약이 갖춰진 뒤에도 그대로다.

    ② 지금의 blocker — 권위 있는 실행 기준일도, 실행 종류를 가를 식별자도 없다
       그래서 **오늘은** 저장하지 않는다.

🔴 ②는 영구 금지가 아니다. "앞으로도 as_of 가 없어야 한다" 거나 "cycle 에 값이
   늘면 안 된다" 는 뜻이 아니다. 계약이 생기면 아래 blocker 검사가 먼저 깨지고,
   그때 이 파일을 **실행이력 저장 회귀 테스트로 전환**하면 된다. 깨지는 것이 신호다.
"""

import pathlib
import re
from typing import get_args

from app.sales.schemas import (
    LogisticsQueryScope,
    SalesContractContext,
    SalesCycle,
    SalesProposalInput,
    SalesProposalReply,
)

_DDL = pathlib.Path(__file__).resolve().parents[3] / "database" / "sales_agent_runs.sql"

_TRANSITION = (
    "권위 있는 Proposal 실행 identity 계약이 생기면 이 blocker 검사를 "
    "실행이력 저장 회귀 테스트로 전환한다."
)


def _ddl() -> str:
    return _DDL.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ① 오래 가는 계약 — identity 를 지어내지 않는다
# ---------------------------------------------------------------------------


def test_proposal_core_does_not_invent_an_execution_date():
    """오늘 날짜·기준일 상수로 실행 시점을 만들지 않는다.

    같은 제안이 돌린 날에 따라 다른 이력을 남기면 그 이력은 재현에 쓸 수 없다.
    이 규율은 실행 기준일 계약이 생긴 뒤에도 그대로다.
    """
    import app.sales.proposal as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")

    for invented in ("date.today()", "datetime.now(", "utcnow(", "sim_start_date"):
        assert invented not in source, invented


def test_proposal_core_does_not_borrow_another_domains_date_as_its_own():
    """다른 도메인의 날짜는 이름이 비슷해도 뜻이 다르다.

        ml_context.as_of              예측을 언제 만들었나
        query_scope.as_of            물류가 무엇을 기준으로 답했나
        contract_delivery_date       언제 납품하나

    셋 다 "이 제안을 언제 세웠나" 가 아니다. 승격하면 이력이 거짓이 된다.
    """
    import app.sales.proposal as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")

    # 승격 흔적: 다른 도메인 날짜를 실행 기준일 이름으로 옮겨 담는 코드
    for promotion in ("as_of =", "as_of=", "run_as_of"):
        assert promotion not in source, promotion


def test_proposal_core_saves_nothing_without_an_execution_identity():
    """identity 가 없는 채로 저장을 흉내내지 않는다 — 못 하는 것을 한 척하지 않는다."""
    import app.sales.proposal as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")

    assert "save_sales_agent_run" not in source
    assert "snapshot_id" not in source


def test_proposal_reply_status_is_not_a_runtime_status():
    """제안 결과의 status 는 실행 runtime 상태가 아니다 — 그대로 넣을 수 없다."""
    annotation = str(SalesProposalReply.model_fields["status"].annotation)

    assert "SCENARIOS_GENERATED" in annotation
    assert "READY" not in annotation


# ---------------------------------------------------------------------------
# ② 지금의 blocker — 계약이 생기면 여기가 먼저 깨진다
# ---------------------------------------------------------------------------


def test_blocker_run_history_currently_requires_a_non_null_as_of():
    """저장소가 실행 기준일을 필수로 요구한다 (현재 스키마 사실)."""
    assert re.search(r"as_of\s+DATE\s+NOT NULL", _ddl()), _TRANSITION


def test_blocker_proposal_has_no_authoritative_execution_date_today():
    """🔴 **오늘은** 제안 입력에 권위 있는 실행 기준일이 없다.

    이 검사는 "as_of 를 추가하면 안 된다" 가 아니다. 추가되면 이 검사가 깨지고,
    그것이 저장을 열어도 된다는 신호다.
    """
    assert "as_of" not in SalesProposalInput.model_fields, _TRANSITION


def test_blocker_no_nested_date_is_an_execution_date_today():
    """중첩 날짜들은 현재 실행 기준일로 쓸 수 없다."""
    # 물류 조회 기준일은 선택 항목이고 뜻도 다르다.
    assert LogisticsQueryScope.model_fields["as_of"].default is None, _TRANSITION
    # 계약 컨텍스트에는 실행 기준일 자체가 없다.
    assert "as_of" not in SalesContractContext.model_fields, _TRANSITION


def test_blocker_execution_kind_cannot_be_distinguished_today():
    """🔴 **오늘의** cycle 어휘로는 제안과 배분을 가를 수 없다.

    배분이 이미 SALES 를 쓰고 있어 같은 값으로 저장하면 두 실행이 이력에서 섞인다.
    새 구분값을 지어내는 것은 공용/DB 계약 결정이라 여기서 할 수 없다.
    이 검사가 깨진다면 구분 수단이 생겼다는 뜻이다.
    """
    from app.sales.schemas import SalesAllocationInput

    assert set(get_args(SalesCycle)) == {"PROCUREMENT", "SALES"}, _TRANSITION
    assert SalesAllocationInput.model_fields["cycle"].default == "SALES", _TRANSITION

    match = re.search(r"cycle\s+IN\s*\(([^)]*)\)", _ddl())
    assert match is not None
    assert set(re.findall(r"'([A-Z_]+)'", match.group(1))) == {
        "PROCUREMENT",
        "SALES",
    }, _TRANSITION


def test_snapshot_id_is_nullable_so_it_need_not_be_invented():
    """스냅샷 id 만은 지어내지 않아도 된다 — 컬럼이 NULL 을 허용한다."""
    assert re.search(r"snapshot_id\s+TEXT\s+NULL", _ddl())
