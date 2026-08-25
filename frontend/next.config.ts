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
        return [{ source: "/api/:path*", destination: `${backendOrigin}/:path*` }];
      },
    }
  : { output: "export" };

export default nextConfig;
