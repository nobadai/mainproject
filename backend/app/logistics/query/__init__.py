"""물류 Read-only 조회 — 공용 Tool 과 질문형 STATUS_QUERY.

```text
tools.py         공용 Read-only 물류 조회 Tool (기존 함수의 wrapper · 쓰기 0)
status_query.py  질문형 STATUS_QUERY — LLM function/tool calling loop
llm.py           provider function calling 전송 (Ollama tool_calls · Gemini functionCall)
```

★ `tools.py` 는 Agent 전용이 아니라 **공용 조회 Tool** 이다 — 무엇이 묻든 같은 구현을
  지나야 회신·화면·조회가 같은 창고를 두고 다른 말을 하지 않는다.
"""
