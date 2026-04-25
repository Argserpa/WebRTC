# WebRTC Live Streaming

Servidor de streaming en tiempo real sobre WebRTC con grabación continua, visor web y monitorización.

## Arquitectura

```
Cámara V4L2 + Audio ALSA
        │
        ▼
   [FFmpeg]  ──────── tee muxer ──────────────────────────────────────────────
        │                                                                      │
        │  UDP:10000 (pipe interno)             Segmentos .ts por fecha/hora   │
        ▼                                       /recordings/YYYY-MM-DD/        │
  [aiortc MediaPlayer]                         HH-MM-SS.ts                    │
        │
        ▼
  [MediaRelay]  ──── RTCPeerConnection × N ──── Navegador(es)
        │
  [aiohttp :8081]
        │
        ├── POST /offer          (señalización WebRTC SDP)
        ├── GET  /api/recordings (API de grabaciones)
        ├── GET  /metrics        (métricas Prometheus)
        │
        ▼
  [Nginx :8080]  ──── proxy inverso + HTML/CSS estático ──── Navegador
```

**Servicios del stack:**

| Servicio       | Imagen                | Puerto | Función                                      |
|----------------|-----------------------|--------|----------------------------------------------|
| `video-streamer` | Python/aiortc build | 8081   | FFmpeg + WebRTC signaling + API REST          |
| `hls-web`      | nginx build           | 8080   | Frontend estático + proxy inverso al streamer |
| `prometheus`   | `prom/prometheus`     | 9090   | Scraping de métricas cada 15 s               |
| `grafana`      | `grafana/grafana`     | 3000   | Dashboards (fuente: Prometheus)               |
| `node-exporter`| `prom/node-exporter`  | 9100   | Métricas del SO del host                     |
| `coturn`       | `coturn/coturn`       | 3478   | TURN/STUN relay ICE (opcional)               |

---

## Dos modos de despliegue

Este proyecto tiene dos modos de despliegue con comportamientos distintos. **Elige uno según tu caso de uso:**

### Docker Compose — acceso desde la LAN ✅

El modo recomendado para streaming en red local.  
El streamer usa `network_mode: host`, lo que le da la IP real del host → aiortc genera ICE candidates alcanzables por cualquier dispositivo de la LAN.

```
Dispositivo LAN → 192.168.x.x:8080 (nginx) → proxy → localhost:8081 (streamer)
```

**Cuándo usarlo:** cuando quieres acceder al stream desde otros dispositivos de tu red (teléfono, otro PC, TV...).

---

### Kubernetes / Minikube — solo localhost ⚠️

El modo para desarrollo y pruebas en la propia máquina.  
Con el driver `docker` de Minikube, el nodo vive en una red interna Docker (`192.168.49.x`) que **no es enrutable desde otros dispositivos de la LAN**. El acceso se hace vía `kubectl port-forward` o `minikube tunnel`, ambos limitados al `localhost` del host.

```
Host (localhost) → port-forward → Minikube node (192.168.49.2) → pods
Otro dispositivo LAN → ✗ no alcanzable
```

**Cuándo usarlo:** para desarrollar, probar manifests k8s o validar el despliegue en cloud antes de subir a un cluster real.

> Para guía completa de Kubernetes ver [`k8s/README.md`](k8s/README.md).

---

## Prerrequisitos

```bash
# Verificar cámara disponible
v4l2-ctl --list-devices
v4l2-ctl --device=/dev/video0 --list-formats-ext

# Verificar dispositivos de audio
arecord -l
aplay -l
```

---

## Despliegue con Docker Compose (LAN)

### 1. Configurar el entorno

Editar `.env` con los parámetros de la máquina:

```env
INPUT=/dev/video0        # dispositivo V4L2 de la cámara
USE_NVENC=false          # true si tienes GPU NVIDIA
VIDEO_SCALE=1920:1080    # resolución de salida
SEGMENT_DURATION=1800    # duración de cada fichero de grabación (segundos)
```

