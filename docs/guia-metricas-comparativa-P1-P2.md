# Guía paso a paso: obtención de métricas comparables P1 (HLS) vs P2 (WebRTC)

**Objetivo:** recoger, de forma reproducible y comparable, todas las métricas de la
Tabla 4.1 del TFG para los dos proyectos, de modo que las gráficas y tablas finales
sean directamente comparables.

- **P1 = NginxRTMP / HLS** (repo *NginxRTMP*).
- **P2 = WebRTC** (este repo).
- Los dos proyectos **no corren a la vez**: comparten el namespace `streaming`. Se
  ejecuta uno, se recogen sus métricas, se cambia al otro (ver §6) y se repite.

---

## 0. ¿Están todas las métricas disponibles? (inventario y estado)

| Métrica (Tabla 4.1) | Eje | Fuente | P1 (HLS) | P2 (WebRTC) | Estado |
|---|---|---|---|---|---|
| **Startup delay (ms)** | QoE cliente | `qoe-meter.js` → CSV | ✅ (repo P1) | ✅ integrado (mismo script, ver §2.7) | OK |
| **Stalls/min** | QoE cliente | `qoe-meter.js` → CSV (waiting en HLS / freeze en WebRTC) | ✅ | ✅ integrado | OK |
| **CPU del nodo vs N** | Capacidad | `node_exporter` | ✅ | ✅ | OK |
| **Memoria del nodo vs N** | Capacidad | `node_exporter` | ✅ | ✅ | OK |
| **Red egress vs N** | Capacidad | `node_exporter` (tx, `device!="lo"`) | ✅ | ✅ | OK (ver caveat §7) |
| **N (audiencia, eje X)** | eje X | P1: viewers del nginx-exporter · P2: `TFG_webrtc_peers` | ✅ | ✅ (corregido) | OK |
| **Coste marginal por cliente (CPU·MB)** | Capacidad | `kubectl top pod` | ✅ | ✅ | OK (metrics-server ya habilitado) |
| **Latencia glass-to-glass (ms)** | Latencia | Cronómetro manual (emitido vs visto) | ✅ | ✅ | OK (manual) |
| **Bitrate de salida (Mbps)** *(contexto, no comparativa)* | — | P2: `TFG_streaming_bitrate` | ✅ | ✅ (corregido) | OK |

> **Respuesta corta:** ya están disponibles **todas** las métricas. La QoE de cliente en P2 se ha
> integrado con el mismo `qoe-meter.js` de P1 (idéntico, mismo md5 → comparabilidad
> garantizada). Capacidad, N, coste por cliente, latencia y bitrate ya estaban listos.
> Uso de la QoE en §2.7.

---

## 1. Prerrequisitos (una vez por sesión de medición)

1. **El proyecto que se va a medir está desplegado** en `streaming` y todos los pods
   en `Running` (ver §6 para cambiar de proyecto).
2. **metrics-server habilitado** (necesario para el coste por cliente):
   ```bash
   minikube addons enable metrics-server
   kubectl top node        # debe devolver datos (no "Metrics API not available")
   ```
3. **Dashboard importado** en Grafana (mismo procedimiento en ambos proyectos):
   ```bash
   kubectl -n streaming port-forward svc/grafana 3000:3000
   # http://localhost:3000  (admin / ver k8s/01-secrets.yaml)
   # P2: Dashboards → Import → monitoring/grafana_proyecto2_webrtc Dashboard.json
   ```
4. **Prometheus accesible** para lecturas precisas por API:
   ```bash
   kubectl -n streaming port-forward svc/prometheus 9090:9090
   ```
5. **Navegador Chrome** para las tomas de QoE de P2 (el `freezeCount` de WebRTC es de
   Chrome). El `qoe-meter.js` ya viene integrado en el reproductor de P2 (§2.7).

---

## 2. Cómo obtener CADA métrica

Cada métrica se puede leer de dos formas: **(A)** mirando el panel del dashboard, o
**(B)** consultando Prometheus por API (más preciso para volcar a CSV). Para los
snapshots de una prueba, conviene usar (B).

Helper de consulta (con el port-forward de Prometheus activo):
```bash
q() { curl -s -G "http://localhost:9090/api/v1/query" --data-urlencode "query=$1" | python3 -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else "NA")'; }
```

