ARG BASE_IMAGE=10.10.5.21:5001/library/python:3.11-slim
ARG GO_IMAGE=golang:1.24-bookworm
FROM ${GO_IMAGE} AS backup-tools
COPY tools/backup_artifacts.go tools/restore_archive.go tools/fast_restore.go tools/backup_chunks.go /src/
RUN mkdir -p /backup-tools && \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags='-s -w' -o /backup-tools/linux-amd64 /src/*.go && \
    CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags='-s -w' -o /backup-tools/linux-arm64 /src/*.go

FROM ${BASE_IMAGE} AS dependencies

WORKDIR /app

COPY requirements.txt .
COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels/ -r requirements.txt && rm -rf /tmp/wheels

COPY . /source
RUN rm -rf /source/wheels /source/tests /source/tools /source/__pycache__

FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=dependencies /usr/local /usr/local
COPY --from=dependencies /source /app
COPY --from=backup-tools /backup-tools /usr/local/lib/gitea-manager/backup-artifacts

RUN mkdir -p /app/data/backups

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "600", "app:create_app()"]
