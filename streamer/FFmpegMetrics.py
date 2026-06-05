# ─── Métricas de FFmpeg y exportador Prometheus ────────────────────────────────
# Responsabilidades:
#   1. Definir todas las métricas Prometheus de la aplicación (WebRTC + FFmpeg + latencia)
#   2. Parsear la salida stderr de FFmpeg y actualizar los valores de las métricas
#   3. Mantener un registro de la latencia RTT de WebRTC con ventana deslizante
#   4. Gestionar el separador de línea \r que usa FFmpeg en su salida de stats
#   5. Exponer las métricas en el endpoint /metrics
# ──────────────────────────────────────────────────────────────────────────────
import re
import logging
import time
from typing import Optional
from dataclasses import dataclass
from aiohttp import web
from prometheus_client import (
    Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
)

@dataclass
class FFmpegMetrics:
    """Contenedor con los últimos valores parseados de una línea de stats de FFmpeg."""
    bitrate: Optional[float] = None  # kbits/s — bitrate actual de salida
    fps: Optional[float] = None      # fotogramas por segundo que se están codificando
    speed: Optional[float] = None    # multiplicador de velocidad de procesado (1.0x = tiempo real)
    time: Optional[str] = None       # posición del stream codificado como HH:MM:SS.ms
    frame: Optional[int] = None      # total de fotogramas codificados hasta ahora
    size: Optional[str] = None       # tamaño de los datos de salida (ej: "128kB", "15MB")

# Patrones regex compilados para cada campo de las stats de FFmpeg.
# Las líneas de stats tienen esta forma: "frame= 123 fps= 10 ... bitrate=456.7kbits/s speed=1.00x"
# Se compilan una sola vez al importar el módulo para no repetirlo en el bucle de lectura.
FFMPEG_PATTERNS = {
    'bitrate': re.compile(r'bitrate=\s*([\d.]+)\s*kbits/s'),
    'fps': re.compile(r'fps=\s*([\d.]+)'),
    'speed': re.compile(r'speed=\s*([\d.]+)x'),
    'time': re.compile(r'time=\s*(\d{2}:\d{2}:\d{2}\.\d{2})'),
    'frame': re.compile(r'frame=\s*(\d+)'),
    'size': re.compile(r'size=\s*(\S+)')
}

# Momento de arranque de la aplicación — lo usa el gauge de uptime
START_TIME = time.time()

# ── Definición de métricas Prometheus ─────────────────────────────────────────
# Prefijo TFG_ (Trabajo Final de Grado)
# Tipos de métricas:
#   Counter   → solo sube (offers recibidas, errores)
#   Gauge     → valor actual (peers conectados, bitrate, uptime)
#   Histogram → distribución de valores (buckets de latencia)

# Seguimiento de conexiones WebRTC
webrtc_peers = Gauge("TFG_webrtc_peers", "Active WebRTC peer connections")
webrtc_offers = Counter("TFG_webrtc_offers_total", "Total WebRTC offers received")
webrtc_errors = Counter("TFG_webrtc_errors_total", "Total WebRTC errors")

# Estado del proceso FFmpeg
ffmpeg_running = Gauge("TFG_ffmpeg_running", "FFmpeg process running (1 = yes, 0 = no)")
uptime = Gauge("TFG_app_uptime_seconds", "Application uptime in seconds")

# Stats de streaming sin label — cómodos para los paneles "stat" de valor único en Grafana
streaming_bitrate = Gauge('TFG_streaming_bitrate', 'Current bitrate in Kbps')
streaming_fps = Gauge('TFG_streaming_fps', 'Current frames per second')

# Stats de streaming con label stream_id — permiten gráficas de series temporales
# cuando hay varios streams FFmpeg activos simultáneamente
ffmpeg_bitrate = Gauge('TFG_ffmpeg_bitrate_kbits', 'Bitrate actual de FFmpeg', ['stream_id'])
ffmpeg_fps = Gauge('TFG_ffmpeg_fps', 'FPS actual de FFmpeg', ['stream_id'])
ffmpeg_speed = Gauge('TFG_ffmpeg_speed', 'Velocidad de procesamiento', ['stream_id'])