### 2.1 N — audiencia (eje X de todas las gráficas)
- **P2 (WebRTC):** panel *"Peers WebRTC conectados (N)"*.
  ```bash
  q 'TFG_webrtc_peers'
  ```
- **P1 (HLS):** contador de viewers del nginx-exporter (panel N del dashboard de P1).

### 2.2 CPU del nodo (%) — igual en P1 y P2 (node_exporter)
```bash
q '100 - (avg(rate(node_cpu_seconds_total{job="node_exporter",mode="idle"}[2m])) * 100)'
```
Panel: *"CPU del servidor (%)"*.

### 2.3 Memoria del nodo (%) — igual en P1 y P2
```bash
q '100 * (1 - avg(node_memory_MemAvailable_bytes{job="node_exporter"}) / avg(node_memory_MemTotal_bytes{job="node_exporter"}))'
```
Panel: *"Memoria del servidor (%)"*.

### 2.4 Red egress (bytes/s) — igual en P1 y P2
```bash
q 'sum(rate(node_network_transmit_bytes_total{job="node_exporter",device!="lo"}[1m]))'   # tx = entrega al cliente
q 'sum(rate(node_network_receive_bytes_total{job="node_exporter",device!="lo"}[1m]))'    # rx
```
Panel: *"Red del nodo (E/S)"*. Multiplica por 8 para bits/s. **Ver caveat §7 (loopback).**

### 2.5 Coste marginal por cliente (CPU·MB) — fuera de Grafana, igual en P1 y P2
```bash
kubectl top pod -n streaming
```
- P2: el pod relevante es `video-streamer`. P1: el pod de nginx.
- Fórmula:  `coste_por_cliente = (uso_con_N − uso_baseline_N0) / N`
- **Baseline P2 medido (N=0):** `video-streamer` ≈ **244m CPU / 1022Mi** (coste fijo de
  FFmpeg, compartido por todos los peers vía MediaRelay; media de 4 tomas el 2026-07-03).
  La memoria en reposo ya rozaba el antiguo límite de 1Gi, por eso se subió a 2Gi en
  `k8s/streamer.yaml` (ver caveat §3).

### 2.6 Latencia glass-to-glass (ms) — manual, igual en P1 y P2
Cronómetro entre un evento visible en la fuente (p.ej. un cronómetro grabado por la
cámara) y su aparición en el reproductor. **No** uses el panel "Latencia RTT": ese es
RTT de red (P2), no glass-to-glass.

### 2.7 QoE de cliente (startup delay, stalls/min) — `qoe-meter.js`, igual en P1 y P2
Ya integrado en el reproductor de P2 (mismo `qoe-meter.js` que P1, cargado desde
`index.html`). El player llama solo a `QoE.attach(video, pc)` + `QoE.start(...)` justo
antes de negociar, así que el startup se mide sin tocar nada. Por cada toma:

1. **Usar Chrome** (los stalls en WebRTC se miden con `freezeCount` de `getStats`, propio
   de Chrome; en otros navegadores cae a eventos `waiting`, menos fiable).
2. Abrir el reproductor con los metadatos del escenario en la URL:
   ```
   http://localhost:8080/?escenario=E2&parametro=N=10&rep=1
   ```
   (`sistema` por defecto = P2). La medición arranca sola al cargar la página.
3. Dejar correr la ventana de la toma (p.ej. 60 s).
4. En la consola del navegador (F12 → Console):
   ```js
   await QoE.stop();     // registra la fila: startup_ms, stalls_per_min, duracion_s...
   QoE.downloadCSV();    // descarga el CSV con todas las tomas acumuladas
   ```
   En multi-visor (E2), repetir en cada pestaña/cliente: cada una mide su propia QoE.

### 2.8 Bitrate de salida (contexto) — P2
```bash
q 'TFG_streaming_bitrate / 1000'   # Mbps
```
Sirve para verificar que ambos proyectos emiten a un bitrate equivalente (misma
resolución/fps) y que la comparación es justa; no es una métrica de comparación en sí.

---

## 3. Generar la carga N (para E2)

Hace falta llevar el sistema a N = 1, 5, 10, 25 espectadores.

- **Con navegadores reales** (necesario para QoE y para egress real en la LAN): abrir
  N pestañas/dispositivos con el reproductor.
