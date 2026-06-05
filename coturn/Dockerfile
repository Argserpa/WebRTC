# ─── Imagen del servidor TURN/STUN (coturn) ───────────────────────────────────
# coturn implementa dos servicios para la negociación ICE de WebRTC:
#   STUN  → permite que el cliente descubra su IP pública (para P2P directo)
#   TURN  → retransmite el tráfico cuando el P2P queda bloqueado por NAT simétrico
#
# Esta imagen es opcional en el stack: por defecto el navegador y el streamer
# emplean el STUN de Google. coturn solo se despliega cuando los espectadores
# se encuentran detrás de un firewall restrictivo.
# ──────────────────────────────────────────────────────────────────────────────
FROM alpine:latest

RUN apk update && \
    apk add --no-cache coturn   # servidor TURN/STUN

COPY turnserver.conf /etc/turnserver.conf

# Puertos:
#   3478/tcp + 3478/udp  → STUN/TURN estándar
#   5349/tcp             → TURNS (TURN sobre TLS)
#   49152-65535/udp      → rango de relay; coturn asigna uno por cada allocación TURN
EXPOSE 3478/tcp 3478/udp 5349/tcp 49152-65535/udp

CMD ["turnserver", "-c", "/etc/turnserver.conf"]
