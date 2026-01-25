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

# instalar arecord en el contenedor 
``` bash
    apt-get update && apt install alsa-utils
```

# probar el audio dentro del contenedor para ver si funcionan
``` bash
    arecord -D hw:1,0 -f S16_LE -r 44100 -vv /dev/null 
    arecord -D hw:1,0 -d 5 test.wav -f S16_LE
    aplay -D plughw:2,0 test.wav
```