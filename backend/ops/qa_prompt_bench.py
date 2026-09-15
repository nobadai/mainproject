"""지시문 언어 채점 — 한국어 프롬프트 vs 영어 프롬프트 (2026-09-15).

## 무엇을 재나

**정답을 아는 질문 12개**를 주고, 해석기가 네 칸(route·item·kind·dates)을
제대로 골랐는지 센다. 바꾸는 것은 **지시문 언어 하나뿐**이다 —
모델·온도·응답 스키마·질문·기준일은 그대로 둔다.

## 왜 한 번만 돌리면 안 되나

한 문제 한 번의 차이는 그날 운일 수 있다. 그래서 `--rounds` 회 반복하고
**회차마다 같은 답이 나오는지(흔들림)도** 같이 잰다. 맞히는 것만큼
**매번 같게 맞히는 것**이 중요하다 — 같은 질문에 다른 답이 나오면
사람이 믿을 수가 없다.

## ★ 분당 한도가 있다 — 2026-09-15 에 여기서 한 번 틀렸다

무료 등급 `gemini-3.5-flash-lite` 는 **분당 15회**다. 처음에 48회를 쉬지 않고
쏘면서 «한국어 전부 → 영어 전부» 순서로 돌렸더니, 한국어가 한도를 다 쓰고
영어는 24회 모두 429 로 막혔다. 채점표에는 그것이 **«영어 4.2%»** 로 찍혔다.
**호출 실패와 오답이 같은 칸에 섞이면 실험이 거짓말을 한다.**

그래서 셋을 고쳤다.

    간격      기본 5초 (분당 12회). `--delay` 로 조절
    번갈아    한 문제마다 한국어·영어를 **붙여서** 부른다
              -> 한도나 서버 상태가 흔들려도 양쪽이 똑같이 걸린다
    못 부름   오답과 **따로 센다.** 하나라도 있으면 채점을 믿지 말 것

## 주의

**진짜 호출이 나간다.** 12문제 x 2언어 x rounds 회 (기본 2회차 = 48회),
간격 5초면 약 4분 걸린다.

    python ops/qa_prompt_bench.py --rounds 2
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ml import qa_llm

BASE = date(2026, 9, 14)

#: (질문, 기대 route, 기대 item, 기대 kind, 기대 날짜의 기준일 대비 차이)
#: ★ 기대값은 «우리 규칙이 맞다고 보는 것» 이다. None 은 «비어 있어야 한다».
CASES: list[tuple[str, str, str | None, str | None, list[int] | None]] = [
    ("내일 배추 경락가 얼마야?", "forecast", "배추", "AUC", [1]),
    ("모레 무 소매가 알려줘", "forecast", "무", "RTL", [2]),
    ("오늘 양파 중도매가는?", "forecast", "양파", "WHSL", [0]),
    ("10일 뒤 배추 경락가", "forecast", "배추", "AUC", [10]),
    ("내일, 내일 모래 배추 경락가 얼마야?", "forecast", "배추", "AUC", [1, 2]),
    ("오늘 내일 10일 뒤 무 경락가 알려줘", "forecast", "무", "AUC", [0, 1, 10]),
    ("배추 가격 알려줘", "forecast", "배추", None, None),
    ("대파 내일 얼마야?", "out_of_scope", None, None, None),
    ("마늘 경락가 알려줘", "out_of_scope", None, None, None),
    ("배추 경락가 예측 얼마나 맞아?", "accuracy", "배추", "AUC", None),
    ("양파 중도매가 예측 믿고 사도 돼?", "usability", "양파", "WHSL", None),
    ("5일 뒤에는 얼마나 바뀌어?", "forecast", None, None, [5]),
    #   ★ 범위 — 여기가 이번 실험의 과녁이다. 코드가 세 주지 않고 **LLM 이 고른다.**
    ("모든날의 배추 경락가에 대한 예측값을 알려줘", "forecast", "배추", "AUC",
     list(range(1, 19))),
    ("배추 경락가 전부 다 보여줘", "forecast", "배추", "AUC", list(range(1, 19))),
    ("무 소매가 5일 뒤까지 알려줘", "forecast", "무", "RTL", [1, 2, 3, 4, 5]),
    ("양파 경락가 일주일치", "forecast", "양파", "AUC", [1, 2, 3, 4, 5, 6, 7]),
]


def _expected_dates(offsets: list[int] | None) -> set[date] | None:
    return None if offsets is None else {BASE + timedelta(days=d) for d in offsets}


def score(got: dict | None, case) -> tuple[int, int, list[str]]:
    """맞은 칸 수 · 전체 칸 수 · 틀린 칸 이름. 못 부르면 0점이다."""
    _, route, item, kind, offsets = case
    if got is None:
        return 0, 4, ["호출실패"]
    wrong = []
    if got.get("route") != route:
        wrong.append("route={}".format(got.get("route")))
    if got.get("item") != item:
        wrong.append("item={}".format(got.get("item")))
    if got.get("kind") != kind:
        wrong.append("kind={}".format(got.get("kind")))
    want = _expected_dates(offsets)
    have = set(got.get("dates") or [])
    if want is None:
        if have:
            wrong.append(f"dates={sorted(str(d) for d in have)}")
    elif have != want:
        wrong.append(f"dates={sorted(str(d) for d in have)}")
    return 4 - len(wrong), 4, wrong


#: 비교하는 두 판. **바뀌는 것은 날짜를 어떻게 받느냐 하나뿐이다.**
#: 지시문 언어는 한국어로 고정한다 (2026-09-15 실험에서 한국어가 같거나 나았다).
VARIANTS: dict[str, dict[str, str]] = {
    "free": {"ML_LLM_DATE_ENUM": "0"},    # 지금 판 — 날짜를 자유 문자열로 만든다
    "enum": {"ML_LLM_DATE_ENUM": "1"},    # 고를 수 있는 18개를 주고 고르게 한다
}


def ask(lang: str, question: str, delay: float, retries: int = 2):
    """한 번 묻는다. 못 부르면 쉬었다 다시 — 분당 한도는 기다리면 풀린다."""
    os.environ["ML_LLM_PROMPT_LANG"] = "ko"
    for key, value in VARIANTS[lang].items():
        os.environ[key] = value
    for attempt in range(retries + 1):
        got = qa_llm.interpret(question, BASE)
        if got is not None:
            return got
        if attempt < retries:
            time.sleep(20.0 + delay)
    return None


def run(rounds: int, delay: float, verbose: bool) -> list[dict]:
    """두 언어를 **번갈아** 부른다. 몰아서 부르면 뒤엣것만 한도에 걸린다."""
    state = {
        lang: {"hit": 0, "total": 0, "seconds": 0.0, "fail": 0, "answers": {}}
        for lang in ("free", "enum")
    }
    for _ in range(rounds):
        for i, case in enumerate(CASES):
            for lang in ("free", "enum"):
                started = time.time()
                got = ask(lang, case[0], delay)
                st = state[lang]
                st["seconds"] += time.time() - started
                good, all_, wrong = score(got, case)
                st["hit"] += good
                st["total"] += all_
                if got is None:
                    st["fail"] += 1
                st["answers"].setdefault(i, set()).add(repr(got))
                if verbose and wrong:
                    print(f"  [{lang}] {case[0][:22]:24s} {' · '.join(wrong)}")
                time.sleep(delay)
    out = []
    for lang in ("free", "enum"):
        st = state[lang]
        shaky = [i for i, seen in st["answers"].items() if len(seen) > 1]
        out.append({"lang": lang, "hit": st["hit"], "total": st["total"],
                    "seconds": st["seconds"], "fail": st["fail"], "shaky": shaky})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--delay", type=float, default=5.0,
                    help="호출 간격(초). 무료 등급은 분당 15회다")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not qa_llm.enabled():
        print("LLM 이 꺼져 있습니다 (ML_LLM_ENABLED / 키 확인)")
        return 1

    calls = len(CASES) * args.rounds * 2
    print(f"질문 {len(CASES)}개 · {args.rounds}회차 · 칸 4개씩 채점")
    print(f"호출 {calls}회 · 간격 {args.delay:.1f}초 "
          f"→ 약 {calls * args.delay / 60:.1f}분")
    print()
    out = run(args.rounds, args.delay, not args.quiet)

    print()
    print(f"{'날짜판':6s} {'맞은 칸':>10s} {'정확도':>8s} "
          f"{'못 부름':>8s} {'흔들림':>8s} {'초':>8s}")
    #   ★ 총점만 보면 안 된다. 2026-09-15 실측에서 두 판의 총점은 0.8%p 차이였는데
    #     **틀리는 자리가 통째로 갈렸다** — free 는 범위 질문에서 전멸했고 enum 은
    #     「오늘」에서 전멸했다. 평균이 그 둘을 서로 가려 준 것이다.
    for r in out:
        pct = 100.0 * r["hit"] / r["total"]
        print(f"{r['lang']:6s} {r['hit']:>6d}/{r['total']:<4d} {pct:>7.1f}% "
              f"{r['fail']:>6d}회 {len(r['shaky']):>6d}개 {r['seconds']:>7.1f}")

    if any(r["fail"] for r in out):
        print()
        print("★ 못 부른 것이 있습니다. 호출 실패와 오답이 섞이면 채점이 거짓말을 합니다 —")
        print("  --delay 를 늘려 다시 재세요.")
        return 2
    gap = (out[1]["hit"] - out[0]["hit"]) / out[0]["total"] * 100
    print()
    print(f"enum − free = {gap:+.1f}%p")
    print("★ 한두 칸 차이는 그날 운입니다. 방향을 말하려면 회차를 늘리세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
