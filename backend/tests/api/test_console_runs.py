"""운영 콘솔 실행 목록 — 저장된 행만, 결정론적 순서로.

🔴 이 목록이 틀리면 **그 뒤의 모든 조회가 남의 실행을 본다.** 그래서 여기서 재는
   것은 «행이 나오는가» 가 아니라 «어느 행이 어떤 순서로 나오는가» 다.
"""

from datetime import UTC, date, datetime

from app.api.console.runs import get_console_runs

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _row(sim_run_id: str, *, activity: datetime | None = _NOW, as_of: date | None = None) -> dict:
    return {
        "sim_run_id": sim_run_id,
        "run_type": "WALK",
        "as_of": as_of or date(2026, 1, 1),
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 9, 9),
        "status": "RUNNING",
        "financing_mode": "LOAN_BASELINE",
        "company_persona_id": "PERSONA-V1.3",
        "started_at": None,
        "finished_at": None,
        "note": None,
        "latest_activity_at": activity,
    }


class _Capture:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.queries: list[tuple[str, list]] = []

    def __call__(self, query, params):
        self.queries.append((str(query), list(params)))
        return list(self.rows)


def test_runs_are_listed_with_their_stored_facts(monkeypatch):
    capture = _Capture([_row("SIM-CONSOLE-A"), _row("SIM-CONSOLE-B")])
    monkeypatch.setattr("app.api.console.runs.fetch_all", capture)
    monkeypatch.setattr("app.api.console.runs.get_db_schema", lambda: "haetdeul")

    rows = get_console_runs().rows

    assert [row.sim_run_id for row in rows] == ["SIM-CONSOLE-A", "SIM-CONSOLE-B"]
    assert rows[0].run_type == "WALK"
    assert rows[0].status == "RUNNING"
    assert rows[0].financing_mode == "LOAN_BASELINE"


def test_the_order_is_deterministic_and_keeps_idle_runs(monkeypatch):
    """🔴 활동이 없는 실행도 목록에 남는다 — 아직 안 걸은 실행을 고를 수 있어야 한다."""
    capture = _Capture([_row("SIM-CONSOLE-A"), _row("SIM-CONSOLE-B", activity=None)])
    monkeypatch.setattr("app.api.console.runs.fetch_all", capture)
    monkeypatch.setattr("app.api.console.runs.get_db_schema", lambda: "haetdeul")

    rows = get_console_runs().rows
    statement, params = capture.queries[0]

    assert "ORDER BY a.latest_activity_at DESC NULLS LAST" in statement
    assert "r.sim_run_id ASC" in statement
    assert params == [100]
    # 🔴 활동이 없는 것은 «없음» 이다. 실행 생성 시각으로 메우지 않는다.
    assert rows[1].latest_activity_at is None


def test_no_run_is_listed_twice(monkeypatch):
    """LATERAL 집계라 실행 하나가 여러 줄로 불어나지 않는다."""
    capture = _Capture([_row("SIM-CONSOLE-A")])
    monkeypatch.setattr("app.api.console.runs.fetch_all", capture)
    monkeypatch.setattr("app.api.console.runs.get_db_schema", lambda: "haetdeul")

    rows = get_console_runs().rows
    identifiers = [row.sim_run_id for row in rows]

    assert len(identifiers) == len(set(identifiers))
    # 집계는 JOIN 이 아니라 LATERAL 한 줄이다 — 행이 불어날 자리가 없다.
    assert "LEFT JOIN LATERAL" in capture.queries[0][0]


def test_an_empty_database_is_an_empty_list_not_an_error(monkeypatch):
    monkeypatch.setattr("app.api.console.runs.fetch_all", _Capture([]))
    monkeypatch.setattr("app.api.console.runs.get_db_schema", lambda: "haetdeul")

    assert get_console_runs().rows == []


def test_the_limit_reaches_sql(monkeypatch):
    capture = _Capture([])
    monkeypatch.setattr("app.api.console.runs.fetch_all", capture)
    monkeypatch.setattr("app.api.console.runs.get_db_schema", lambda: "haetdeul")

    get_console_runs(limit=7)

    assert capture.queries[0][1] == [7]


def test_the_list_never_invents_a_policy_version(monkeypatch):
    """🔴 `sim_runs` 에 정책 버전 칸이 없다. 칸 자체를 두지 않는다 — `null` 도 아니다."""
    monkeypatch.setattr("app.api.console.runs.fetch_all", _Capture([_row("SIM-CONSOLE-A")]))
    monkeypatch.setattr("app.api.console.runs.get_db_schema", lambda: "haetdeul")

    row = get_console_runs().rows[0]

    assert "policy_version" not in row.model_dump()
