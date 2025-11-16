# WebRTC
Servidor de medios con Nginx  WebRTC

## 1. Construir la imagen de Docker
``` bash
    docker-compose up --build
```

``` bash    
    
    docker run -d -p 8081 --name streamer streamer && docker logs -f streamer
```