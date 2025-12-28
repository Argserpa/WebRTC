# WebRTC
servidor de medios con WebRTC

## 1. Construir la imagen de Docker
``` bash
    docker compose build
```
``` bash
    docker compose up
```
``` bash
    docker compose down
```

``` bash
    docker run -d -p 8081 --name video-streamer video-streamer && docker logs -f video-streamer
```
# copiar ficheros desde la imagen
``` bash
    docker cp video-streamer:/hls .
```