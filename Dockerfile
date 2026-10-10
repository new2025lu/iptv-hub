FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    TZ=Asia/Shanghai

WORKDIR /app

RUN groupadd --gid 1001 iptv \
    && useradd --uid 1000 --gid 1001 --no-create-home --shell /usr/sbin/nologin iptv

COPY requirements.txt ./
# PyPI 索引：默认走阿里云镜像。生产环境（本机）直连 pypi.org 实测约 0.1 MB/s，
# 一次 `--build` 会卡在 pip install 十几分钟甚至更久；镜像源实测 2 MB/s。
# 需要换源或改回官方时用构建参数覆盖，不必改这个文件：
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple/
#   PIP_INDEX_URL= docker compose build --build-arg PIP_INDEX_URL=
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=1000:1001 app ./app
RUN mkdir -p /data && chown -R 1000:1001 /data /app

USER 1000:1001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
