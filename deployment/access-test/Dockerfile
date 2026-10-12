# 固定基础镜像；安装真实 OpenLDAP 和 Samba AD，禁止用模拟目录替代验收。
FROM debian@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171
ENV DEBIAN_FRONTEND=noninteractive
RUN echo 'slapd slapd/no_configuration boolean true' | debconf-set-selections \
 && apt-get update \
 && apt-get install -y --no-install-recommends slapd ldap-utils samba-ad-dc samba-ad-provision samba-dsdb-modules samba-vfs-modules winbind ldb-tools openssl ca-certificates python3 python3-ldap3 python3-requests python3-cryptography \
 && rm -rf /var/lib/apt/lists/* /etc/samba/smb.conf
COPY start-directory.py /usr/local/bin/start-directory.py
ENTRYPOINT ["python3", "/usr/local/bin/start-directory.py"]
