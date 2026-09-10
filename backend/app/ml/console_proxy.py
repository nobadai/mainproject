"""ML 운영 콘솔 프록시 — 우리 ML 백엔드로 넘긴다.

★ **왜 프록시인가.**

  재학습·에이전트·설명은 **ML 저장소가 있는 곳에서만** 돌 수 있습니다.
  학습 꾸러미(`ML/.../ml_train_kit_2/`), 모델 번들 208MB, 에이전트 기록 파일이
  거기 있습니다. 이 저장소로 옮길 수 있는 것이 아닙니다.

  그렇다고 화면이 브라우저에서 직접 그쪽을 부르면 출처가 달라져 CORS 를
  만나고, 주소가 화면 코드에 박힙니다. 그래서 **서버가 대신 물어봅니다.**

      브라우저 ──/ml/console/…──▶ 이 서버 ──▶ ML 백엔드(기본 8102)

★ **안 되면 조용히 비우지 않고 말합니다.** ML 백엔드가 안 떠 있으면
  502 와 함께 «무엇이 안 됐는지» 를 그대로 올립니다. 화면이 빈 채로
  «자료가 없다» 로 보이면 안 됩니다.

★ **넘길 경로를 목록으로 못박습니다.** 아무 경로나 넘기면 이 서버가
  열린 문이 됩니다. 쓰기(POST)는 재학습 셋뿐입니다.

설정: `backend/.env` 의 `ML_CONSOLE_ORIGIN` (없으면 `http://127.0.0.1:8102`)
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/ml/console", tags=["ml:console"])

#: `.env` 자리. `app/ml/db.py` 와 같은 규칙이다 (backend/.env).
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

#: 같은 컴퓨터에서 돌 때의 기본값.
_DEFAULT_ORIGIN = "http://127.0.0.1:8102"


def _origin() -> tuple[str, str]:
    """ML 백엔드 주소와, 그 주소를 어디서 가져왔는지.

    ★ **`.env` 를 여기서 직접 읽는다** (2026-09-10 고침).

      이 저장소는 `.env` 를 **모듈마다 따로** 읽는다 (`app/ml/db.py` ·
      `app/finance/db.py` 가 각자 `load_dotenv` 를 부른다). 그런데 여기서는
      안 불렀고, `os.getenv` 를 **모듈을 불러오는 시점에** 한 번만 봤다.
      그 시점에는 아무도 `.env` 를 안 읽었을 수 있다 — 부르는 순서에 달렸다.

      그래서 `.env` 에 `ML_CONSOLE_ORIGIN` 을 바르게 적어도 **읽히지 않고**
      기본값 `127.0.0.1` 로 떨어졌다. ML 서버가 다른 컴퓨터에 있으면
      자기 안에서 찾다가 502 가 난다.

      ★ 같은 컴퓨터에서 돌릴 때는 기본값이 우연히 맞아서 **아무 증상이
        없었다.** 다른 컴퓨터를 붙이는 날 처음 드러났다.

      요청마다 읽는다. `load_dotenv` 는 이미 있는 환경변수를 덮지 않고,
      한 번 읽은 뒤로는 값이 환경에 남으므로 파일을 매번 훑지 않는다.
    """
    load_dotenv(_ENV_FILE, override=False)
    got = os.getenv("ML_CONSOLE_ORIGIN")
    if got:
        return got.rstrip("/"), "이 주소는 backend/.env 의 ML_CONSOLE_ORIGIN 에서 왔습니다."
    #   ★ 「서버가 떠 있나」만 물으면, 정작 서버는 떠 있는데 주소가 틀린
    #     경우에 엉뚱한 데를 뒤지게 된다. 실제로 그랬다 (2026-09-10).
    return _DEFAULT_ORIGIN, (
        "★ backend/.env 에 ML_CONSOLE_ORIGIN 이 없어 기본값을 썼습니다. "
        "ML 서버가 다른 컴퓨터에 있으면 그 주소를 적으세요 "
        "(예: ML_CONSOLE_ORIGIN=http://192.168.0.x:8102) — .env 는 깃에 안 올라가므로 "
        "컴퓨터마다 따로 적어야 합니다."
    )

#: 얼마나 기다리나. 재학습 후보 만들기는 몇 분 걸리므로 길게 둔다 —
#: 다만 그 호출은 **바로 돌아오고 진행은 따로 물어보는** 구조라
#: 실제로 오래 걸리는 요청은 없다.
TIMEOUT = httpx.Timeout(connect=3.0, read=30.0, write=10.0, pool=3.0)

#: 넘길 수 있는 것. **여기 없는 경로는 404 다.**
READ = frozenset({
    "meta",
    "forecast",
    "forecast/base-dates",
    "accuracy",
    "accuracy/leadtime",
    "quality",
    #   ★ 아침에 저장된 점검 결과. 다시 안 돌리고 그대로 읽습니다.
    "quality/saved",
    "quality-table",
    "delivery",
    "batch/recent",
    "agent/batch",
    "agent/news",
    "agent/history",
    "agent/report",
    "agent/explain",
    "retrain/status",
    #   ★ 사람이 눌러야 할 결정이 있나. 화면이 탭을 띄울지 정하는 데 씁니다.
    "retrain/pending",
    "retrain/job",
    "retrain/graph/status",
    #   ★ 다시 돌리기가 어디까지 갔나. 누른 뒤 진행을 묻는 자리입니다.
    "ops/job",
})

#: 쓰기. **재학습 셋뿐이다.** 사람이 눌러야 도는 것들이고,
#: 화면은 승인권자에게만 버튼을 보인다.
WRITE = frozenset({
    "retrain/build",
    "retrain/apply",
    "retrain/rollback",
    "retrain/graph/act",
    "retrain/graph/reset",
    #   ★ 아침에 실패한 것을 다시 돌립니다 (자동 작업 · AI 점검).
    #     같은 기준일을 다시 쓰는 것이라 UPSERT 로 덮입니다 — 없던 날이
    #     생기거나 있던 날이 사라지지 않습니다.
    "ops/rerun",
})


async def _forward(method: str, path: str, request: Request) -> JSONResponse:
    origin, hint = _origin()
    url = f"{origin}/{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.request(
                method, url,
                params=dict(request.query_params),
                content=await request.body() if method == "POST" else None,
                headers={"Content-Type": "application/json"} if method == "POST" else None,
            )
    except httpx.ConnectError as error:
        #   ★ 여기서 조용히 빈 값을 돌려주면 화면이 «자료가 없다» 로 보인다.
        #     ML 백엔드가 안 떠 있다는 사실 자체를 알려야 한다.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=(f"ML 백엔드에 닿지 못했습니다 ({origin}). {hint} "
                    f"주소가 맞다면 그쪽 서버가 떠 있는지, 방화벽이 그 포트를 "
                    f"막고 있지 않은지 보세요 — {error}"),
        ) from error
    except httpx.TimeoutException as error:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"ML 백엔드가 제때 답하지 않았습니다 ({origin}/{path}) — {error}",
        ) from error

    #   저쪽이 낸 상태와 본문을 **그대로** 올린다. 400 을 200 으로 바꾸면
    #   무엇을 잘못 넣었는지가 사라진다.
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text[:500]}
    return JSONResponse(status_code=response.status_code, content=body)


@router.get("/{path:path}", summary="ML 콘솔 조회 — ML 백엔드로 넘긴다")
async def proxy_get(path: str, request: Request) -> JSONResponse:
    if path not in READ:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"넘길 수 있는 경로가 아닙니다: {path}",
        )
    return await _forward("GET", path, request)


@router.post("/{path:path}", summary="ML 콘솔 실행 — 재학습만")
async def proxy_post(path: str, request: Request) -> JSONResponse:
    if path not in WRITE:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=(f"쓰기로 넘길 수 있는 경로가 아닙니다: {path}. "
                    f"가능: {', '.join(sorted(WRITE))}"),
        )
    return await _forward("POST", path, request)
