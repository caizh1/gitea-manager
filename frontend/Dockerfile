ARG BASE_IMAGE=10.10.5.21:5001/library/nginx:1.31.1
FROM ${BASE_IMAGE}
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY nginx-https.conf /etc/nginx/manager-https.conf
COPY start-nginx.sh /usr/local/bin/start-manager-nginx
COPY dist/ /usr/share/nginx/html/
EXPOSE 80 443
CMD ["sh", "/usr/local/bin/start-manager-nginx"]
