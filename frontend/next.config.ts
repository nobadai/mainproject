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
      /**
       * 개발 모드에 Next 가 띄우는 **동그란 `N` 배지**를 끈다.
       *
       * 화면 구석에 떠서 마우스로 끌어 옮길 수 있는 그것이고, 우리 코드가 그리는 것이
       * 아니라 `next dev` 가 넣는다. 사이드바 브랜드 배지(`햇`)·사용자 이니셜 동그라미와
       * 생김새가 겹쳐 **콘솔이 띄운 것으로 오해하게 만든다.**
       *
       * ⚠️ 끄는 것은 표시기뿐이다 — 컴파일·런타임 오류는 그대로 화면에 뜬다
       *    (`node_modules/next/dist/docs/.../devIndicators.md`).
       *
       * ★ 배포 빌드(`output: "export"`)에는 원래 안 들어가므로 산출물은 그대로다.
       */
      devIndicators: false,
      experimental: {
        /**
         * 🔴 프록시 상한. **`lib/api.ts` 의 `EXECUTE_TIMEOUT_MS` 와 같은 값이다.**
         *
         * Next 의 기본값은 `30초`(`next/dist/server/lib/router-utils/proxy-request.js`
         * 의 `proxyTimeout || 30000`) 인데, 마스터 승인 한 번이 그보다 오래 걸린다.
         *
         * 2026-09-16 실측 (실 서버 · 화면에서 직접) —
         *
         *     프록시 없이 직접   POST /master/ask/execute   200 · **31.4초** · 승인 정상 기록
         *     화면(프록시 경유)  POST /api/master/ask/execute
         *                       🔴 500 ·「요청을 처리하지 못했습니다」
         *                       Next: Failed to proxy ... socket hang up { code: 'ECONNRESET' }
         *                       uvicorn 접근 로그에 **그 요청이 아예 없다** (먼저 끊어서)
         *
         * 결과가 나빴던 이유는 실패해서가 아니다 — **DB 에는 승인이 남고 화면만 실패로 보여서**
         * 실매입 기록 카드가 안 떴다. 09-11 시연이 이것 때문에 두 번 막혔다.
         *
         * ⚠️ 화면이 준 시간을 프록시가 30초로 잘라먹고 있었다. 두 값이 갈리면 어느 쪽이 끊었는지
         *   못 가리므로 **`EXECUTE_TIMEOUT_MS` 와 같은 값을 유지한다** — 한쪽을 고치면 다른 쪽도.
         */
        proxyTimeout: 900_000,
      },
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
          // 운영 콘솔 read API. 백엔드 route 자체가 `/api/console/...` 이므로
          // generic `/api` 제거 규칙보다 먼저 원래 prefix를 보존한다.
          { source: "/api/console/:path*", destination: `${backendOrigin}/api/console/:path*` },
          // 에이전트 API. 백엔드는 `/master`·`/finance` 처럼 `/api` 없이 받는다.
          { source: "/api/:path*", destination: `${backendOrigin}/:path*` },
        ];
      },
    }
  : { output: "export" };

export default nextConfig;
