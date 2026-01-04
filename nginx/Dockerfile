FROM nginx:stable

# Crea la carpeta donde se montará el volumen HLS
RUN mkdir -p /var/www/hls

# Copia la configuración de nginx
COPY nginx.conf /etc/nginx/nginx.conf

# Copia tu index.html
COPY html /usr/share/nginx/html