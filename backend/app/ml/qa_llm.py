"""예측 질의응답 — 질문 해석 한 번.

★ **LLM 은 고르기만 한다.** 어느 도구를 부를지, 품목·가격 종류가 무엇인지, 날짜가
  언제인지를 고른다. 숫자를 만들지 않고, 답변 문장도 쓰지 않는다.

🔴 **여기서 고른 값을 그대로 쿼리에 넣지 않는다.** `qa_graph.gate` 가 다시 검사한다.
   응답 스키마에 enum 을 걸어도 «범위 안» 까지 보장되지는 않는다 — 날짜가 18일을
   넘는지, 그 행이 실제로 있는지는 표가 답할 일이다.

부르는 방식은 **다른 파트와 같게** 맞췄다 (`app/logistics/llm/runtime.py`).

```text
호출     urllib 로 직접 · x-goog-api-key 헤더
키       ML_GEMINI_API_KEY → 없으면 GEMINI_API_KEY
모델     ML_LLM_MODEL → 없으면 gemini-3.5-flash-lite (stable 고정)
         ★ latest·preview 같은 자동 갱신 별칭은 출력 성향이 예고 없이 바뀌어 금지
온도     0 · 응답은 JSON 스키마로 받는다
```

★ **실패를 삼키지 않는다.** 키가 없거나 호출이 실패하면 `None` 을 돌려주고,
  그래프가 «해석하지 못했습니다» 로 답한다. 그럴듯한 값을 지어내지 않는다.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

#: backend/.env 와 저장소 루트 .env 를 순서대로 읽는다 (마스터·물류와 같은 패턴).
_ENV_FILES = (
    Path(__file__).resolve().parents[2] / ".env",
    Path(__file__).resolve().parents[3] / ".env",
)
_ENV_PREFIX = "ML_"
_DEFAULT_MODEL = "gemini-3.5-flash-lite"
_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_TIMEOUT_SECONDS = 8.0

SYSTEM_PROMPT = """너는 농산물 가격 예측 질의응답의 해석 층이다.
사용자 질문에서 **무엇을 물었는지만** 골라낸다. 가격을 추정하지 말고, 설명 문장도 쓰지 마라.

고를 것
  route  forecast(값) · accuracy(얼마나 맞나) · usability(써도 되나)
         · clarify(못 고르겠다) · out_of_scope(우리 품목이 아님)
  item   배추 · 무 · 양파 중 하나. 질문에 없으면 비운다
  kind   AUC(경락가·매입) · WHSL(중도매가) · RTL(소매가) 중 하나. 질문에 없으면 비운다
  dates  질문이 가리키는 날짜를 ISO 형식으로. 「오늘」은 기준일, 「내일」은 기준일+1,
         「10일 뒤」는 기준일+10 이다. 여러 개면 모두 적는다. 없으면 비운다

규칙
  · 배추·무·양파가 아닌 품목(마늘·대파 등)이면 route 를 out_of_scope 로 둔다
  · 품목이나 가격 종류를 못 고르겠으면 비워 두고 route 는 forecast 로 둔다 —
    되묻거나 전부 보여주는 판단은 우리 규칙이 한다
  · 날짜를 못 고르겠으면 비워 둔다. 임의로 채우지 마라
"""

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "route": {
            "type": "string",
            "enum": ["forecast", "accuracy", "usability", "clarify", "out_of_scope"],
        },
        #   ★ enum 에 빈 문자열을 넣으면 Gemini 가 400 을 낸다 (enum[n]: cannot be empty).
        #     «못 골랐다» 는 값을 비워서(= 이 칸을 빼서) 말한다. required 에 없다.
        "item": {"type": "string", "enum": ["배추", "무", "양파"], "nullable": True},
        "kind": {"type": "string", "enum": ["AUC", "WHSL", "RTL"], "nullable": True},
        "dates": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["route"],
}


def _model() -> str:
    return os.getenv(f"{_ENV_PREFIX}LLM_MODEL", "").strip() or _DEFAULT_MODEL


def _api_key() -> str:
    for path in _ENV_FILES:
        if path.exists():
            load_dotenv(path, override=False)
    return (os.getenv(f"{_ENV_PREFIX}GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()


def enabled() -> bool:
    """키가 있어야 부른다. `ML_LLM_ENABLED=0` 으로 끌 수 있다."""
    if os.getenv(f"{_ENV_PREFIX}LLM_ENABLED", "1").strip() in {"0", "false", "False"}:
        return False
    return bool(_api_key())


def _parse_dates(raw: Any) -> list[date]:
    """날짜만 걸러 낸다. 형식이 틀린 것은 **버린다** — 고쳐서 쓰지 않는다."""
    out: list[date] = []
    for value in raw if isinstance(raw, list) else []:
        try:
            out.append(date.fromisoformat(str(value)[:10]))
        except ValueError:
            continue
    return out


def interpret(question: str, base_dt: date) -> dict[str, Any] | None:
    """질문 → `{route, item, kind, dates}`. 못 부르거나 못 읽으면 `None`.

    `base_dt` 를 같이 준다 — 「내일」이 며칠인지는 기준일이 있어야 정해진다.
    """
    key = _api_key()
    if not key:
        return None
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": json.dumps(
                            {"question": question, "base_dt": base_dt.isoformat()},
                            ensure_ascii=False,
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": _RESPONSE_SCHEMA,
        },
    }
    base = (os.getenv(f"{_ENV_PREFIX}GEMINI_BASE_URL") or _BASE_URL).rstrip("/")
    request = urllib.request.Request(
        f"{base}/models/{_model()}:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            document = json.loads(response.read().decode("utf-8"))
        text = document["candidates"][0]["content"]["parts"][0]["text"]
        chosen = json.loads(text)
    except Exception:                                        # noqa: BLE001
        #   ★ 오류 문구를 밖으로 흘리지 않는다. 접속 정보가 오류에 실려 나온 적이 있다.
        return None
    if not isinstance(chosen, dict):
        return None
    return {
        "route": str(chosen.get("route") or "forecast"),
        "item": (chosen.get("item") or "").strip() or None,
        "kind": (chosen.get("kind") or "").strip() or None,
        "dates": _parse_dates(chosen.get("dates")),
    }