### 2. Configurar la IP en `nginx/html/index.html`

El cliente WebRTC apunta directamente al streamer; actualizar con la IP real del host:

```javascript
// nginx/html/index.html — línea ~127
const resp = await fetch("http://192.168.1.45:8081/offer", {
//                                 ↑ cambiar por la IP de tu máquina
```

> Obtener la IP del host: `ip route get 1 | awk '{print $7; exit}'`

### 3. Verificar `nginx/nginx.conf`

Para Docker Compose deben estar activas las líneas con `localhost`:

```nginx
proxy_pass http://localhost:8081;       # ← activa para Docker Compose
#proxy_pass http://video-streamer:8081; # ← comentada (es para k8s)
```

### 4. Construir y arrancar

```bash
docker compose build
docker compose up
```

```bash
# Parar y reconstruir
docker compose down && docker compose build
```

### 5. Acceder a los servicios

| Servicio    | URL                              |
|-------------|----------------------------------|
| Visor live  | http://192.168.x.x:8080          |
| Grabaciones | http://192.168.x.x:8080/recordings.html |
| Grafana     | http://192.168.x.x:3000          |
| Prometheus  | http://192.168.x.x:9090          |

---

## Despliegue con Kubernetes / Minikube

### Diferencias respecto a Docker Compose

Antes de construir la imagen `hls-web`, **cambiar `nginx/nginx.conf`** para que el proxy apunte al Service de k8s en lugar de `localhost`:

```nginx
#proxy_pass http://localhost:8081;       # comentar (es para Docker Compose)
proxy_pass http://video-streamer:8081;   # ← descomentar (nombre del Service k8s)
```

El motivo: en Docker Compose el streamer usa `network_mode: host` y no tiene interfaz en la red Docker, por lo que nginx no puede resolver el nombre `video-streamer`. En Kubernetes el DNS del cluster sí resuelve ese nombre correctamente.

### Construir y cargar imágenes en Minikube

```bash
docker build -t video-streamer:latest ./streamer
docker build -t hls-web:latest ./nginx

minikube image load video-streamer:latest
minikube image load hls-web:latest
```

### Desplegar

```bash
kubectl apply -k k8s/
kubectl get pods -n streaming -w
```

### Acceder (solo desde el host, no desde la LAN)

```bash
kubectl port-forward --address 0.0.0.0 svc/hls-web 8080:80 -n streaming
kubectl port-forward --address 0.0.0.0 svc/video-streamer 8081:8081 -n streaming
```

Abrir: http://localhost:8080

> Para instrucciones completas, FAQ y problemas conocidos → [`k8s/README.md`](k8s/README.md)

---

## Comandos útiles

### Docker Compose

```bash
# Entrar a un contenedor
docker exec -it hls-web /bin/bash
docker exec -it video-streamer /bin/bash

# Copiar grabaciones al local
docker cp hls-web:/var/www/recordings .

# Borrar grabaciones
docker exec video-streamer rm -rf /recordings/*

# Copiar segmentos HLS
docker cp video-streamer:/hls .

# Ver logs en tiempo real
docker compose logs -f streamer
docker compose logs -f web
```

### Diagnóstico de audio y vídeo

```bash
# Probar captura de audio dentro del contenedor
docker exec -it video-streamer arecord -D hw:1,0 -f S16_LE -r 44100 -vv /dev/null
docker exec -it video-streamer arecord -D hw:1,0 -d 5 test.wav -f S16_LE

# Probar el stream UDP (antes de que el navegador conecte)
ffplay -fflags nobuffer -flags low_delay udp://127.0.0.1:10000

# Instalar arecord en el contenedor si no está disponible
docker exec -it video-streamer apt-get update && apt install alsa-utils
```

### Redes Docker

```bash
# Crear red manualmente si no se crea automáticamente
docker network create -d bridge monitoring_network
```
