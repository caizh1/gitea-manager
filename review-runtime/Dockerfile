ARG BASE_IMAGE=10.10.5.21:5001/library/python:3.11-slim
FROM ${BASE_IMAGE}
WORKDIR /opt/review
COPY backend/wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels flask==3.0.0 requests==2.31.0 gunicorn==21.2.0 && rm -rf /tmp/wheels && useradd --uid 10001 --create-home reviewer
COPY --chmod=555 review-runtime/vendor/opencode-linux-x64 /opt/review/opencode
COPY review-runtime/executor.py /opt/review/executor.py
RUN test "$(/opt/review/opencode --version)" = "1.18.7"
USER 10001:10001
EXPOSE 5052
CMD ["gunicorn", "--bind", "0.0.0.0:5052", "--workers", "1", "--threads", "2", "--timeout", "190", "--access-logfile", "/dev/null", "executor:app"]
