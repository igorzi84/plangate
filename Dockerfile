FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
WORKDIR /app
COPY . /app
RUN uv sync --locked --no-dev
# Remove pip to remove deps with cves
RUN python -m pip uninstall -y pip
ENV PLANGATE_SQLITE_DB_PATH=/data/plangate.db
VOLUME ["/data"]
ENV PLANGATE_HOST=0.0.0.0
ENTRYPOINT ["uv", "run", "--no-sync", "plangate"]
 