"""Sales 가 외부 회신·외부 데이터를 어떻게 읽는가 (Audit 고정).

★ 이 파일이 지키는 것.
    · 검사를 못 돈 것을 검사 성공으로 읽지 않는다 (skipped ≠ ok)
    · 빈 findings 를 PASS 로 읽지 않는다
    · Sales 가 ML DB/Repository/Service 를 직접 조회하지 않는다
    · Sales 가 자체 Forecast fallback/MOCK 을 만들지 않는다

★ 새 로직을 만들지 않았다. 현재 코드가 이미 옳게 읽고 있다는 것을 못 박는 검사다 —
  나중에 누가 "빈 결과 = 통과" 로 바꾸면 여기서 걸린다.
"""

import ast
import pathlib

import pytest

from app.sales.proposal import run_proposal
from app.sales.schemas import SalesProposalInput


def _request(**over):
    data = {
        "business_mode": "SPOT_SALES",
        "user_request": {
            "item": "배추",
            "requested_quantity_kg": 5000,
            "preferred_unit_price_krw": 2000,
            "preferred_delivery_date": "2026-09-10",
        },
        "logistics_context": {
            "query_scope": {"item": "배추", "max_confirmed_sellable_quantity_kg": 3000},
            "sellable_supply": {
                "status": "READY",
                "inventory_by_item": [{"item": "배추", "available_qty_kg": 3000}],
                "supply_capacity_by_date": [
                    {"date": "2026-09-10", "confirmed_sellable_quantity_kg": 3000}
                ],
            },
            "delivery_feasibility": {
                "status": "UNRESOLVED",
                "daily_outbound_capacity_kg": 5000,
                "reason_codes": [],
            },
        },
    }
    data.update(over)
    return SalesProposalInput.model_validate(data)


# ---------------------------------------------------------------------------
# skipped / 미실행을 성공으로 읽지 않는다
# ---------------------------------------------------------------------------


def test_empty_reason_codes_do_not_become_a_pass(monkeypatch):
    """🔴 사유가 비었다고 검사를 통과한 것이 아니다."""
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")

    reply = run_proposal(_request())

    # 사유 목록이 비어 있어도 status 가 READY 가 아니면 검증이 남아 있어야 한다.
    for scenario in reply.scenarios:
        assert "DELIVERY_FEASIBILITY_CONTEXT" in scenario.required_validations
    assert "DELIVERY_FEASIBILITY_CONTEXT" in reply.missing_capabilities


def test_unresolved_delivery_is_not_read_as_ready(monkeypatch):
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")

    reply = run_proposal(_request())

    assert "DELIVERY_FEASIBILITY_CONTEXT" in reply.missing_capabilities


@pytest.mark.parametrize("status", ["UNRESOLVED", "FAIL"])
def test_only_ready_counts_as_a_satisfied_delivery_check(monkeypatch, status):
    """READY 만 통과다 — 그 밖의 상태를 통과 쪽으로 해석하지 않는다.

    상태 어휘 자체가 닫혀 있어(READY|UNRESOLVED|FAIL) 모르는 값은 입구에서 막힌다.
    """
    monkeypatch.setenv("SALES_LLM_ENABLED", "false")
    context = _request().logistics_context.model_dump(mode="json")
    context["delivery_feasibility"]["status"] = status

    reply = run_proposal(_request(logistics_context=context))

    for scenario in reply.scenarios:
        assert "DELIVERY_FEASIBILITY_CONTEXT" in scenario.required_validations


def test_sales_gates_on_ready_positively_not_on_absence_of_findings():
    """소비 코드가 **긍정 조건**(status == READY)으로 판정하는지 구조로 확인한다."""
    source = pathlib.Path("app/sales/proposal.py").read_text(encoding="utf-8")

    # "READY 가 아니면 미충족" 형태가 살아 있어야 한다.
    assert 'status != "READY"' in source or 'status == "READY"' in source


# ---------------------------------------------------------------------------
# ML / Forecast — 타입 의존과 직접 조회를 가른다
# ---------------------------------------------------------------------------


def _imports_of(path: str) -> set[str]:
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_sales_only_depends_on_the_ml_forecast_type():
    """타입 import 는 허용이고, 실행 계층 의존은 금지다."""
    ml_imports = {
        name
        for path in pathlib.Path("app/sales").rglob("*.py")
        for name in _imports_of(str(path))
        if name.startswith("app.ml")
    }

    # 지금 있는 것은 스키마(타입) 하나뿐이다.
    assert ml_imports <= {"app.ml.schemas"}, ml_imports


def test_sales_never_queries_ml_data_directly():
    """Sales 는 받은 ml_context 만 소비한다 — ML 저장소를 직접 읽지 않는다."""
    forbidden = ("app.ml.db", "app.ml.repository", "app.ml.service", "app.ml.runtime")

    for path in pathlib.Path("app/sales").rglob("*.py"):
        imported = _imports_of(str(path))
        for name in forbidden:
            assert not any(item.startswith(name) for item in imported), (path, name)


def test_sales_does_not_build_its_own_forecast_fallback():
    """자체 MOCK/fallback Forecast 를 만들지 않는다 — run 마다 출처가 갈린다."""
    for path in pathlib.Path("app/sales").rglob("*.py"):
        source = pathlib.Path(path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        created = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "Forecast" not in created, path


def test_sales_db_access_is_limited_to_its_own_run_history():
    """Sales 가 여는 DB 경로는 자기 실행이력뿐이다."""
    source = pathlib.Path("app/sales/run_repository.py").read_text(encoding="utf-8")

    tables = set()
    for line in source.splitlines():
        if "haetdeul" in line or "FROM {}" in line or "INTO {}" in line:
            tables.add(line.strip())
    assert all("sales_agent_runs" in line or "{}" in line for line in tables), tables
