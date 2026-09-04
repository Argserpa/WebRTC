# Cómo rellenar los CSV de métricas (Proyecto 2 — WebRTC)

Guía corta, paralela a `NginxRTMP/monitoring/COMO-TOMAR-METRICAS-P1.md` (P1 ya
completo con 3 repeticiones). Mismos 4 escenarios (E1-E4), mismo esquema de
CSV, misma instrumentación de cliente (`qoe-meter.js`) — solo cambian los
scripts y algunos parámetros específicos de WebRTC.

Piezas del repo:
- `monitoring/toma-metricas-p2.sh` — muestrea Prometheus/`kubectl top` (mismas
  columnas que P1: `N_peers … bitrate_Mbps`).
- `monitoring/loadgen-webrtc.py <N> [BASE] [RAMP]` — abre N receptores WebRTC
  reales (aiortc, recvonly) contra `/offer`.
- `nginx/html/js/qoe-meter.js` (usado desde `index.html`) — mide `startup_ms`
  y `stalls_per_min` (aquí vía `freezeCount` de `getStats()`, con fallback a
  eventos `waiting`).

## Diferencias importantes respecto a P1

- **El player NO arranca en autoplay.** Hay que pulsar el botón "▶ Start
  playback" a mano — los navegadores bloquean el autoplay con audio sin gesto
  del usuario. Esto **no contamina** `startup_ms`: `QoE.start()` se dispara
  *dentro* del mismo `onclick` que arranca la negociación WebRTC, así que el
  cronómetro no arranca hasta que ya has pulsado (a diferencia del bug que sí
  hubo que corregir en P1). Simplemente no lo olvides en cada repetición.
- **`loadgen-webrtc.py` no debe lanzarse dentro del propio pod `video-streamer`**
  para N alto: compartiría *cgroup* con FFmpeg y podría contaminar CPU/memoria
  o directamente hacer OOM. Lánzalo desde tu máquina apuntando al
  *port-forward* (`python3 loadgen-webrtc.py <N> http://localhost:8081`).
- **Egress por loopback no cuenta.** Si el loadgen corre en el mismo host que
  minikube, el tráfico va por `lo` y `node_network_*{device!="lo"}` no lo ve
  (mismo efecto que con `enp7s0` en el E3 de P1). Para `egress_tx_Bps` /
  `egress_rx_Bps` reales necesitarías lanzar el loadgen desde otra máquina de
  la LAN — si no lo haces, esas columnas no son fiables y basta con anotarlo
  en `notas`, ya que el resto de métricas (CPU/mem/QoE) sí son válidas desde
  el mismo host.
- **Techo práctico de N=20.** El manifiesto (`k8s/streamer.yaml`) ya tiene los
  4 cambios que exigió E2 (límite CPU 4000m, memoria 8Gi, `strategy: Recreate`,
  `livenessProbe` relajado) — no hace falta tocar nada. Aun así, N=25 no se
  llegó a alcanzar la vez anterior: documenta el intento igualmente, y si
  colapsa (`OOMKilled` en bucle), anótalo como "no alcanzado" en vez de forzarlo.
- **Puertos**: el servicio `video-streamer` sirve señalización, métricas
  (`/metrics`) y las páginas HTML todo por el **8081** (un único
  *port-forward*, no dos). Prometheus sigue en 9090.

## Requisitos previos

1. Minikube con el Proyecto 2 desplegado (ver `k8s/CAMBIAR-PROYECTO.md` si
   vienes de tener P1 arriba — no pueden convivir en el mismo namespace).
2. Cámara/micrófono (o fuente V4L2/ALSA que use FFmpeg) activa — sin esto
   `bitrate_Mbps` sale a 0.
3. Port-forwards en marcha (usa `./portForwards.sh` o a mano):
   ```bash
   kubectl -n streaming port-forward svc/prometheus 9090:9090
   kubectl -n streaming port-forward --address 0.0.0.0 svc/video-streamer 8081:8081
   ```
4. Comprobar `kubectl top pod -n streaming -l app=video-streamer` (si falla,
   `pod_cpu_m`/`pod_mem_Mi` salen `NA`, el resto del script sigue igual).

Sitúate en `monitoring/` para lanzar los scripts.

---

## E1 — referencia (N=0)

```bash
./toma-metricas-p2.sh -e E1 -r 1 -o ../metricas_p2_escaladoNN.csv
```
(usa `NN` = `00`, `01`, `02` según la repetición — ver la sección de
repeticiones más abajo). Sin loadgen ni navegador: solo el emisor.

## E2 — escalado (N = 1, 5, 10, 20; intento opcional en 25)

Por cada nivel de N:

1. **Carga**: `python3 loadgen-webrtc.py <N> http://localhost:8081`. Espera a
   que se conecten los N peers (mira `TFG_webrtc_peers` en Prometheus o el
   log del loadgen).
2. **Muestras de servidor** (4 muestras × 30 s):
   ```bash
   ./toma-metricas-p2.sh -e E2 -r <nivel> -o ../metricas_p2_escaladoNN.csv
   ```
