# 基础镜像：Python 3.12 / Debian bookworm
FROM python:3.12-slim-bookworm

# 使用阿里云 Debian 镜像加速（同时兼容 deb822 与旧 sources.list 格式）
RUN set -eux; \
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources; do \
        [ -f "$f" ] || continue; \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' "$f"; \
    done; \
    apt-get -o Acquire::http::Timeout=30 -o Acquire::ftp::Timeout=30 update; \
    apt-get -o Acquire::http::Timeout=30 -o Acquire::ftp::Timeout=30 install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        openssh-client \
        unzip \
        && rm -rf /var/lib/apt/lists/*; \
    # 安装 ossutil 到 /app/bin/
    mkdir -p /app/bin; \
    curl -L https://gosspublic.alicdn.com/ossutil/v2/2.1.2/ossutil-2.1.2-linux-amd64.zip -o /tmp/ossutil.zip; \
    unzip /tmp/ossutil.zip -d /tmp/; \
    mv /tmp/ossutil-*-linux-amd64/ossutil /app/bin/ossutil; \
    chmod +x /app/bin/ossutil; \
    rm -rf /tmp/ossutil*; \
    # 清理仅构建期需要的工具
    apt-get purge -y unzip curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

# 工作目录
WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制全部代码（依赖 .dockerignore 过滤）
COPY . /app/

# 确保脚本可执行
RUN chmod +x /app/bin/*.sh /app/entrypoint.sh

# 暴露端口
EXPOSE 8000

# 环境变量
ENV FLASK_APP=app.py \
    PYTHONUNBUFFERED=1 \
    GUNICORN_WORKERS=2 \
    GUNICORN_THREADS=4

# 启动
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["sh", "-c", "exec gunicorn --workers ${GUNICORN_WORKERS:-2} --threads ${GUNICORN_THREADS:-4} --worker-class gthread --bind 0.0.0.0:8000 --timeout 0 --graceful-timeout 30 app:app"]
