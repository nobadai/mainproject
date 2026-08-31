import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans_KR } from "next/font/google";
import type { ReactNode } from "react";

import "./globals.css";

/**
 * 한글이 본문이라 한글 지원 서체를 쓰고, 수치는 모노스페이스로 줄을 맞춘다.
 *
 * ★ `next/font` 로 받는다 — `<link>` 로 붙이면 페이지마다 다시 받고 레이아웃이 튄다.
 *   정적 내보내기(`output: "export"`)에서도 번들에 들어가므로 배포가 같다.
 */
const sans = IBM_Plex_Sans_KR({
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700"],
  variable: "--font-plex-sans",
  display: "swap",
});

const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "햇들농산 운영 콘솔",
  description: "마스터 에이전트에게 말로 묻고 매입안을 승인하는 화면",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="ko" className={`${sans.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
