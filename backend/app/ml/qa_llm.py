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
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.ml.qa_schemas import QA_ITEMS, QA_KINDS

#: backend/.env 와 저장소 루트 .env 를 순서대로 읽는다 (마스터·물류와 같은 패턴).
_ENV_FILES = (
    Path(__file__).resolve().parents[2] / ".env",
    Path(__file__).resolve().parents[3] / ".env",
)
_ENV_PREFIX = "ML_"
_DEFAULT_MODEL = "gemini-3.5-flash-lite"
_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_TIMEOUT_SECONDS = 8.0

SYSTEM_PROMPT_KO = """너는 농산물 가격 예측 질의응답의 해석 층이다.
사용자 질문에서 **무엇을 물었는지만** 골라낸다. 가격을 추정하지 말고, 설명 문장도 쓰지 마라.

고를 것
  route  forecast(값) · accuracy(얼마나 맞나) · usability(써도 되나)
         · clarify(못 고르겠다) · out_of_scope(우리 품목이 아님)
  items  배추 · 무 · 양파 중 **질문에 실제로 나온 것만**. 없으면 빈 배열
         🔴 질문에 없는 품목을 채우지 마라. 하나만 나왔으면 하나만 적는다
         「배추랑 무」면 둘 · 「전 품목」·「다」라고 할 때만 셋
  kinds  AUC · WHSL · RTL 중 **질문에 실제로 나온 것만**. 없으면 빈 배열
         🔴 **가장 흔한 실수다.** 「도매가」 하나만 물었는데 셋을 다 적으면 안 된다.
            나온 낱말 수만큼만 적는다 — 하나 나왔으면 하나, 둘 나왔으면 둘
         ★ 낱말 풀이 — 사람마다 다르게 부른다. 이대로 고른다
           경락가 · 경매가 · 낙찰가 · 매입가        → AUC
           도매가 · 중도매가 · 도매시장가           → WHSL   (**소매가가 아니다**)
           소매가 · 마트가 · 소비자가               → RTL
         「경락가랑 도매가」    → ["AUC", "WHSL"]      두 개
         「도매가」             → ["WHSL"]             한 개
         「가격 전부」·「모든 가격」 → ["AUC","WHSL","RTL"]  이때만 셋
  dates  질문이 가리키는 날짜를 ISO 형식으로. 「오늘」은 기준일, 「내일」은 기준일+1,
         「10일 뒤」는 기준일+10 이다. 여러 개면 모두 적는다. 없으면 비운다
  asks   ★ **품목·가격·날짜가 짝지어진 물음**이 여럿일 때 쓴다. 한 물음이 한 칸이다
         「5일 뒤 배추 경락가와 7일 뒤 무 도매가」
           → [{item:배추, kind:AUC, dates:[기준일+5]},
              {item:무,   kind:WHSL, dates:[기준일+7]}]
         짝이 안 갈리는 질문(「배추 경락가랑 도매가 내일」)은 asks 를 비우고
         items·kinds·dates 만 채운다 — 그건 우리가 곱해서 본다

규칙
  · 배추·무·양파가 아닌 품목(마늘·대파 등)이면 route 를 out_of_scope 로 둔다
  · 품목이나 가격 종류를 못 고르겠으면 빈 배열로 두고 route 는 forecast 로 둔다 —
    되묻거나 전부 보여주는 판단은 우리 규칙이 한다
  · 날짜를 못 고르겠으면 비워 둔다. 임의로 채우지 마라
"""

