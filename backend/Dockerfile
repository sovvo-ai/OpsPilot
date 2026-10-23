FROM golang:1.27.2-alpine AS builder

WORKDIR /app

# 使用 vendor 模式构建，避免容器内无法访问外网（环境约束）
COPY go.mod go.sum ./
COPY vendor ./vendor
COPY . .
RUN unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy && \
    CGO_ENABLED=0 GOOS=linux go build -mod=vendor -o /opspilot-backend ./cmd/api

FROM golang:1.27.2-alpine

WORKDIR /app

COPY --from=builder /opspilot-backend /app/opspilot-backend
COPY migrations /app/migrations

EXPOSE 8081
ENTRYPOINT ["/app/opspilot-backend"]
