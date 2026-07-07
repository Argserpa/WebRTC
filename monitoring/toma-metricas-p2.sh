#!/usr/bin/env bash
#
# toma-metricas-p2.sh — Snapshot de las métricas del apartado 2 de
# docs/guia-metricas-comparativa-P1-P2.md que SÍ se pueden leer por comando.
#
# Toma 4 muestras (por defecto) separadas 30 s y las vuelca a un CSV. Las
# métricas manuales (latencia glass-to-glass §2.6 y QoE §2.7: startup/stalls)
# se dejan como columnas vacías para completarlas a mano después.
#
# Requisitos (ver §1 de la guía):
#   - Prometheus accesible por port-forward:  kubectl -n streaming port-forward svc/prometheus 9090:9090
#   - metrics-server habilitado (para `kubectl top pod`)
#
# Uso:
#   ./toma-metricas-p2.sh [-n muestras] [-i intervalo_s] [-o salida.csv] [-e escenario] [-r rep]
# Ejemplo (ahora mismo, con 1 espectador):
#   ./toma-metricas-p2.sh -e E2 -r 1
#
set -euo pipefail

# ---- Parámetros por defecto -------------------------------------------------
MUESTRAS=4
INTERVALO=30
NS=streaming
PROM=http://localhost:9090
ESCENARIO=""
REP=""
SALIDA="metricas_p2_$(date +%Y%m%d_%H%M%S).csv"

while getopts "n:i:o:e:r:h" opt; do
  case "$opt" in
    n) MUESTRAS="$OPTARG" ;;
    i) INTERVALO="$OPTARG" ;;
    o) SALIDA="$OPTARG" ;;
    e) ESCENARIO="$OPTARG" ;;
    r) REP="$OPTARG" ;;
    h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Opción no válida. Usa -h para ayuda." >&2; exit 1 ;;
  esac
done

# ---- Helper de consulta a Prometheus (idéntico al de la guía §2) ------------
# Devuelve el valor escalar de la query, redondeado a $2 decimales, o "NA".
q() {
  curl -s -G "$PROM/api/v1/query" --data-urlencode "query=$1" \
    | python3 -c '
import sys, json
prec = int(sys.argv[1]) if len(sys.argv) > 1 else None
try:
    r = json.load(sys.stdin)["data"]["result"]
    v = r[0]["value"][1] if r else "NA"
    if prec is None or v == "NA":
        print(v)
    elif prec == 0:
        print(int(round(float(v))))          # entero limpio (N, bytes/s...)
    else:
        print(round(float(v), prec))
except Exception:
    print("NA")
' "${2:-}"
}

# ---- Comprobaciones previas -------------------------------------------------
if ! curl -s -o /dev/null "$PROM/-/healthy"; then
  echo "ERROR: no llego a Prometheus en $PROM." >&2
  echo "       Arranca:  kubectl -n $NS port-forward svc/prometheus 9090:9090" >&2
  exit 1
fi

TOP_OK=1
if ! kubectl top pod -n "$NS" -l app=video-streamer --no-headers >/dev/null 2>&1; then
  echo "AVISO: 'kubectl top pod' no responde (metrics-server). Las columnas pod_cpu_m/pod_mem_Mi saldrán NA." >&2
  TOP_OK=0
fi

# ---- Uso del pod video-streamer (§2.5): CPU en millicores, Mem en Mi --------
top_video_streamer() {
  local cpu="NA" mem="NA"
  if [ "$TOP_OK" -eq 1 ]; then
    # Línea tipo:  video-streamer-xxxx   355m   872Mi
    read -r _ cpu mem < <(kubectl top pod -n "$NS" -l app=video-streamer --no-headers 2>/dev/null | head -1)
    cpu="${cpu%m}"     # quita sufijo 'm'
    mem="${mem%Mi}"    # quita sufijo 'Mi'
    : "${cpu:=NA}" "${mem:=NA}"
  fi
  echo "$cpu $mem"
}

# ---- Cabecera del CSV -------------------------------------------------------
# Columnas automáticas + columnas manuales vacías (se rellenan a mano).
if [ ! -f "$SALIDA" ]; then
  echo "escenario,rep,muestra,timestamp,N_peers,cpu_nodo_pct,mem_nodo_pct,egress_tx_Bps,egress_rx_Bps,pod_cpu_m,pod_mem_Mi,bitrate_Mbps,latencia_g2g_ms,startup_ms,stalls_per_min,notas" > "$SALIDA"
fi

echo "Tomando $MUESTRAS muestras cada ${INTERVALO}s → $SALIDA"
echo "(escenario='$ESCENARIO' rep='$REP')"

for i in $(seq 1 "$MUESTRAS"); do
  TS=$(date +%Y-%m-%dT%H:%M:%S)

  # --- Métricas de Prometheus (§2.1–2.4, 2.8) ---
  N=$(q 'TFG_webrtc_peers' 0)
  CPU=$(q '100 - (avg(rate(node_cpu_seconds_total{job="node_exporter",mode="idle"}[2m])) * 100)' 2)
  MEM=$(q '100 * (1 - avg(node_memory_MemAvailable_bytes{job="node_exporter"}) / avg(node_memory_MemTotal_bytes{job="node_exporter"}))' 2)
  TX=$(q 'sum(rate(node_network_transmit_bytes_total{job="node_exporter",device!="lo"}[1m]))' 0)
  RX=$(q 'sum(rate(node_network_receive_bytes_total{job="node_exporter",device!="lo"}[1m]))' 0)
  BR=$(q 'TFG_streaming_bitrate / 1000' 3)

  # --- Uso del pod (§2.5) ---
  read -r POD_CPU POD_MEM < <(top_video_streamer)

  # Columnas manuales vacías: latencia_g2g_ms, startup_ms, stalls_per_min, notas
  echo "${ESCENARIO},${REP},${i},${TS},${N},${CPU},${MEM},${TX},${RX},${POD_CPU},${POD_MEM},${BR},,,," >> "$SALIDA"
  echo "  [$i/$MUESTRAS] $TS  N=$N  CPU%=$CPU  Mem%=$MEM  tx=$TX B/s  pod=${POD_CPU}m/${POD_MEM}Mi  br=${BR}Mbps"

  # Espera entre muestras (no después de la última)
  if [ "$i" -lt "$MUESTRAS" ]; then
    sleep "$INTERVALO"
  fi
done

echo "Hecho. CSV en: $SALIDA"
echo "Rellena a mano: latencia_g2g_ms (§2.6) y startup_ms / stalls_per_min (§2.7, qoe-meter.js)."
