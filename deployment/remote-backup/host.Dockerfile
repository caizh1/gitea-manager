FROM docker:29-dind
RUN apk add --no-cache openssh bash python3 curl git git-lfs coreutils findutils util-linux iproute2 iptables tar unzip
COPY host-entrypoint.sh /usr/local/bin/remote-lab-host
RUN chmod 755 /usr/local/bin/remote-lab-host
ENTRYPOINT ["remote-lab-host"]