- **Con el generador de carga headless** (práctico para CPU/Mem/coste vs N en P2):
  ```bash
  pod=$(kubectl get pod -n streaming -l app=video-streamer -o jsonpath='{.items[0].metadata.name}')
  kubectl cp monitoring/loadgen-webrtc.py streaming/$pod:/tmp/loadgen.py
  kubectl exec -it -n streaming $pod -- python /tmp/loadgen.py 10   # 10 peers
  ```
  Abre 10 receptores WebRTC reales y los mantiene hasta Ctrl-C. Se puede comprobar en el panel
  *Peers WebRTC (N)* que sube a 10.
  > Ver el caveat §7: si el loadgen corre en la misma máquina (loopback), mide
  > CPU/Mem/coste vs N pero **no** egress en la NIC. Para egress, hay que lanzarlo desde otra
  > máquina de la LAN: `python loadgen-webrtc.py 10 http://<host>:8081`.

Metodología recomendada para cada N: subir la carga con el loadgen (CPU/Mem/coste) y,
en paralelo, mantener **1–2 navegadores reales** con `qoe-meter.js` para la QoE a ese N.

---

## 4. Procedimiento de recogida por escenario (E1–E4)

Para **cada** escenario, y para **cada** proyecto, anotar una fila con: `N`, CPU%, Mem%,
egress, coste/cliente, startup, stalls/min, latencia g2g.

### E1 · Baseline (N = 0)
1. Sin espectadores. Esperar ~1 min a que se estabilice.
2. Anotar CPU% (§2.2), Mem% (§2.3), egress (§2.4) y `kubectl top pod` (§2.5) → este es
   el baseline para el coste marginal.

### E2 · Escalado (N = 1, 5, 10, 25)
Por cada N:
1. Llevar la audiencia a N (§3). Confirmar N en el panel de peers/viewers.
2. Esperar ~1–2 min de estabilización.
3. Anotar CPU%, Mem%, egress, `kubectl top pod`, y la QoE (startup, stalls/min) de los
   navegadores reales.
4. Calcular `coste/cliente = (uso_N − baseline_N0) / N`.

### E3 · Red degradada (tc/netem)
1. Con un N fijo (p.ej. N=5), aplicar pérdida/latencia en la interfaz:
   ```bash
   # ejemplo: 100ms de latencia + 2% de pérdida (ajustar iface)
   sudo tc qdisc add dev <iface> root netem delay 100ms loss 2%
   # quitar:  sudo tc qdisc del dev <iface> root netem
   ```
2. Repetir la recogida de QoE (startup, stalls/min) y latencia g2g. Comparar P1 vs P2
   ante las mismas condiciones de red.

### E4 · Larga duración (60 min)
1. N fijo moderado. Dejar corriendo 60 min.
2. Vigilar fugas de memoria (Mem% y `kubectl top pod` al inicio y al final), estabilidad
   (que `TFG_ffmpeg_running=1` y N se mantengan) y stalls acumulados.

---

## 5. Volcado rápido de un snapshot (todas las capacidades de golpe)

Con los dos port-forwards activos, este bloque imprime una fila de capacidad:
```bash
q() { curl -s -G "http://localhost:9090/api/v1/query" --data-urlencode "query=$1" | python3 -c 'import sys,json;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else "NA")'; }
echo "N=$(q 'TFG_webrtc_peers')  CPU%=$(q '100 - (avg(rate(node_cpu_seconds_total{job=\"node_exporter\",mode=\"idle\"}[2m])) * 100)')  Mem%=$(q '100 * (1 - avg(node_memory_MemAvailable_bytes{job=\"node_exporter\"}) / avg(node_memory_MemTotal_bytes{job=\"node_exporter\"}))')  tx_Bps=$(q 'sum(rate(node_network_transmit_bytes_total{job=\"node_exporter\",device!=\"lo\"}[1m]))')"
kubectl top pod -n streaming | grep -E 'video-streamer|nginx'
```

---

## 6. Cambiar de P1 a P2 (o viceversa) en Minikube

