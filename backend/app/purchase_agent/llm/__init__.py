"""매입 에이전트의 LLM 경계 (백로그 E3-2 · 상세설계 §4-⑤ E3-2 확정 블록).

**노드는 이 패키지 안쪽을 모른다.** ⑤가 아는 표면은 ``mix.make_mix_selector()``가
돌려주는 콜러블 하나뿐이고, 설정·프로바이더·검증·재시도·fallback은 전부 여기서 끝난다.
critic의 ``llm/judge.py``, orchestrator의 ``llm/selector.py``와 같은 배치다.

⚠️ **팀 런타임의 5번째 복제다.** finance·logistics·orchestrator·critic이 각자
``llm/runtime.py``를 들고 있고(finance↔logistics는 47줄 차이), 공용 ``app/llm/`` 층으로
뽑는 것은 남의 코드 4개를 건드리는 일이라 **팀 안건**으로 남긴다. 여기서는 규약(환경변수
이름·status 5종·Provider 프로토콜·temperature 0·숫자 금지 검증)을 그대로 따르고
**다른 점을 만들지 않는다** — §4-⑤ E3-2 확정 블록의 마지막 줄이 그 뜻이다.
"""
