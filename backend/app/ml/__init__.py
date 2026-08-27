"""ML 파트 — 농산물 가격 예측.

경락가·중도매가·소매가를 품목별로 1~18일 앞까지 예측해 매입 판단에 넘긴다.

    repository.py   원본 창고에서 예측을 읽고 서비스 창고에 적재
    service.py      영업일 -> 달력일 변환, 계약 형태로 조립
    router.py       조회 API
    schemas.py      계약 (purchase_agent.ports.get_forecast 와 1:1)
    db.py           창고 두 곳 연결
"""
