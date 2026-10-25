ARG MANAGER_IMAGE
FROM ${MANAGER_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends git git-lfs openssh-client iproute2 curl ca-certificates nodejs npm && rm -rf /var/lib/apt/lists/*
COPY remote_backup /checks/remote_backup
ENTRYPOINT ["python", "-m", "remote_backup.driver"]
ENV PYTHONPATH=/checks