# Latencia RTT de WebRTC — medida via ping/pong por el DataChannel de index.html
latency_avg = Gauge('TFG_streaming_latency_avg_ms', 'Average RTT latency in milliseconds')
latency_max = Gauge('TFG_streaming_latency_max_ms', 'Max RTT latency in milliseconds')
latency_last = Gauge('TFG_streaming_latency_last_ms', 'Last reported RTT latency in milliseconds')
# Buckets en milisegundos: desde menos de 10 ms en LAN hasta 10 s de timeout
latency_histogram = Histogram(
    'TFG_streaming_latency',
    'RTT latency distribution in milliseconds',
    buckets=[10, 25, 50, 100, 200, 500, 1000, 2000, 5000, 10000]
)


class LatencyTracker:
    """
    Acumulador con ventana deslizante para las mediciones de RTT de WebRTC.

    Guarda las últimas `window_size` muestras y recalcula media y máximo en
    cada nueva medición. Actualiza los gauges de Prometheus de forma síncrona
    en cada llamada a record() para que el scraping siempre vea valores frescos.
    """
    def __init__(self, window_size=20):
        self._samples = []
        self._window_size = window_size

    def record(self, rtt_ms: float):
        """Registra una nueva medición de RTT y actualiza todas las métricas de latencia."""
        self._samples.append(rtt_ms)
        # Mantener solo las últimas N muestras (ventana deslizante)
        if len(self._samples) > self._window_size:
            self._samples = self._samples[-self._window_size:]

        # Actualizar el último valor inmediatamente
        latency_last.set(rtt_ms)
        # Añadir al histograma (registra la distribución a lo largo del tiempo)
        latency_histogram.observe(rtt_ms)

        if self._samples:
            latency_avg.set(sum(self._samples) / len(self._samples))
            latency_max.set(max(self._samples))


# Instancia global compartida por todos los handlers de mensajes del DataChannel
latency_tracker = LatencyTracker(window_size=20)


def parse_ffmpeg_output(line: str, metricsParam: FFmpegMetrics) -> FFmpegMetrics:
    """
    Parsea una línea de stats de FFmpeg y actualiza el dataclass en su lugar.
    Devuelve el mismo dataclass (mutado) por comodidad al encadenar llamadas.

    Sale antes de tiempo si la línea no contiene ninguna palabra clave de stats:
    FFmpeg también escribe líneas de advertencias y de info del muxer que no
    hay que parsear.
    """
    if not any(key in line for key in ['bitrate=', 'fps=', 'frame=', 'size=']):
        return metricsParam

    try:
        if match := FFMPEG_PATTERNS['bitrate'].search(line):
            metricsParam.bitrate = float(match.group(1))

        if match := FFMPEG_PATTERNS['fps'].search(line):
            metricsParam.fps = float(match.group(1))

        if match := FFMPEG_PATTERNS['speed'].search(line):
            metricsParam.speed = float(match.group(1))

        if match := FFMPEG_PATTERNS['time'].search(line):
            metricsParam.time = match.group(1)

        if match := FFMPEG_PATTERNS['frame'].search(line):
            metricsParam.frame = int(match.group(1))

        if match := FFMPEG_PATTERNS['size'].search(line):
            metricsParam.size = match.group(1)

    except ValueError as e:
        logging.warning("Error de conversión numérica en línea FFmpeg: %s", e)
    except Exception as e:
        logging.error("Error inesperado parseando FFmpeg: %s", e)

    return metricsParam


class PrometheusExporter:
    """
    Puente entre los valores de FFmpegMetrics y los gauges de Prometheus con label.
    Cada stream_id tiene sus propios valores para que Grafana pueda graficar varios
    streams a la vez. También actualiza los gauges sin label para los paneles de valor único.
    """
    def __init__(self, stream_id: str):
        self.stream_id = stream_id

    def set_bitrate(self, value: float):
        ffmpeg_bitrate.labels(stream_id=self.stream_id).set(value)
        streaming_bitrate.set(value)   # copia sin label para paneles de stat único

    def set_fps(self, value: float):
        ffmpeg_fps.labels(stream_id=self.stream_id).set(value)
        streaming_fps.set(value)

    def set_speed(self, value: float):
        ffmpeg_speed.labels(stream_id=self.stream_id).set(value)


