from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

import pytest
from psycopg import OperationalError
from psycopg.types.json import Jsonb

from app.finance.run_repository import (
    get_finance_agent_run,
    list_finance_agent_runs,
    save_finance_agent_run,
)
from app.finance.schemas import PurchaseAgentOutput
from app.finance.service import run_finance_procurement

#: `patch()` 대상 모듈 경로 — 소유 모듈을 직접 가리킨다.
_LEGACY_SERVICE = "app.finance.legacy.deterministic_service"


def _run_row() -> dict[str, object]:
    return {
        "run_id": UUID("00000000-0000-0000-0000-000000000001"),
        "cycle": "PROCUREMENT",
        "as_of": date(2026, 8, 21),
        "snapshot_id": "FIN-DAY30-LOAN",
        "runtime_status": "RUNTIME_NOT_READY",
        "verdict": None,
        "request_payload": {"meta": {"as_of": "2026-08-21"}},
        "response_payload": {"runtime_status": "RUNTIME_NOT_READY", "verdict": None},
        "created_at": datetime(2026, 8, 21, tzinfo=UTC),
    }


def test_save_finance_agent_run_uses_jsonb_and_preserves_metadata():
    row = _run_row()
    with (
        patch("app.finance.run_repository.get_db_schema", return_value="haetdeul"),
        patch("app.finance.run_repository.execute_returning_one", return_value=row) as execute,
    ):
        saved = save_finance_agent_run(
            cycle="PROCUREMENT",
            as_of=date(2026, 8, 21),
            snapshot_id="FIN-DAY30-LOAN",
            runtime_status="RUNTIME_NOT_READY",
            verdict=None,
            request_payload=row["request_payload"],
            response_payload=row["response_payload"],
        )

    params = execute.call_args.args[1]
    assert saved == row
    assert params[1:5] == (
        "PROCUREMENT",
        date(2026, 8, 21),
        "FIN-DAY30-LOAN",
        "RUNTIME_NOT_READY",
    )
    assert params[5] is None
    assert isinstance(params[6], Jsonb)
    assert isinstance(params[7], Jsonb)


def test_list_finance_agent_runs_passes_filters_and_limit():
    row = _run_row()
    with (
        patch("app.finance.run_repository.get_db_schema", return_value="haetdeul"),
        patch("app.finance.run_repository.fetch_all", return_value=[row]) as fetch,
    ):
        rows = list_finance_agent_runs(
            cycle="PROCUREMENT",
            as_of=date(2026, 8, 21),
            runtime_status="RUNTIME_NOT_READY",
            verdict="FAIL",
            limit=25,
        )

    assert rows == [row]
    assert fetch.call_args.args[1] == [
        "PROCUREMENT",
        date(2026, 8, 21),
        "RUNTIME_NOT_READY",
        "FAIL",
        25,
    ]


def test_save_finance_agent_run_rejects_verdict_metadata_mismatch():
    with pytest.raises(ValueError, match="must match"):
        save_finance_agent_run(
            cycle="PROCUREMENT",
            as_of=date(2026, 8, 21),
            snapshot_id=None,
            runtime_status="READY",
            verdict="PASS",
            request_payload={},
            response_payload={"verdict": "FAIL"},
        )


def test_get_finance_agent_run_raises_lookup_error():
    with (
        patch("app.finance.run_repository.get_db_schema", return_value="haetdeul"),
        patch("app.finance.run_repository.fetch_one", return_value=None),
        pytest.raises(LookupError),
    ):
        get_finance_agent_run(UUID("00000000-0000-0000-0000-000000000001"))


def test_as_of_mismatch_response_is_saved(finance_context, purchase_payload):
    purchase_payload["meta"]["as_of"] = "2026-08-21"
    purchase_payload["scenarios"][0]["split_plan"][0]["date"] = "2026-08-21"
    request = PurchaseAgentOutput.model_validate(purchase_payload)

    with (
        patch(
            f"{_LEGACY_SERVICE}.get_current_finance_runtime_context", return_value=finance_context
        ),
        patch(f"{_LEGACY_SERVICE}.save_finance_agent_run") as save_run,
    ):
        response = run_finance_procurement(request)

    assert response.runtime_status == "RUNTIME_NOT_READY"
    assert response.hard_constraints == ["AS_OF_MISMATCH"]
    saved = save_run.call_args.kwargs
    assert saved["runtime_status"] == "RUNTIME_NOT_READY"
    assert saved["verdict"] is None
    assert saved["response_payload"]["verdict"] is None
    assert saved["response_payload"]["hard_constraints"] == ["AS_OF_MISMATCH"]
    assert saved["response_payload"]["soft_warnings"] == []


def test_persistence_error_is_not_converted_to_runtime_warning(finance_context, purchase_payload):
    request = PurchaseAgentOutput.model_validate(purchase_payload)

    with (
        patch(
            f"{_LEGACY_SERVICE}.get_current_finance_runtime_context", return_value=finance_context
        ),
        patch(
            f"{_LEGACY_SERVICE}.save_finance_agent_run",
            side_effect=OperationalError("persistence unavailable"),
        ),
        pytest.raises(OperationalError, match="persistence unavailable"),
    ):
        run_finance_procurement(request)


def test_finance_calculation_remains_decimal_before_json_serialization(
    finance_context, purchase_payload
):
    request = PurchaseAgentOutput.model_validate(purchase_payload)

    with (
        patch(
            f"{_LEGACY_SERVICE}.get_current_finance_runtime_context", return_value=finance_context
        ),
        patch(f"{_LEGACY_SERVICE}.save_finance_agent_run") as save_run,
    ):
        response = run_finance_procurement(request)

    assert response.band.max_feasible_amount_krw == Decimal(6111353)
    assert (
        save_run.call_args.kwargs["response_payload"]["band"]["max_feasible_amount_krw"]
        == "6111353"
    )