#: 같은 지시를 영어로 옮긴 것. **뜻을 바꾸지 않았다** — 순서·항목·규칙이 같다.
#:
#: 왜 두 벌을 두나: 「영어 프롬프트가 낫다」 는 말은 흔한데 우리는 한 번도 안 쟀다.
#: 재려면 **지시문 언어만** 다르고 나머지가 같은 짝이 있어야 한다. 모델·온도·
#: 응답 스키마·질문은 그대로 둔다. 채점은 `app/ml/ops/qa_prompt_bench.py` 가 한다.
SYSTEM_PROMPT_EN = """You are the interpretation layer of a crop price forecast Q&A system.
From the user's question, pick out **only what was asked**. Do not estimate a price,
and do not write any explanatory sentence.

What to pick
  route  forecast (a value) · accuracy (how accurate it is) · usability (safe to use)
         · clarify (cannot decide) · out_of_scope (not one of our crops)
  items  **only the crops actually named** in the question, from 배추 · 무 · 양파.
         Empty array if none. Do not add a crop the question did not name.
  kinds  **only the price series actually named**, from AUC (auction) ·
         WHSL (wholesale) · RTL (retail). Empty array if none.
         🔴 Most common mistake: naming one ("도매가") and answering with all three.
         ★ Korean wording maps like this — 경락가·경매가·낙찰가·매입가 → AUC ·
           도매가·중도매가 → WHSL (**not retail**) · 소매가·마트가 → RTL.
         "경락가랑 도매가" is AUC and WHSL **only**, not all three.
  dates  the dates the question refers to, in ISO format. "today" is the base date,
         "tomorrow" is base date + 1, "in 10 days" is base date + 10. List all of them.
         Leave empty if there are none.
  asks   ★ use this when the question pairs a crop, a price and a date **per ask**.
         "배추 경락가 in 5 days and 무 도매가 in 7 days"
           → [{item:배추, kind:AUC, dates:[base+5]},
              {item:무,   kind:WHSL, dates:[base+7]}]
         Leave `asks` empty when the parts are not paired — then fill items·kinds·dates
         and we take every combination ourselves.

Rules
  · If the crop is not 배추, 무 or 양파 (garlic, spring onion and so on),
    set route to out_of_scope
  · If you cannot decide the crop or the price kind, leave it empty and set route to
    forecast — whether to ask back or to show everything is decided by our own rules
  · If you cannot decide a date, leave it empty. Do not fill one in arbitrarily

The question may be written in Korean. Answer with the JSON schema only.
"""


def _prompt(base_dt: date) -> str:
    """지시문. 언어는 기본 한국어이고, enum 판이면 **고를 수 있는 날을 붙인다.**

    ★ 범위(「모든 날」·「5일 뒤까지」)를 코드가 세지 않는다. **LLM 이 목록에서 골라야
      한다** — 그걸 얼마나 잘하는지가 이 실험에서 재려는 것이다.
    """
    lang = os.getenv(f"{_ENV_PREFIX}LLM_PROMPT_LANG", "ko").strip().lower()
    prompt = SYSTEM_PROMPT_EN if lang == "en" else SYSTEM_PROMPT_KO
    if not _date_enum_on():
        return prompt
    days = _selectable(base_dt)
    return "\n".join(
        [
            prompt,
            "고를 수 있는 날짜는 아래 19개뿐이다. **맨 앞이 오늘**이고 그 뒤가 내일부터다.",
            "이 목록 밖의 날짜는 만들지 마라.",
            "  " + " · ".join(days),
            #   ★ 「모든 날」에 오늘을 **넣는다** (2026-09-15 · 사용자 지적으로 고침).
            #     전에는 빼게 했다 — 전달표가 D+1 부터라서였다. 그런데 그건 우리 창고
            #     사정이지 묻는 사람의 뜻이 아니다. 「전부」라고 하면 오늘부터다.
            "「모든 날」·「전부」면 **오늘을 포함해 19개를 다** 적는다.",
            "「내일부터」라고 하면 오늘을 뺀다. 「5일 뒤까지」면 내일부터 5개다.",
            #   ★ 「N일치」가 빠져 있었다 (2026-09-15 실측). 「일주일치 배추 경락가」에
            #     날짜를 비워 내 «날짜를 안 말씀하셨다» 로 답했다. 뜻은 「모든 날」과
            #     맞춰 **오늘부터 N개**로 둔다.
            "「일주일치」·「앞으로 일주일」이면 **오늘부터 7개**, 「3일치」면 오늘부터 3개다.",
            "「이번 주」도 오늘부터 7개로 본다. 기간을 말했으면 날짜를 **비우지 마라**.",
            "★ 이 목록 **밖의 날**을 물었으면 dates 에 넣지 말고 **far_offsets** 에 오늘로부터",
            "  며칠인지 정수로 적는다. 「30일 뒤」→ [30] · 「한 달 뒤」→ [30] · 「어제」→ [-1].",
            "  목록 밖이라고 날짜를 **비우면 안 된다** — 비우면 «날짜를 안 물었다» 로 읽힌다.",
            "",
        ]
    )


