import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypeScript from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTypeScript,
  // 개발 서버가 작업공간 루트에서 시작된 경우에도 생성물이 린트 대상이 되지 않게 한다.
  globalIgnores([".next/**", "frontend/.next/**", "out/**", "build/**", "next-env.d.ts"]),
]);
