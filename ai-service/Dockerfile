FROM python:3.11-slim

WORKDIR /app

# 避免写入 .pyc，并确保输出实时刷新
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 安装依赖：使用本地 vendor，避免容器构建阶段访问外网
COPY requirements.txt ./
COPY vendor ./vendor
RUN unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy && \
    pip install --no-cache-dir --no-index --find-links vendor -r requirements.txt

COPY app /app/app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
