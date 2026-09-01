"""Finance Agent 실행 계층.

`controller` 가 수명주기를 잡고, `planner_loop` 가 Tool 선택 루프를 돌리며,
`guards` 가 계약·상한을 지키고, `finalization` 이 업무 결과를 확정한다.
업무 공식과 판정은 여기 없다 — `domain`(tools/rules)과 `capabilities` 소유다.
"""
