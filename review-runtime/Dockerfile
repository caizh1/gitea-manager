ARG BASE_IMAGE=10.10.5.21:5001/library/python:3.11-slim
ARG PR_AGENT_BASE_IMAGE=python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
FROM ${PR_AGENT_BASE_IMAGE} AS pr_agent
COPY review-runtime/vendor/pr-agent-wheels/ /tmp/wheels/
COPY review-runtime/pr-agent-requirements.txt /tmp/requirements.txt
RUN pip install --no-compile --no-cache-dir --no-index --find-links=/tmp/wheels -r /tmp/requirements.txt pr-agent==0.49.0
FROM ${BASE_IMAGE} AS dependencies
WORKDIR /opt/review
COPY backend/wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels flask==3.0.0 requests==2.31.0 gunicorn==21.2.0 && rm -rf /tmp/wheels
FROM ${BASE_IMAGE}
WORKDIR /opt/review
COPY --from=dependencies /usr/local /usr/local
COPY --from=pr_agent /usr/local /opt/pr-agent-runtime
COPY review-runtime/vendor/pr-agent-assets/ /opt/pr-agent-assets/
RUN apt-get update && apt-get install -y --no-install-recommends git=1:2.47.3-0+deb13u1 ca-certificates && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10001 --create-home reviewer
RUN LD_LIBRARY_PATH=/opt/pr-agent-runtime/lib LITELLM_LOCAL_MODEL_COST_MAP=True /opt/pr-agent-runtime/bin/python3.12 -c "from importlib.metadata import version; from pr_agent.commands.pr_reviewer import PRReviewer; assert version('pr-agent') == '0.49.0'"
COPY --chmod=555 review-runtime/vendor/opencode-linux-x64 /opt/review/opencode
COPY --chmod=555 review-runtime/vendor/codex-linux-x64 /opt/review/codex
COPY --chmod=555 review-runtime/vendor/rg-linux-x64 /usr/local/bin/rg
COPY review-runtime/codex_session.py review-runtime/review_tools.py review-runtime/backend_responses.py review-runtime/codex_model.json /opt/review/
COPY review-runtime/executor.py /opt/review/executor.py
COPY review-runtime/pr_agent_session.py review-runtime/pr_agent_runner.py /opt/review/
COPY review-runtime/native_session.py review-runtime/native_gateway.py review-runtime/raw_diagnostics.py review-runtime/runtime_diagnostics.py review-runtime/runtime_probe.py review-runtime/legacy_adapter.py review-runtime/review_result_schema.json review-runtime/StructuredOutput.js /opt/review/
RUN python -c "import native_session, runtime_probe; native_session.load_review_result_schema()"
RUN test "$(/opt/review/opencode --version)" = "1.18.7"
RUN test "$(/opt/review/codex --version)" = "codex-cli 0.169.0"
RUN rg --version | grep -q '^ripgrep 14.1.1'
USER 10001:10001
EXPOSE 5052
CMD ["gunicorn", "--bind", "0.0.0.0:5052", "--workers", "1", "--threads", "32", "--timeout", "190", "--access-logfile", "/dev/null", "executor:app"]
