# WebRTC — Kubernetes Setup

## Requisitos

- [minikube](https://minikube.sigs.k8s.io/docs/start/) >= 1.32
- [kubectl](https://kubernetes.io/docs/tasks/tools/) >= 1.29
- Docker (para construir las imágenes)
- Host Linux con `/dev/video0` y `/dev/snd` disponibles

---

## 1. Arrancar Minikube

```bash
minikube start --driver=docker --cpus=4 --memory=4096
```

> ⚠️ El streamer necesita acceso a dispositivos del host (`/dev/video0`, `/dev/snd`).
> Minikube con driver `docker` monta el host automáticamente en el nodo.
> Si los dispositivos no aparecen dentro del nodo, usa `--driver=kvm2` o `--driver=none`.

---

## 2. Obtener la IP del nodo y actualizar la config

```bash
minikube ip   # ej: 192.168.49.2
```

Editar `streamer.yaml` → ConfigMap `streamer-config`:
```yaml
TURN_SERVER_HOST: "192.168.49.2"   # ← poner aquí la IP de minikube ip
```

Editar `coturn.yaml` → ConfigMap `coturn-config`:
```yaml
# external-ip=192.168.49.2         # ← descomentar y poner la misma IP
```

---

## 3. Construir y cargar las imágenes en Minikube

```bash
# Construir con Docker normal
docker build -t video-streamer:latest ./streamer
docker build -t hls-web:latest ./nginx

# Cargar en el daemon de Minikube
minikube image load video-streamer:latest
minikube image load hls-web:latest
```

> Con `imagePullPolicy: Never` Kubernetes nunca intentará bajar estas imágenes
> de un registry externo.

---

## 4. Desplegar todo

```bash
# Desde la carpeta k8s/
kubectl apply -k .

# O manifest por manifest (mismo orden):
kubectl apply -f 00-namespace.yaml
kubectl apply -f 01-secrets.yaml
kubectl apply -f streamer.yaml
kubectl apply -f web.yaml
kubectl apply -f coturn.yaml
kubectl apply -f prometheus.yaml
kubectl apply -f grafana.yaml
kubectl apply -f node-exporter.yaml
```

Verificar que todos los pods están `Running`:
```bash
kubectl get pods -n streaming -w
```

---

## 5. Acceder a los servicios

### Streamer (señalización WebRTC) y Web (HLS) — LoadBalancer

Ambos servicios son `LoadBalancer`. Requieren tunnel en una terminal separada:

```bash
# Mantener abierta mientras se usa
minikube tunnel
```

Obtener las IPs asignadas:
```bash
kubectl get svc -n streaming
```

| Servicio        | Puerto | URL                          |
|-----------------|--------|------------------------------|
| `hls-web`       | 80     | `http://<EXTERNAL-IP>`       |
| `video-streamer`| 8081   | `http://<EXTERNAL-IP>:8081`  |

> ⚠️ El HTML del cliente usa `http://${location.hostname}:8081/offer`.
> Si `hls-web` y `video-streamer` tienen IPs distintas, actualizar el JS
> para apuntar explícitamente a la IP del streamer.

### Grafana — NodePort

```bash
minikube service grafana -n streaming
# o: http://<minikube ip>:30300
```

Credenciales: `admin` / `prom_admin`

### TURN server — NodePort

- UDP: `<minikube ip>:30478`
- TCP: `<minikube ip>:30479`

Actualizar `webrtc.html` / `index.html` para usar este servidor TURN:
```js
{
  urls: "turn:<minikube ip>:30478",
  username: "turnuser",
  credential: "turnpass"
}
```

### Prometheus — port-forward (acceso puntual)

```bash
kubectl port-forward svc/prometheus 9090:9090 -n streaming
# → http://localhost:9090
```

---

## 6. Operaciones habituales

```bash
# Ver logs del streamer
kubectl logs -f deployment/video-streamer -n streaming

# Ver logs del nginx HLS
kubectl logs -f deployment/hls-web -n streaming

# Recargar config de Prometheus sin reiniciar
kubectl port-forward svc/prometheus 9090:9090 -n streaming &
curl -X POST http://localhost:9090/-/reload

# Eliminar todo el despliegue
kubectl delete namespace streaming
```

---

## Diferencias respecto al proyecto RTPM

| Aspecto | RTPM | WebRTC |
|---|---|---|
| Imagen principal | `nginx-rtmp-server` | `video-streamer` + `hls-web` |
| Exporter separado | `rtmp-exporter` + `nginx-exporter` | No necesario — `/metrics` en el streamer |
| Dispositivos host | No | `/dev/video0` + `/dev/snd` (privileged) |
| Volumen compartido | No | PVC `hls-data` entre streamer y web |
| TURN server | No | `coturn` (NodePort 30478) |



# WebRTC — Kubernetes Setup

## Requisitos

- [minikube](https://minikube.sigs.k8s.io/docs/start/) >= 1.32
- [kubectl](https://kubernetes.io/docs/tasks/tools/) >= 1.29
- Docker
- Host Linux con `/dev/video0` y `/dev/snd` disponibles

---

## 1. Arrancar Minikube

```bash
minikube start --driver=docker --cpus=4 --memory=4096
```

---

## 2. Obtener la IP del nodo y actualizar la config

```bash
minikube ip   # ej: 192.168.49.2
```

Editar `streamer.yaml` → ConfigMap `streamer-config`:
```yaml
TURN_SERVER_HOST: "192.168.49.2"   # ← poner aquí la IP de minikube ip
```

Editar `coturn.yaml` → ConfigMap `coturn-config`:
```yaml
# external-ip=192.168.49.2         # ← descomentar y poner la misma IP
```

---

## 3. Construir y cargar las imágenes en Minikube

```bash
docker build -t video-streamer:latest ./streamer
docker build -t hls-web:latest ./nginx
minikube image load video-streamer:latest
minikube image load hls-web:latest
```

> Con `imagePullPolicy: Never` Kubernetes nunca intentará bajar estas imágenes
> de un registry externo.

---

## 4. Desplegar todo

```bash
# Desde la carpeta k8s/
kubectl apply -k .
```

Verificar que todos los pods están Running:
```bash
kubectl get pods -n streaming -w
```

---

## 5. Acceder a los servicios

### Web (HLS + WebRTC) — port-forward

Con el driver docker en Linux, los NodePorts y LoadBalancer IPs no son
accesibles directamente desde el host. Usar port-forward:

```bash
kubectl port-forward svc/hls-web 8080:80 -n streaming
```

Abrir: http://localhost:8080

### Grafana — NodePort

```bash
minikube service grafana -n streaming
# o: http://<minikube ip>:30300
```

Credenciales: admin / prom_admin

### Prometheus — port-forward

```bash
kubectl port-forward svc/prometheus 9090:9090 -n streaming
```

---

## 6. Operaciones habituales

```bash
# Ver logs del streamer
kubectl logs -f deployment/video-streamer -n streaming

# Ver logs del nginx HLS
kubectl logs -f deployment/hls-web -n streaming

# Actualizar imagen (usar tag nuevo para forzar recarga en minikube)
docker build -t hls-web:v2 ./nginx
minikube image load hls-web:v2
kubectl set image deployment/hls-web hls-web=hls-web:v2 -n streaming

# Eliminar todo el despliegue
kubectl delete namespace streaming
```

---

## FAQ — Problemas encontrados en el despliegue inicial

### ❌ Connection refused al hacer port-forward al pod hls-web

Causa: El nginx.conf escucha en el puerto 8080, pero el Service apuntaba
al targetPort: 80.

Fix aplicado en web.yaml: targetPort: 8080 en el Service y
containerPort: 8080 en el Deployment. Ya está corregido en los YAMLs.

---

### ❌ 502 Bad Gateway al intentar conectar WebRTC

Causa: La imagen hls-web se construyó con el nginx.conf antiguo que tenía
proxy_pass http://192.168.1.45:8081 (IP local de la máquina de desarrollo)
en lugar del nombre del Service de Kubernetes video-streamer:8081.

Fix aplicado en nginx/nginx.conf:
proxy_pass http://video-streamer:8081;

Lección: Si kubectl exec -- cat /etc/nginx/nginx.conf muestra la config
correcta pero el contenedor sigue usando la antigua, minikube tiene la imagen
cacheada. Solución: usar un tag nuevo (v2, v3...) en lugar de :latest.

    docker build -t hls-web:v4 ./nginx
    minikube image load hls-web:v4
    kubectl set image deployment/hls-web hls-web=hls-web:v4 -n streaming
    kubectl rollout status deployment/hls-web -n streaming

---

### ❌ ICE connection failed — WebRTC no conecta

Causa: aiortc genera ICE candidates usando la IP del pod (10.244.x.x).
Esas IPs son internas del cluster y el navegador no puede alcanzarlas.

Fix aplicado en streamer.yaml:
hostNetwork: true
dnsPolicy: ClusterFirstWithHostNet

Con hostNetwork: true el pod comparte la red del nodo minikube, así los ICE
candidates contienen la IP real del nodo (192.168.49.2) que sí es alcanzable.

Nota: con hostNetwork: true el pod ocupa directamente el puerto 8081 del nodo.
No puede haber dos pods con el mismo puerto en el mismo nodo.

---

### ❌ minikube image load no actualiza la imagen

Causa: Minikube cachea imágenes por nombre+tag. Con :latest puede servir
la versión antigua.

Fix: usar tags versionados:
docker build -t hls-web:v2 ./nginx
minikube image load hls-web:v2
kubectl set image deployment/hls-web hls-web=hls-web:v2 -n streaming

---

### ❌ La IP 192.168.49.2 no es accesible desde el host

Causa: Con el driver docker, minikube corre dentro de un contenedor Docker.
La red 192.168.49.0/24 no se enruta automáticamente al host.

Fix: usar kubectl port-forward en lugar de acceder por IP directa:
kubectl port-forward svc/hls-web 8080:80 -n streaming

minikube tunnel teóricamente lo resuelve pero en algunos entornos no funciona
con el driver docker.
