FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BRAIN_DB_PATH=/data/shared-brain.db

# pip 镜像源：默认官方 PyPI；在中国大陆构建时传
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" .

RUN useradd --create-home --uid 10001 brain && mkdir -p /data && chown brain:brain /data
USER brain

EXPOSE 8787
VOLUME ["/data"]
CMD ["uvicorn", "shared_brain.api:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8787"]

