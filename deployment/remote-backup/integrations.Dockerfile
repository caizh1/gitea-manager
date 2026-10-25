ARG MANAGER_IMAGE
FROM ${MANAGER_IMAGE}
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends slapd ldap-utils \
    && rm -rf /var/lib/apt/lists/*
COPY integration_server.py /opt/lab/integration_server.py
ENTRYPOINT ["python", "/opt/lab/integration_server.py"]