#: 예전 이름으로 부르던 곳이 있으면 한국어판을 가리킨다.
SYSTEM_PROMPT = SYSTEM_PROMPT_KO

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "route": {
            "type": "string",
            "enum": ["forecast", "accuracy", "usability", "clarify", "out_of_scope"],
        },
        #   ★ enum 에 빈 문자열을 넣으면 Gemini 가 400 을 낸다 (enum[n]: cannot be empty).
        #     «못 골랐다» 는 값을 비워서(= 이 칸을 빼서) 말한다. required 에 없다.
        #   ★ **배열이다** (2026-09-15). 「배추 경락가랑 도매가」처럼 여럿을 묻는 질문에
        #     한 칸짜리로는 하나만 답하게 된다 — 실제로 중도매가만 나갔다.
        #     «못 골랐다» 는 빈 배열로 말한다. nullable 이 필요 없어졌다.
        "items": {"type": "array", "items": {"type": "string",
                                             "enum": ["배추", "무", "양파"]}},
        "kinds": {"type": "array", "items": {"type": "string",
                                             "enum": ["AUC", "WHSL", "RTL"]}},
        "dates": {"type": "array", "items": {"type": "string"}},
        #   ★ **목록 밖 날짜를 적는 칸** (2026-09-15 · 화면에서 발견).
        #     날짜를 19개 목록에서만 고르게 한 뒤로 「30일 뒤」는 고를 보기가 없어
        #     모델이 날짜를 비웠다. 코드는 그걸 «날짜를 안 말했다» 로 읽어 **오늘 값**을
        #     줬고, «범위 밖입니다» 안내는 사라졌다. 기준일로부터 며칠인지를 정수로 받는다.
        "far_offsets": {"type": "array", "items": {"type": "integer"}},
        #   ★ **짝지어진 물음** (2026-09-15). 「5일 뒤 배추 경락가와 7일 뒤 무 도매가」를
        #     items x kinds 로 곱으면 **안 물어본 조합**(배추 중도매가 · 무 경락가)이
        #     나가고 날짜도 뒤섞인다. 실제로 그렇게 나갔다.
        "asks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string", "enum": ["배추", "무", "양파"]},
                    "kind": {"type": "string", "enum": ["AUC", "WHSL", "RTL"]},
                    "dates": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["item", "kind"],
            },
        },
    },
    #   🔴 **칸을 전부 꼭 쓰게 한다** (2026-09-15 · 화면에서 발견).
    #     `route` 하나만 필수였을 때, `asks` 칸을 더한 뒤로 모델이 `items`·`kinds`
    #     까지만 쓰고 **`dates`·`asks` 를 통째로 빼먹었다.** 「5일뒤」·「전체」가 전부
    #     «날짜를 말씀하지 않으셨다» 로 떨어졌다. 비어도 되지만 칸은 반드시 쓴다.
    "required": ["route", "items", "kinds", "dates", "far_offsets", "asks"],
}


#: 전달표의 창. **늘 같다** — 2026년 1,557개 조합 전부 18칸 온전(2026-09-15 실측).
#: 그래서 품목·가격종류를 고르기 전에도 «고를 수 있는 날» 목록을 만들 수 있다.
_WINDOW_DAYS = 18


