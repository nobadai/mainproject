from datetime import UTC, date, datetime
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.logistics.run_repository import list_logistics_agent_runs, save_logistics_agent_run
from app.main import app


def _run_row() -> dict[str, object]:
    return {
        "run_id": UUID("00000000-0000-0000-0000-000000000001"),
        "cycle": "PROCUREMENT",
        "as_of": date(2026, 8, 21),
        "snapshot_id": None,
        "runtime_status": "RUNTIME_NOT_READY",
        "verdict": None,
        "request_payload": {},
        "response_payload": {"verdict": None},
        "created_at": datetime(2026, 8, 21, tzinfo=UTC),
    }


def test_run_repository_uses_jsonb_and_metadata():
    row = _run_row()
    with (
        patch("app.logistics.run_repository.get_db_schema", return_value="haetdeul"),
        patch(
            "app.logistics.run_repository.execute_returning_one",
            return_value=row,
        ) as execute,
    ):
        saved = save_logistics_agent_run(
            cycle="PROCUREMENT",
            as_of=date(2026, 8, 21),
            snapshot_id=None,
            runtime_status="RUNTIME_NOT_READY",
            verdict=None,
            request_payload={},
            response_payload=row["response_payload"],
        )

    params = execute.call_args.args[1]
    assert saved == row
    assert params[1:5] == ("PROCUREMENT", date(2026, 8, 21), None, "RUNTIME_NOT_READY")
    assert params[5] is None
    assert isinstance(params[6], Jsonb)
    assert isinstance(params[7], Jsonb)


def test_run_repository_filters_by_verdict():
    row = _run_row()
    with (
        patch("app.logistics.run_repository.get_db_schema", return_value="haetdeul"),
        patch("app.logistics.run_repository.fetch_all", return_value=[row]) as fetch,
    ):
        assert list_logistics_agent_runs(verdict="REVIEW_REQUIRED") == [row]

    assert fetch.call_args.args[1] == ["REVIEW_REQUIRED", 100]


def test_run_repository_rejects_verdict_metadata_mismatch():
    with pytest.raises(ValueError, match="must match"):
        save_logistics_agent_run(
            cycle="PROCUREMENT",
            as_of=date(2026, 8, 21),
            snapshot_id=None,
            runtime_status="READY",
            verdict="PASS",
            request_payload={},
            response_payload={"verdict": "FAIL"},
        )


# ── 물류 HTTP 경계 ──────────────────────────────────────────────────────


def test_물류에는_자기_HTTP_라우터가_없다():
    """🔴 **물류 HTTP 경계는 `/api/logistics` 하나다** (2026-09-15 · 물류 문서 28).

    종전에는 `app/logistics/router.py` 가 `/logistics/…` 16 경로를 냈다. 그런데

    ```text
    화면    /api/logistics 를 친다 (app/api/logistics/routes.py)   ← 프론트 진입점
    마스터  adapter.logistics_port 를 **파이썬으로** 부른다          ← HTTP 가 아니다
            (master/bootstrap.py 의 register_agent("inventory", logistics_port))
    ```

    라서 그 16 경로를 **아무도 안 불렀다.** 같은 콘솔 조회가 두 주소로 나가면 어느 쪽이
    정본인지 갈리므로 걷어냈다.

    ⚠️ 종전 이 자리의 검사는 `/logistics/procurement` 등이 **등록돼 있는지**를 봤다.
       지금은 그 반대를 잠근다 — 되살아나면 경계가 다시 둘이 된다.
    """
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    물류 = sorted(p for p in paths if "logistics" in p)

    assert 물류 == ["/api/logistics"], 물류
