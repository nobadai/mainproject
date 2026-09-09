import type { NextConfig } from "next";

/**
 * 배포는 정적 내보내기(out/) → nginx 다. `output: "export"` 를 유지해야 한다.
 *
 * 다만 정적 내보내기에는 서버가 없어 rewrites 를 쓸 수 없다. 기능 테스트 페이지는
 * `npm run dev` 로만 쓰므로, **개발 모드에서만** 프록시를 켜서 CORS 를 우회한다.
 * 이렇게 하면 배포 빌드 산출물은 종전과 완전히 동일하다.
 */
const isDev = process.env.NODE_ENV === "development";
const backendOrigin = process.env.BACKEND_ORIGIN ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = isDev
  ? {
      async rewrites() {
        return [
          // 화면용 API. 백엔드에서도 `/api` 로 시작하는데, 아래 규칙이 앞의
          // `/api` 를 떼어 버리므로 **여기서 다시 붙여** 준다. 순서가 중요하다 —
          // 먼저 걸리는 규칙이 이긴다.
          { source: "/api/screen/:path*", destination: `${backendOrigin}/api/:path*` },
          //  ML 운영 콘솔 — 이 서버가 다시 ML 백엔드로 넘긴다
          //  (`app/ml/console_proxy.py`). 브라우저가 직접 부르면 출처가 달라
          //  CORS 를 만나고 주소가 화면 코드에 박힌다.
          { source: "/api/ml/:path*", destination: `${backendOrigin}/ml/console/:path*` },
          // 에이전트 API. 백엔드는 `/master`·`/finance` 처럼 `/api` 없이 받는다.
          { source: "/api/:path*", destination: `${backendOrigin}/:path*` },
        ];
      },
    }
  : { output: "export" };

export default nextConfig;
