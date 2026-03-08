# WebRTC
servidor de medios con WebRTC

## Instalación y configuración del proyecto
Este proyecto utiliza una cámara conectada al equipo (en mi caso por usb)
con los comandos unix
``` bash
arecord -l
aplay -l
```
podrá ver qué dispositivos existen en su máquina para luego poder utilizarlos como entrada y salida de audio.<br> 
La entrada de video se revisa con
```
    v4l2-ctl --list-devices
    v4l2-ctl --device=/dev/videoX --list-formats-ext
```
para listar los dispositivos y sus características de grabado<br>
Los parámetros dependientes de la máquina son:<br>
3 en el main.py  bajo el comentario SYSTEM DEPENDENTS
la dirección IP del nginx, que debe configurarse manualmente con la del equipo

## 1. Construir, ejecutar y parar la imagen de Docker
``` bash
    docker compose build
```
``` bash
    docker compose up
```
``` bash
    docker compose down && docker compose build
```

## 2. Otros comandos interesantes
### Copiar ficheros desde la imagen
``` bash
    docker cp video-streamer:/hls .
```

### Instalar arecord en el contenedor
Para comprobar si está grabando imágenes dentro del contenedor. Se ingresa al contenedor y se instala el paquete

``` bash
    apt-get update && apt install alsa-utils
```

### Probar el audio dentro del contenedor para ver si funcionan
``` bash
    arecord -D hw:1,0 -f S16_LE -r 44100 -vv /dev/null 
    arecord -D hw:1,0 -d 5 test.wav -f S16_LE
    aplay -D plughw:2,0 test.wav
```

### Probar el video tanto en host como en contenedor 
``` bash
    ffplay -fflags nobuffer -flags low_delay udp://127.0.0.1:10000
```
### Crear una red (si no la crea automáticamente)
``` bash
    docker network create -d bridge monitoring_network
```    
### Ingresar a uno de los contenedores por el nombre
``` bash    
    docker exec -it hls-web "/bin/bash"
```    
### Copiar las grabaciones al local
``` bash    
    docker cp hls-web:/var/www/recordings .
```    
### Borrar las grabaciones
``` bash    
    docker exec video-streamer rm -rf /recordings/*
```