async def metrics(request):
    """
    GET /metrics — endpoint de scraping para Prometheus.
    Actualiza el gauge de uptime en cada scraping y serializa todas las métricas
    registradas al formato de texto estándar de Prometheus.
    """
    uptime.set(time.time() - START_TIME)
    return web.Response(
        body=generate_latest(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


# ── Lector de stderr de FFmpeg ─────────────────────────────────────────────────

async def read_ffmpeg_stderr(stream):
    """
    Generador asíncrono que devuelve líneas decodificadas del stderr de FFmpeg.

    FFmpeg con -stats escribe el progreso usando \\r (retorno de carro) para
    sobreescribir la misma línea en el terminal. El readline() de asyncio solo
    corta por \\n, así que las líneas de stats nunca llegan completas.

    Este generador lee chunks en bruto y los divide tanto por \\r como por \\n,
    devolviendo una cadena decodificada por cada línea lógica (stats o error).
    """
    buffer = b""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            # El proceso terminó — vaciar lo que quede en el buffer
            if buffer:
                yield buffer.decode('utf-8', errors='replace').strip()
            break

        buffer += chunk

        # Dividir por el delimitador que aparezca primero (\r o \n)
        while b'\r' in buffer or b'\n' in buffer:
            r_pos = buffer.find(b'\r')
            n_pos = buffer.find(b'\n')
            if r_pos == -1:
                r_pos = len(buffer)
            if n_pos == -1:
                n_pos = len(buffer)

            pos = min(r_pos, n_pos)
            line = buffer[:pos]
            # Saltar el delimitador (saltar dos bytes si es \r\n juntos)
            if pos + 1 < len(buffer) and buffer[pos:pos+2] == b'\r\n':
                buffer = buffer[pos+2:]
            else:
                buffer = buffer[pos+1:]

            decoded = line.decode('utf-8', errors='replace').strip()
            if decoded:
                yield decoded


async def monitor_ffmpeg_stream(process, stream_id: str):
    """
    Punto de entrada de la corutina de monitorización de FFmpeg.
    Crea un PrometheusExporter para este stream_id y delega en el bucle principal.
    Al terminar FFmpeg, registra las últimas métricas en el log.
    """
    logging.info("Iniciando monitorización de FFmpeg para stream '%s'", stream_id)
    exporter = PrometheusExporter(stream_id)
    final_metrics = await monitor_ffmpeg_process(process, exporter)
    logging.info("Monitorización finalizada. Últimas métricas: bitrate=%s fps=%s speed=%s",
                 final_metrics.bitrate, final_metrics.fps, final_metrics.speed)


async def monitor_ffmpeg_process(process, metrics_exporter):
    """
    Bucle principal de monitorización del stderr de FFmpeg.
    Lee línea a línea (manejando los separadores \\r), parsea los campos de stats
    y exporta los valores actualizados a Prometheus en cada línea de estadísticas.
    """
    ffmpeg_metrics = FFmpegMetrics()

    logging.info("monitor_ffmpeg_process: comenzando lectura de stderr")

    async for line_str in read_ffmpeg_stderr(process.stderr):
        try:
            logging.debug("FFmpeg: %s", line_str)

            parse_ffmpeg_output(line_str, ffmpeg_metrics)

            # Solo exportar los campos que ya se han parseado (None = aún no aparecieron)
            if ffmpeg_metrics.bitrate is not None:
                metrics_exporter.set_bitrate(ffmpeg_metrics.bitrate)
            if ffmpeg_metrics.fps is not None:
                metrics_exporter.set_fps(ffmpeg_metrics.fps)
            if ffmpeg_metrics.speed is not None:
                metrics_exporter.set_speed(ffmpeg_metrics.speed)

        except Exception as e:
            logging.error("Error procesando línea de FFmpeg: %s", e)
            continue

    return ffmpeg_metrics
