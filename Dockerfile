FROM python:3.14-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 9986

USER nobody
ENTRYPOINT ["hive-exporter"]
