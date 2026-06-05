# ─── Imagen del servidor web Nginx ────────────────────────────────────────────
# Sirve el frontend estático del visor WebRTC y actúa como proxy inverso hacia
# el servidor Python (aiohttp) que corre en el puerto 8081.
#
# Esta imagen no contiene lógica de aplicación, solo configuración de nginx.
# ──────────────────────────────────────────────────────────────────────────────
FROM nginx:stable

# Crear los directorios antes de que los volúmenes los monten en tiempo de ejecución.
# Sin esto, Docker puede crearlos como root con permisos incorrectos.
#   /var/www/hls        → aquí llegan los segmentos HLS que escribe FFmpeg
#   /var/www/recordings → aquí llegan los .ts de grabación que escribe FFmpeg
RUN mkdir -p /var/www/hls /var/www/recordings

# Sustituir la config por defecto de nginx con la del proyecto
# (un único servidor en :8080 con rutas HLS, grabaciones y proxy)
COPY nginx.conf /etc/nginx/nginx.conf

# Copiar los assets del frontend:
#   index.html      → visor WebRTC en directo
#   recordings.html → navegador de grabaciones con player hls.js
#   css/            → hojas de estilo (común, live, recordings)
#   images/         → favicons
COPY html /usr/share/nginx/html
