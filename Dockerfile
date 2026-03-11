FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV ORBIT_HUB_HOST=0.0.0.0
ENV ORBIT_HUB_PORT=8080
ENV ORBIT_HUB_DB=/var/lib/orbit/hub.sqlite3
ENV ORBIT_OBJECT_ROOT=/var/lib/orbit/objects

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src

RUN pip install --no-cache-dir .

RUN mkdir -p /var/lib/orbit

EXPOSE 8080

VOLUME ["/var/lib/orbit"]

CMD ["orbit", "hub", "serve"]
