"""배포 파이프라인 검증용 최소 앱.

/health 는 deploy.ps1 의 헬스체크가 호출하는 엔드포인트입니다.
실제 앱으로 교체하더라도 /health 는 남겨두세요.
"""

import os

from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/")
def index():
    return jsonify(message="mainproject is running", version=os.getenv("APP_VERSION", "dev"))


def main():
    from waitress import serve

    port = int(os.getenv("APP_PORT", "8000"))
    print(f"serving on 0.0.0.0:{port}", flush=True)
    serve(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
