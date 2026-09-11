"""ML 가격 예측 API Router.

★ 2026-09-11 — 두 주소를 **주석 처리**했습니다 (지우지 않음).

    GET  /ml/forecast        코드에서 부르는 곳이 없습니다
    POST /ml/forecast/push   부르는 곳이 없습니다. 매입 DB 적재는 ML 배치가
                             `연동/push_forecast.py` 로 직접 합니다 — 같은 일을
                             하는 코드가 두 벌이면 날짜 변환 규칙이 갈라집니다

  마스터는 `ml_price_forecasts` 를 DB 에서 직접 읽고, 판매는 `app.ml.schemas.Forecast`
  모양만 씁니다. **`schemas.py` · `service.py` 는 그대로 둡니다.**
  되살리려면 아래 `# ` 를 지우면 됩니다.
"""

from fastapi import APIRouter

# from datetime import date
# from typing import Annotated
#
# from fastapi import HTTPException, Query, status
#
# from app.ml.schemas import ITEMS, Forecast, TargetKind
# from app.ml.service import get_forecast, push_forecasts

router = APIRouter(prefix="/ml", tags=["ml"])
#
#
# @router.get(
#     "/forecast",
#     response_model=Forecast,
#     summary="가격 예측 조회",
#     description=(
#         "품목 하나의 D+1~D+18 예측을 돌려준다. 날짜는 **연속 달력일**이다.\n\n"
#         "`as_of` 이하의 가장 최근 기준일 예측을 준다 — 그날 예측이 없으면 "
#         "전날 것을 준다. 미래 정보는 쓰지 않는다.\n\n"
#         "`use_recommended` 가 false 면 그 조합은 우리 모델보다 "
#         "'어제 가격 그대로' 가 낫다는 뜻이므로 판단에 쓰지 말 것.\n\n"
#         "`filled_count` 는 토·일·공휴일처럼 경매가 없어 직전 값으로 채운 "
#         "칸 수다. 보통 18일 중 5~6일이다."
#     ),
# )
# def read_forecast(
#     item: Annotated[str, Query(description="품목명", examples=["배추"])],
#     as_of: Annotated[date, Query(description="기준일. 이 날짜 이하의 최신 예측")],
#     target_kind: Annotated[
#         TargetKind, Query(description="AUC 경락가 · WHSL 중도매가 · RTL 소매가")
#     ] = "AUC",
# ) -> Forecast:
#     if item not in ITEMS:
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail=f"지원하지 않는 품목입니다: {item}. 가능: {', '.join(ITEMS)}",
#         )
#     try:
#         return get_forecast(item, as_of, target_kind)
#     except LookupError as error:
#         raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
#     except RuntimeError as error:
#         # DB 환경변수 누락·연결 실패. **404 로 내보내면 안 된다** — "그날 예측이
#         # 없다"와 "창고에 못 붙었다"는 부르는 쪽이 해야 할 일이 다르다.
#         # (`push` 는 이미 이렇게 하고 있었는데 여기만 빠져 있었다.)
#         raise HTTPException(
#             status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
#         ) from error
#
#
# @router.post(
#     "/forecast/push",
#     status_code=status.HTTP_202_ACCEPTED,
#     summary="예측 적재 (배치용)",
#     description=(
#         "원본 창고의 예측을 서비스 창고로 옮긴다. 배치가 하루 한 번 부른다.\n\n"
#         "여기서 **개장일 축을 달력일 축으로 바꾼다.** 같은 기준일을 다시 "
#         "불러도 덮어쓰므로 여러 번 호출해도 안전하다."
#     ),
# )
# def push(
#     base_dt: Annotated[date | None, Query(description="기준일. 생략하면 최신")] = None,
# ) -> dict:
#     try:
#         return push_forecasts(base_dt)
#     except RuntimeError as error:
#         raise HTTPException(
#             status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
#         ) from error
