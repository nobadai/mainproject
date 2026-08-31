"""매입안 보고서 — **화면 밖으로 들고 나갈 수 있는 형태.**

`answer.py` 와 무엇이 다른가:

```text
answer.py   대화창에 붙는 답. 짧고, 결론과 못 본 것만 담는다.
report.py   들고 나가는 문서. 안마다 분할·조달·지급 일정과 근거를 다 편다.
```

★ **값을 만들지 않는다.** 저장된 실행에 있는 것만 옮긴다 — 합계도 다시 세지 않는다.
  보고서가 계산을 시작하면 화면과 문서가 **다른 숫자**를 말하게 된다.

★ **못 한 것을 같이 싣는다.** 지적·확인 필요·못 돈 검사·입력 출처가 안 옆에 있어야
  들고 나간 사람이 그 숫자를 어떻게 읽어야 하는지 안다. **결론만 담은 문서가 가장
  위험하다** — 읽는 사람은 그것을 확정으로 읽는다.

★ Markdown 이다. 붙여 넣기·메신저·이슈 어디에도 그대로 들어간다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.master.answer import agent_label

#: 판정 라벨. `answer.py` 와 같은 어휘를 쓴다 — 두 벌을 두면 언젠가 갈린다.
_VERDICT_LABEL: dict[str, str] = {"ok": "통과", "conditional": "조건부", "reject": "거절"}

#: 근거 등급. 매입이 붙이는 값이라 **모르는 값은 그대로 적는다** — 번역하지 않는다.
_GRADE_NOTE: dict[str, str] = {
    "MEASURED": "실측",
    "SIM_FIXED": "시뮬 고정값",
    "ASSUMED": "가정",
    "DERIVED": "파생",
}


def _won(value: Any) -> str:
    try:
        return f"{int(float(value)):,}원"
    except (TypeError, ValueError):
        return "—"


def _kg(value: Any) -> str:
    try:
        return f"{float(value):,.10g}kg"
    except (TypeError, ValueError):
        return "—"


def _rows(header: Sequence[str], body: Sequence[Sequence[str]]) -> list[str]:
    """표 하나. 본문이 비면 표를 만들지 않는다 — **빈 표는 "없다" 로 안 읽힌다.**"""
    if not body:
        return []
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(cells) + " |" for cells in body]
    return [*out, ""]


def _scenario_block(scenario: Mapping[str, Any], index: int) -> list[str]:
    label = str(scenario.get("label") or f"안 {index + 1}")
    out = [
        f"### {index + 1}. {label}",
        "",
        f"- 수량 **{_kg(scenario.get('total_qty_kg'))}** · 금액 "
        f"**{_won(scenario.get('total_amount_krw'))}**",
    ]
    if scenario.get("strategy_type"):
        out.append(f"- 전략 `{scenario['strategy_type']}` · 커버 {scenario.get('coverage_days')}일")
    if scenario.get("max_price") is not None:
        out.append(f"- 매입 상한 단가 {_won(scenario['max_price'])}/kg")
    # 🔴 `null` 을 0 으로 적지 않는다 — 마진이 없는 것과 마진이 0 인 것은 다르다.
    if scenario.get("expected_margin_rate") is None:
        out.append("- 기대 마진율 — **산출하지 못함**")
    else:
        out.append(f"- 기대 마진율 {float(scenario['expected_margin_rate']) * 100:.1f}%")
    if scenario.get("margin_warning"):
        out.append(f"- ⚠️ {scenario['margin_warning']}")
    out.append("")

    out += _rows(
        ["회차", "발주일", "수량"],
        [
            [str(p.get("seq", i + 1)), str(p.get("date", "—")), _kg(p.get("qty_kg"))]
            for i, p in enumerate(scenario.get("split_plan") or [])
        ],
    )
    out += _rows(
        ["시장", "등급", "수량", "단가"],
        [
            [
                str(s.get("market", "—")),
                str(s.get("grade", "—")),
                _kg(s.get("qty_kg")),
                _won(s.get("grade_unit_price")),
            ]
            for s in (scenario.get("sourcing_plan") or [])
        ],
    )
    out += _rows(
        ["회차", "매입일", "지급일", "수량", "금액", "최대 금액", "근거"],
        [
            [
                str(p.get("seq", i + 1)),
                str(p.get("purchase_date", "—")),
                str(p.get("payment_date", "—")),
                _kg(p.get("qty_kg")),
                _won(p.get("amount_krw")),
                _won(p.get("amount_max_krw")),
                str(p.get("basis", "—")),
            ]
            for i, p in enumerate(scenario.get("payment_schedule") or [])
        ],
    )

    rationale = scenario.get("rationale") or []
    if rationale:
        out.append("**근거**")
        out.append("")
        for item in rationale:
            grade = str(item.get("evidence_grade") or "")
            note = _GRADE_NOTE.get(grade, grade)
            ref = item.get("ref_id")
            tail = f" · `{ref}`" if ref else ""
            out.append(f"- [{item.get('source', '—')}] {item.get('claim', '')} — {note}{tail}")
        out.append("")

    # 🔴 위험을 안 옆에 둔다. 뒤에 몰아 두면 안만 보고 결정한다.
    risks = scenario.get("risks") or []
    if risks:
        out.append("**이 안의 위험**")
        out.append("")
        out += [f"- {r}" for r in risks]
        out.append("")
    return out


def render_report(run: Mapping[str, Any]) -> str:
    """저장된 실행 하나 → 보고서 Markdown.

    ★ 실행이 안을 안 냈으면 **왜 없는지**가 보고서의 본문이다 (`E2_HELD`).
      빈 문서를 내면 들고 나간 사람이 "아직 안 돌았나" 로 읽는다.
    """
    scenarios = list(run.get("scenarios") or [])
    judgment = run.get("judgment") or {}
    out = [
        f"# 매입안 보고서 — {run.get('request_id', '')}",
        "",
        f"> 기준일 {run.get('as_of', '')} · 종료 코드 `{run.get('end_code', '')}` · "
        f"안 {len(scenarios)}개",
        "> **이 문서는 판단 기록이지 발주가 아닙니다.**",
        "",
    ]

    if run.get("reason"):
        out += ["## 결론", "", str(run["reason"]), ""]
    if not scenarios and judgment.get("no_proposal_reason"):
        out += [f"**매입:** {judgment['no_proposal_reason']}", ""]
        for item in judgment.get("rejected_reasons") or []:
            out.append(f"- **{item.get('label', '안')}** — {item.get('reason', '')}")
        out.append("")

    sources = run.get("input_sources") or {}
    if sources:
        out += ["## 입력 출처", ""]
        out += [f"- `{key}` — {value}" for key, value in sources.items()]
        out.append("")
    # 🔴 mock 경고를 결론과 같은 문서에 둔다. 빼면 이 숫자가 실측으로 읽힌다.
    if run.get("mocked_inputs"):
        out += [
            f"> 🔴 **{', '.join(run['mocked_inputs'])} 는 mock 에서 왔습니다** — "
            "이 결론을 실측으로 읽지 마십시오.",
            "",
        ]

    verdicts = run.get("verdicts") or {}
    if verdicts:
        out += ["## 부서 판정", ""]
        for agent, verdict in verdicts.items():
            status = str(verdict.get("business_status"))
            label = _VERDICT_LABEL.get(status)
            if label is not None:
                out.append(f"- {agent_label(agent)} — **{label}**")
                continue
            # 🔴 영어 코드를 그대로 찍지 않는다. 종전에는 `skipped` 가 문서에 그대로
            #   나가서, 같은 상태를 화면은 **침묵**하고 문서는 **영어**로 말했다.
            #   들고 나간 사람이 이것을 판정 하나로 읽는다 (실측 2026-08-31).
            why = str(verdict.get("reasoning") or "").strip()
            out.append(
                f"- {agent_label(agent)} — **판정을 내지 못함** "
                f"(`{status}`) — {why or '사유 미기재'}"
            )
        out.append("")

    if scenarios:
        out += ["## 제시한 안", ""]
        for index, scenario in enumerate(scenarios):
            out += _scenario_block(scenario, index)

    # ★ 검증은 **분수로** 낸다. "지적 0건" 만 쓰면 전부 통과한 것처럼 읽힌다.
    out += [
        "## 검증",
        "",
        f"- 지적 {len(run.get('findings') or [])}건 · "
        f"판정하지 못한 검사 {len(run.get('skipped_checks') or [])}건",
        "",
    ]
    for finding in run.get("findings") or []:
        out.append(f"- 🔴 지적: {finding}")
    for concern in run.get("concerns") or []:
        out.append(f"- 확인 필요: {concern}")
    for skipped in run.get("skipped_checks") or []:
        out.append(f"- 못 돈 검사: {skipped}")
    out.append("")

    return "\n".join(out).rstrip() + "\n"


def report_filename(run: Mapping[str, Any]) -> str:
    """`REQ-20251231-0001_2025-12-31_매입안.md`. 날짜를 넣어 여러 장이 안 겹치게 한다."""
    return f"{run.get('request_id', 'run')}_{run.get('as_of', '')}_매입안.md"
