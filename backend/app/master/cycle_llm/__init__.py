"""★ **`app/orchestrator/` 에서 옮겼다** (2026-09-07 · 지시). 옛 경로는 없다.

⚠️ **`cycle_` 는 옮겨 온 사이클 묶음을 한 이름 아래 모으려고 붙였다.**
  마스터의 Flow 골격과 섞이지 않게 한다.

Orchestrator-owned Local LLM layer (T3-5 selection).

★ **selector 구현(`runtime.py` · `selector.py`)은 2026-09-08 에 걷어냈다.**
  남은 것은 `schemas.py` 하나다 — `cycle_schemas.py` 가 `LLMResponseFields` 를 쓴다.
  seam 은 `cycle_graph.Selector` Protocol 에 남아 있고, 걷어낸 사정도 거기 적었다."""
