FROM gitea/act_runner:0.2.13 AS runner
FROM node:20-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates bash && rm -rf /var/lib/apt/lists/*
COPY --from=runner /usr/local/bin/act_runner /usr/local/bin/act_runner
COPY upload-artifact /opt/lab-actions/upload-artifact
WORKDIR /runner
ENTRYPOINT ["act_runner"]