def _date_enum_on() -> bool:
    """날짜를 **목록에서 고르게** 할까. **기본이 켬이다** (2026-09-15 채점으로 정했다).

    ```text
    자유 판(free)   119/128  93.0%
    목록 판(enum)   125/128  97.7%      +4.7%p
    ```

    갈린 자리는 범위 질문이었다 — 「모든 날」·「5일 뒤까지」를 자유 판은 빈 값이나
    마지막 하루로 답했고, 목록 판은 다 맞혔다. `ML_LLM_DATE_ENUM=0` 으로 되돌릴 수 있다.
    """
    return os.getenv(f"{_ENV_PREFIX}LLM_DATE_ENUM", "1").strip() in {"1", "true", "True"}


def _selectable(base_dt: date) -> list[str]:
    """고를 수 있는 날. **오늘(기준일) + 1~18** = 19개.

    ★ **오늘을 빼면 안 된다** (2026-09-15 실측으로 배웠다). 전달표는 D+1~D+18 이지만
      우리는 오늘 값을 원본 창고(`prediction_log` 리드 0)에서 읽어 답한다. 목록에서
      빼 두었더니 「오늘 양파 중도매가는?」에 **내일을 골랐다** — 없는 보기를 주면
      모델은 답을 비우는 대신 **가장 가까운 것을 고른다.**
    """
    return [
        (base_dt + timedelta(days=n)).isoformat() for n in range(_WINDOW_DAYS + 1)
    ]


