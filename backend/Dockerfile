FROM golang:1.27.2-alpine AS builder

WORKDIR /app

# 使用 vendor 模式构建，避免容器内无法访问外网（环境约束）
COPY go.mod go.sum ./
COPY vendor ./vendor
COPY . .

# 构建期注入版本信息（清 S3 挂账）；可用 --build-arg 覆盖
ARG APP_VERSION=dev
ARG GIT_COMMIT=unknown

RUN unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy && \
    BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)" && \
    CGO_ENABLED=0 GOOS=linux go build -mod=vendor \
      -ldflags "-s -w \
        -X opspilot/backend/internal/buildinfo.Version=${APP_VERSION} \
        -X opspilot/backend/internal/buildinfo.Commit=${GIT_COMMIT} \
        -X opspilot/backend/internal/buildinfo.BuildTime=${BUILD_TIME}" \
      -o /opspilot-backend ./cmd/api

FROM golang:1.27.2-alpine

WORKDIR /app

# release 模式（gin 不输出调试日志）
ENV GIN_MODE=release

COPY --from=builder /opspilot-backend /app/opspilot-backend
COPY migrations /app/migrations

EXPOSE 8081
ENTRYPOINT ["/app/opspilot-backend"]