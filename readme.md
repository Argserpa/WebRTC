# WebRTC
servidor de medios con WebRTC

## 1. Construir la imagen de Docker
``` bash
    docker compose build
```
``` bash
    docker compose up --build
```
``` bash
    docker compose down
```

``` bash
    docker compose down && docker compose up && docker logs -f video-streamer
```

# copiar ficheros desde la imagen
``` bash
    docker cp video-streamer:/hls .
```