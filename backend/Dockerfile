FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/srv/.venv/bin:$PATH"

WORKDIR /srv

# 의존성을 먼저 복사해 레이어 캐시를 살립니다.
# 앱 코드만 바뀌면 이 레이어는 재사용됩니다.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

# root 로 실행할 이유가 없으므로 전용 계정으로 낮춥니다.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# 컨테이너 자체 헬스체크. 배포 스크립트의 폴링과는 별개로,
# 실행 중 앱이 죽으면 docker ps 에서 unhealthy 로 드러납니다.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