Guía completa: `k8s/CAMBIAR-PROYECTO.md` del repo NginxRTMP. Resumen:
```bash
# (Si se tocó el dashboard en la UI, exportarlo antes: borrar el namespace se lleva el PVC de Grafana.)
kubectl delete namespace streaming
kubectl get ns streaming -w        # Ctrl-C cuando diga "NotFound"

# Desplegar el OTRO proyecto (ejemplo P2):
cd /home/args/AquaProjects/WebRTC
eval $(minikube docker-env)
docker build -t video-streamer:latest ./streamer
docker build -t hls-web:latest ./nginx
eval $(minikube docker-env -u)
kubectl apply -k k8s/
kubectl -n streaming get pods -w   # esperar Running
```
Reimportar el dashboard del proyecto y repetir §1–§4. Como los dashboards de P1 y P2
tienen la **misma estructura**, las filas de la tabla comparativa se alinean 1:1.

---

## 7. Métricas que faltan / caveats a tener en cuenta

1. **✅ QoE de cliente en P2 — HECHO.** Se copió el `qoe-meter.js` de P1 (idéntico,
   mismo md5) a `nginx/html/js/` y se cableó en `nginx/html/index.html`
   (`QoE.attach(video, pc)` + `QoE.start()` justo antes de negociar). Uso en §2.7.
   Consideraciones de comparabilidad:
   - Requiere **Chrome** para el `freezeCount` de WebRTC (en otros navegadores usa
     `waiting` como aproximación, menos fiable).
   - En P2 el **t0** del startup se fija al cargar la página (auto), justo antes de la
     señalización WebRTC. Conviene fijar t0 en P1 con el mismo criterio (el gesto/
     carga que lanza la reproducción) para que los startup sean comparables.
   - Si el autoplay del navegador bloquea la reproducción con audio, el primer frame
     (y por tanto el startup) se retrasa hasta el gesto del usuario: pulsar "Start
     playback" de inmediato o silenciar la pestaña para una medición limpia.

2. **Egress vs N solo es real con espectadores fuera de la máquina.** Con visores en
   localhost (o el loadgen en loopback) el tráfico va por `lo` y la query
   `node_network_transmit_bytes_total{device!="lo"}` da ~0. CPU/Mem/coste vs N **sí**
   son válidos en localhost; para **egress** lanza los espectadores desde otras
   máquinas de la LAN.

3. **⚠️ Techo de capacidad de P2 (WebRTC) — HALLAZGO del TFG.** Medido el 2026-07-03
   con carga real (loadgen que negocia por aiohttp y **drena** los tracks, corriendo en
   un pod aparte para no contaminar CPU/mem del streamer). Resultado: **P2 no escala a
   N alto en un solo nodo**, y el límite es doble:
   - **CPU-bound (aiortc hace SRTP por peer en Python):** ~**150–200m de CPU por peer**.
     A **N=20 se satura el límite de 4 cores** (`limits.cpu: 4000m`) del `video-streamer`.
     Es el cuello de botella estructural: a diferencia de P1 (HLS sirve segmentos
     estáticos), P2 cifra SRTP para cada suscriptor.
   - **Crecimiento de memoria bajo carga sostenida:** con N≥20 la memoria **no se
     estabiliza** (crece ~4–5 Mi/s de forma agregada) y acaba pegando un pico que cruza
     el límite → **OOMKilled**. Ventana estable observada: **N=25 ≈ 60 s**, **N=20 ≈ 100 s**
     antes del OOM. Con N≤~15 el crecimiento es lo bastante lento para tomar el snapshot.
   - **Baseline (N=0):** ~150Mi / ~120m CPU (contenedor recién arrancado; en reposo con
     FFmpeg estabiliza ~1Gi). Ver §2.5.

   **Interpretación para la comparativa:** el eje X útil de P2 llega en la práctica a
   **N≈15–20 estable**; a partir de ahí P2 degrada por CPU (SRTP en Python de aiortc) y
   por crecimiento de memoria. Documentar esto como límite de capacidad de P2 frente a
   P1 es un resultado válido (no un fallo de configuración): subir memoria/CPU solo
   retrasa el OOM, no lo elimina, porque el coste de SRTP por peer es intrínseco a la
   arquitectura WebRTC servidor→N-peers de este diseño. Config actual del `video-streamer`:
   `limits: cpu 4000m / memory 8Gi`, liveness con `timeoutSeconds 5` (el default de 1s
   mataba el pod en falso bajo CPU alta).

4. **Latencia glass-to-glass es manual en ambos.** El panel "Latencia RTT" de P2 es
   latencia de red (RTT del DataChannel), útil pero **no** es la glass-to-glass que se
   compara con P1.
