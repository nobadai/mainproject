"""프로바이더 공통화 — **역할이 늘어도 ⑤ 가 그대로인가.**

🔴 이 파일이 지키는 것은 두 가지다.

① **기본 인자로 만든 프로바이더는 ⑤ 명세를 든다** — 지시문도 응답 스키마도 옛 것 그대로다.
② **역할별 분기가 프로바이더 안에 없다** — 프로바이더는 *"이 지시문과 이 스키마로 한 번
   물어본다"* 까지만 하고 무엇을 묻는지는 모른다.

★ ②를 안 잠그면 역할이 늘 때마다 세 프로바이더를 다 고치게 되고, 그 셋은 SDK 사정으로
이미 서로 다르다 — 한 역할의 사정이 다른 역할의 호출을 깨는 자리가 생긴다.
"""

import ast
import json
from pathlib import Path

import pytest

from app.purchase_agent.llm import runtime as rt
from app.purchase_agent.llm.mix import build_mix_context
from app.purchase_agent.llm.schemas import MixCandidate


def _context():
    return build_mix_context(
        "배추",
        spread_widened=True,
        shelf_days=6.0,
        shelf_tight=True,
        signals=["GRADE_SPREAD_WIDENED"],
        facts=["등급 스프레드가 평시보다 확대됐다."],
        candidates=[MixCandidate(candidate_id="BASE_ONLY", summary="기본")],
    )


def test_역할_명세가_기존_상수를_그대로_든다() -> None:
    """🔴 **값을 새로 짓지 않았다** — 담는 그릇만 생겼다."""
    assert rt.MIX_ROLE.system_prompt is rt.SYSTEM_PROMPT
    assert json.dumps(rt.MIX_ROLE.response_schema, sort_keys=True) == json.dumps(
        rt._response_schema(), sort_keys=True
    )


@pytest.mark.parametrize("provider_name", ["anthropic", "openai", "ollama"])
def test_기본_인자로_만들면_다섯번_역할이다(provider_name: str) -> None:
    """조립하던 자리(``get_mix_selection_service``)는 인자를 안 넘긴다 — 기본이 ⑤ 여야 한다."""
    provider = rt._PROVIDERS[provider_name](rt.get_llm_settings())
    assert provider.spec is rt.MIX_ROLE


def test_보내는_본문이_예전과_같다() -> None:
    """지시문·스키마가 아니라 **본문**도 안 바뀌었는지 본다."""
    context = _context()
    assert rt._user_payload(context, None) == json.dumps(
        {"context": context.model_dump(mode="json")}, ensure_ascii=False
    )
    assert rt._user_payload(context, ["다시"]) == json.dumps(
        {"context": context.model_dump(mode="json"), "correction": ["다시"]},
        ensure_ascii=False,
    )


class _터짐:
    def generate(self, context, *, retry_guidance=None):
        raise RuntimeError("키가 없다")


class _됨:
    def generate(self, context, *, retry_guidance=None):
        return "ok"


def _설정(*, enabled: bool):
    return type(
        "S",
        (),
        {
            "enabled": enabled,
            "max_retries": 1,
            "provider": "p",
            "model": "m",
            "reason_max_chars": 300,
        },
    )()


@pytest.mark.parametrize(
    ("라벨", "켬", "프로바이더", "부를조건", "기대"),
    [
        ("설정 꺼짐", False, _됨(), True, ("기본안", "DISABLED", 0, False)),
        ("부를 조건 아님", True, _됨(), False, ("기본안", "SKIPPED_TEMPLATE", 0, False)),
        ("성공", True, _됨(), True, ("해석", "SUCCESS", 1, False)),
        ("전면 실패", True, _터짐(), True, ("기본안", "FALLBACK", 2, True)),
    ],
)
def test_골격이_상태_넷을_옛_규칙대로_낸다(
    라벨: str, 켬: bool, 프로바이더: object, 부를조건: bool, 기대: tuple
) -> None:
    """🔴 **이 넷이 ⑤ 가 쓰던 규칙 그대로다.**

    ``FALLBACK`` 의 시도 수가 ``max_retries + 1`` 인 것도 그대로다 — 재시도를 두 층이
    각자 세면 상한이 곱해진다.
    """
    assert (
        rt.run_with_fallback(
            settings=_설정(enabled=켬),
            provider=프로바이더,
            context=_context(),
            template="기본안",
            validate=lambda raw: "해석",
            needs_call=부를조건,
            guidance_for=lambda error: ["다시"],
        )
        == 기대
    ), 라벨


def test_프로바이더_안에_역할_이름이_없다() -> None:
    """🔴 **역할별 분기를 공통 층에 안 넣는다.**

    프로바이더가 역할 이름을 알면 역할이 늘 때마다 셋을 다 고치게 된다. 아는 것은
    ``self.spec`` 하나여야 한다.
    """
    소스 = Path(rt.__file__).read_text(encoding="utf-8")
    tree = ast.parse(소스)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("Provider"):
            continue
        몸통 = ast.get_source_segment(소스, node) or ""
        for 금지 in ("split_allocation", "rationale_self_review", "sourcing_selection"):
            assert 금지 not in 몸통, f"{node.name} 이 역할 이름을 안다: {금지}"