def _schema(base_dt: date) -> dict[str, Any]:
    """응답 스키마. enum 판이면 **날짜를 만들 수 없고 고르기만** 한다.

    ★ 지금 판은 날짜를 자유 문자열로 받는다. 그래서 틀릴 자리가 셋이다 —
      형식(「내일」이 그대로 옴) · 범위(19일 뒤) · 연도(2025). enum 으로 묶으면
      **애초에 만들 수가 없어** 셋이 통째로 사라진다.

    🔴 **그래도 `gate` 는 그대로 둔다.** 창 안이어도 그 행이 있는지는 표만 안다.
      enum 은 «있을 법한 날» 까지만 보장한다.
    """
    if not _date_enum_on():
        return _RESPONSE_SCHEMA
    schema = {k: v for k, v in _RESPONSE_SCHEMA.items()}
    props = {k: v for k, v in _RESPONSE_SCHEMA["properties"].items()}
    days = _selectable(base_dt)
    props["dates"] = {"type": "array", "items": {"type": "string", "enum": days}}
    #   asks 안의 날짜도 같은 목록으로 묶는다 — 한쪽만 묶으면 그쪽으로만 안 틀린다.
    ask_item = {k: v for k, v in props["asks"]["items"].items()}
    ask_props = {k: v for k, v in ask_item["properties"].items()}
    ask_props["dates"] = {"type": "array", "items": {"type": "string", "enum": days}}
    ask_item["properties"] = ask_props
    props["asks"] = {"type": "array", "items": ask_item}
    schema["properties"] = props
    return schema


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
    #   ★ 스위치를 **부르기 직전에** 본다. 예전에는 `enabled()` 를 만들어 놓고
    #     아무도 안 불러서, `ML_LLM_ENABLED=0` 을 넣어도 그대로 호출했다.
    #     끄는 스위치가 안 끄면 없느니만 못하다 — 껐다고 믿고 할당량을 쓴다.
    if not enabled():
        return None
    key = _api_key()
    if not key:
        return None
    payload = {
        "system_instruction": {"parts": [{"text": _prompt(base_dt)}]},
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
            "responseSchema": _schema(base_dt),
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
    items = _pick_all(chosen.get("items"), QA_ITEMS)
    kinds = _trim_kinds(_pick_all(chosen.get("kinds"), QA_KINDS), question)
    asks = _pick_asks(chosen.get("asks"))
    if asks and not items:
        items = _pick_all([a["item"] for a in asks], QA_ITEMS)
    if asks and not kinds:
        kinds = _pick_all([a["kind"] for a in asks], QA_KINDS)
    return {
        "asks": asks,
        "route": str(chosen.get("route") or "forecast"),
        "items": items,
        "kinds": kinds,
        #   예전 이름 — 하나만 쓰는 자리가 아직 있다. 첫 값을 가리킨다.
        "item": items[0] if items else None,
        "kind": kinds[0] if kinds else None,
        #   목록 밖 날(far_offsets)을 날짜로 바꿔 합친다. 범위 검사는 gate 가 한다 —
        #   여기서 거르면 «범위 밖» 이라는 사실이 다시 사라진다.
        "dates": _parse_dates(chosen.get("dates")) + _far_dates(chosen.get("far_offsets"), base_dt),
    }


def _far_dates(raw: Any, today: date) -> list[date]:
    """목록 밖 날을 날짜로. 정수가 아닌 것은 버린다 — 고쳐 쓰지 않는다."""
    out: list[date] = []
    for value in raw if isinstance(raw, list) else []:
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        out.append(today + timedelta(days=value))
    return out


#: 가격 종류를 부르는 말. **질문에 이 낱말이 있어야 그 종류를 남긴다.**
_KIND_WORDS: dict[str, tuple[str, ...]] = {
    "AUC": ("경락", "경매", "낙찰", "매입"),
    "WHSL": ("중도매", "도매"),
    "RTL": ("소매", "마트", "소비자"),
}

#: 「가격 전부」처럼 **정말 다 달라는** 말. 이때는 안 자른다.
_ALL_WORDS = ("전부", "모두", "다 ", "모든", "전체")


def _trim_kinds(kinds: list[str], question: str) -> list[str]:
    """해석기가 넉넉히 고른 가격 종류를 **질문에 나온 것만** 남긴다.

    🔴 **지시문으로 두 번 실패한 자리다** (2026-09-15). 「배추 경락가」 하나를
      물어도 `AUC·WHSL·RTL` 셋을 내놓고, 같은 질문에 세 번 물으면 셋·셋·하나로
      흔들렸다. 낱말 풀이를 넣고 「나온 것만」이라고 적어도 그대로였다.

    **말로 부탁해서 안 되는 것은 규칙이 자른다** — 우리 원칙 그대로다.

    ★ 자르고 나서 **빈손이 되면 자르지 않는다.** 우리가 모르는 표현으로 물었을
      수 있고, 그때는 해석기 쪽이 옳다. 규칙이 답을 없애면 안 된다.
    """
    if len(kinds) <= 1 or any(word in question for word in _ALL_WORDS):
        return kinds
    named = [k for k in kinds if any(w in question for w in _KIND_WORDS.get(k, ()))]
    return named or kinds


def _pick_asks(raw: Any) -> list[dict[str, Any]]:
    """짝지어진 물음만 걸러 낸다. 품목·가격이 우리 것이 아니면 **그 칸을 버린다.**

    🔴 한 칸이 엉터리라고 나머지를 버리지 않는다 — 답할 수 있는 물음까지 사라진다.
    """
    out: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        item = str(entry.get("item") or "").strip()
        kind = str(entry.get("kind") or "").strip()
        if item not in QA_ITEMS or kind not in QA_KINDS:
            continue
        out.append({"item": item, "kind": kind, "dates": _parse_dates(entry.get("dates"))})
    return out


def _pick_all(raw: Any, allowed: tuple[str, ...]) -> list[str]:
    """목록에서 **우리가 아는 값만** 순서대로. 중복은 버린다.

    🔴 enum 을 걸어도 한 번 더 거른다 — 스키마는 «형태» 를 보장할 뿐이고,
      우리가 답할 수 있는 값인지는 우리 목록이 정한다.
    """
    out: list[str] = []
    for value in raw if isinstance(raw, list) else []:
        text = str(value).strip()
        if text in allowed and text not in out:
            out.append(text)
    return out
