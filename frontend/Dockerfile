ARG BASE_IMAGE=10.10.5.21:5001/library/nginx:1.31.1
FROM ${BASE_IMAGE}
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY dist/ /usr/share/nginx/html/
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
