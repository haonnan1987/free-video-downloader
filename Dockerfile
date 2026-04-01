FROM python:3.12-slim

# 使用阿里云镜像加速 apt 下载
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null; \
    apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# 从淘宝 Node.js 镜像下载
RUN curl -fsSL --retry 3 --retry-delay 5 \
      "https://npmmirror.com/mirrors/node/v20.18.1/node-v20.18.1-linux-x64.tar.xz" \
      -o /tmp/node.tar.xz && \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 && \
    rm /tmp/node.tar.xz && \
    node --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt && \
    pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple bgutil-ytdlp-pot-provider && \
    python -m playwright install --with-deps chromium

COPY app/ app/
COPY static/ static/

ENV PYTHONUNBUFFERED=1
ENV COBALT_API_URL=http://cobalt:9000
ENV DOWNLOAD_DIR=/app/data/downloads

EXPOSE 8000

CMD ["uvicorn", "app.main:fastapi_app", "--host", "0.0.0.0", "--port", "8000"]
