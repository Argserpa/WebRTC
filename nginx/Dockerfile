FROM nginx:stable

RUN mkdir -p /var/www/hls /var/www/recordings

COPY nginx.conf /etc/nginx/nginx.conf
COPY html /usr/share/nginx/html