3. **QoE en navegador**, 4 reproducciones sueltas (una por `rep=1..4`):
   ```
   http://localhost:8081/index.html?escenario=E2&parametro=N=<N>&rep=<1..4>
   ```
   Pulsa "▶ Start playback", deja reproducir un rato razonable, luego en la
   consola:
   ```js
   await QoE.stop()
   ```
   Al terminar las 4: `QoE.downloadCSV()` y pega las columnas a la derecha del
   CSV de servidor (mismo procedimiento que en P1: casan por `escenario` +
   `parametro`/N + `repeticion`↔`muestra`).
4. Cierra el loadgen (Ctrl-C) antes de pasar al siguiente N.

`latencia_g2g_ms` se mide a mano igual que en P1 (cronómetro, emisión→visionado).

## E3 — degradación de red (N=5, dos condiciones)

Misma mecánica de `tc netem` que P1 — **usa el bridge Docker de minikube**
si el cliente corre en el mismo host que minikube (no `enp7s0`); confírmalo
con `docker network ls | grep minikube` y `ss -tnp | grep kubectl` si tienes
dudas, y verifica siempre con
`curl -o /dev/null -s -w '%{time_total}\n' http://localhost:8081/index.html`
antes de dar la muestra por buena.

```bash
python3 loadgen-webrtc.py 5 http://localhost:8081

# Condición A: 1% pérdida + 50 ms RTT  → rep 1
sudo tc qdisc add dev br-<id-bridge-minikube> root netem delay 50ms loss 1%
./toma-metricas-p2.sh -e E3 -r 1 -o ../metricas_p2_escaladoNN.csv
sudo tc qdisc del dev br-<id-bridge-minikube> root netem

# Condición B: 5% pérdida + 200 ms RTT → rep 2
sudo tc qdisc add dev br-<id-bridge-minikube> root netem delay 200ms loss 5%
./toma-metricas-p2.sh -e E3 -r 2 -o ../metricas_p2_escaladoNN.csv
sudo tc qdisc del dev br-<id-bridge-minikube> root netem
```

QoE por condición: `?escenario=E3&parametro=N=5&rep=1..4`. **Retira siempre
la regla `tc` al terminar** y comprueba que no quede ninguna huérfana de una
sesión anterior (`tc qdisc show dev br-<id>` / `... dev enp7s0`).

A diferencia de P1 (que colapsaba funcionalmente), en la ejecución
exploratoria previa P2 toleró la condición de 100ms/2% sin *stalls*, a costa
de más CPU/memoria del pod — no des por sentado que verás el mismo patrón de
"pod deja de responder"; si aparece, anótalo igual en `notas`.

## E4 — resistencia 60 min (N=5, **no** N=10/11)

La primera ejecución (N=11) colapsó por saturación de CPU a los ~41 min sin
completar la hora (ver capítulo 6, sección E4-P2). La tarea pendiente es
repetir a **N=5** — la carga más alta que en E2 quedó claramente por debajo
del límite de *cgroup* (1964m de 4000m) — para saber si a una carga sostenible
el sistema es estable o si hay una fuga de memoria real.

```bash
python3 loadgen-webrtc.py 5 http://localhost:8081
./toma-metricas-p2.sh -e E4 -r 1 -n 120 -i 30 -o ../metricas_p2_E4.csv
# en paralelo, navegador con ?escenario=E4&parametro=N=5&rep=1, reproduciendo
# 1h seguida sin recargar, luego QoE.stop() / QoE.downloadCSV()
```

Vigila en Prometheus durante la hora: `TFG_webrtc_peers` estable en 5 (sin
descensos), CPU del pod lejos del límite de 4000m, memoria sin pendiente
sostenida, `TFG_streaming_latency_max_ms` sin dispararse (el patrón de
colapso ya visto fue CPU pegada al límite → memoria creciendo linealmente →
RTT disparado a los pocos segundos/minutos).

## Repeticiones (igual que P1: 3 ficheros independientes)

El plan exige ≥3 repeticiones independientes por combinación. Para que
luego se puedan procesar igual que P1 (ver `[[test-methodology-p1]]` en la
memoria del asistente si retomas esto con Claude), guarda cada repetición
completa de E1+E2+E3 en su propio fichero:

```
metricas_p2_escalado00.csv   # repetición 1
metricas_p2_escalado01.csv   # repetición 2
metricas_p2_escalado02.csv   # repetición 3
```

con los bloques en **orden N=1,5,10,20/25** (E2) y **orden rep1=Cond.A,
rep2=Cond.B** (E3) dentro de cada fichero — así no depende de que las
etiquetas `rep`/`escenario` estén siempre bien puestas a mano. E4 no necesita
3 repeticiones, con una ejecución completa a N=5 basta.

Ya existe `metricas_p2_escalado.csv` con una ejecución antigua (2026-07-03/06,
antes de este esquema de 3 ficheros) — trátalo como referencia histórica, no
como una de las 3 repeticiones nuevas (mismo caso que `escalado00.csv` de P1:
puede tener metodología distinta a lo que midas ahora, revisa antes de mezclar).
