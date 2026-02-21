FROM alpine:latest

RUN apk update && \
    apk add --no-cache coturn

COPY turnserver.conf /etc/turnserver.conf

EXPOSE 3478/tcp 3478/udp 49152-65535/udp

CMD ["turnserver", "-c", "/etc/turnserver.conf"]