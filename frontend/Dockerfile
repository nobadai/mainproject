# 1단계: Next.js 정적 파일 빌드
FROM node:18-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build  # next.config.js의 export 설정 덕분에 'out' 폴더가 생성됨

# 2단계: Nginx만 단독 실행
FROM nginx:alpine
# 빌드된 out 폴더의 내용물을 Nginx의 웹 루트 디렉토리로 복사
COPY --from=builder /app/out /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]



# FROM node:24-alpine AS dependencies

# WORKDIR /app

# COPY package.json package-lock.json ./
# RUN npm ci

# FROM node:24-alpine AS builder

# WORKDIR /app

# ENV NEXT_TELEMETRY_DISABLED=1

# COPY --from=dependencies /app/node_modules ./node_modules
# COPY . .
# RUN npm run build

# FROM node:24-alpine AS runner

# ENV NODE_ENV=production \
#     NEXT_TELEMETRY_DISABLED=1 \
#     HOSTNAME=0.0.0.0 \
#     PORT=3000

# WORKDIR /app

# RUN chown node:node /app

# COPY --from=builder --chown=node:node /app/public ./public
# COPY --from=builder --chown=node:node /app/.next/standalone ./
# COPY --from=builder --chown=node:node /app/.next/static ./.next/static

# USER node

# EXPOSE 3000

# HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
#     CMD wget --no-verbose --tries=1 --spider http://127.0.0.1:3000/ || exit 1

# CMD ["node", "server.js"]
