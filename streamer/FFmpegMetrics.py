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
    """Estructura para almacenar métricas de FFmpeg"""
    bitrate: Optional[float] = None  # kbits/s
    fps: Optional[float] = None
    speed: Optional[float] = None    # x (ej: 1.5x)
    time: Optional[str] = None       # HH:MM:SS.ms
    frame: Optional[int] = None
    size: Optional[str] = None       # ej: "128kB", "15MB"

# Regex compilados para mejor rendimiento
FFMPEG_PATTERNS = {
    'bitrate': re.compile(r'bitrate=\s*([\d.]+)\s*kbits/s'),
    'fps': re.compile(r'fps=\s*([\d.]+)'),
    'speed': re.compile(r'speed=\s*([\d.]+)x'),
    'time': re.compile(r'time=\s*(\d{2}:\d{2}:\d{2}\.\d{2})'),
    'frame': re.compile(r'frame=\s*(\d+)'),
    'size': re.compile(r'size=\s*(\S+)')
}

START_TIME = time.time()

# ---- Métricas WebRTC ----
webrtc_peers = Gauge("TFG_webrtc_peers", "Active WebRTC peer connections")
webrtc_offers = Counter("TFG_webrtc_offers_total", "Total WebRTC offers received")
webrtc_errors = Counter("TFG_webrtc_errors_total", "Total WebRTC errors")

# ---- Métricas FFmpeg proceso ----
ffmpeg_running = Gauge("TFG_ffmpeg_running", "FFmpeg process running (1 = yes, 0 = no)")
uptime = Gauge("TFG_app_uptime_seconds", "Application uptime in seconds")

# ---- Métricas de streaming (sin label, para paneles stat) ----
streaming_bitrate = Gauge('TFG_streaming_bitrate', 'Current bitrate in Kbps')
streaming_fps = Gauge('TFG_streaming_fps', 'Current frames per second')

# ---- Métricas FFmpeg detalladas (con label stream_id, para timeseries) ----
ffmpeg_bitrate = Gauge('TFG_ffmpeg_bitrate_kbits', 'Bitrate actual de FFmpeg', ['stream_id'])
ffmpeg_fps = Gauge('TFG_ffmpeg_fps', 'FPS actual de FFmpeg', ['stream_id'])
ffmpeg_speed = Gauge('TFG_ffmpeg_speed', 'Velocidad de procesamiento', ['stream_id'])

# ---- Latencia WebRTC ----
# Gauge con la media de RTT de todos los peers (se recalcula en cada reporte)
latency_avg = Gauge('TFG_streaming_latency_avg_ms', 'Average RTT latency in milliseconds')
latency_max = Gauge('TFG_streaming_latency_max_ms', 'Max RTT latency in milliseconds')
latency_last = Gauge('TFG_streaming_latency_last_ms', 'Last reported RTT latency in milliseconds')
# Histograma para distribución (opcional, para análisis detallado)
latency_histogram = Histogram(
    'TFG_streaming_latency',
    'RTT latency distribution in milliseconds',
    buckets=[10, 25, 50, 100, 200, 500, 1000, 2000, 5000, 10000]
)


class LatencyTracker:
    """
    Mantiene un registro de las últimas mediciones de latencia
    y calcula la media para exportar a Prometheus.
    """
    def __init__(self, window_size=20):
        self._samples = []
        self._window_size = window_size

    def record(self, rtt_ms: float):
        """Registra una nueva medición de RTT"""
        self._samples.append(rtt_ms)
        # Mantener solo las últimas N muestras
        if len(self._samples) > self._window_size:
            self._samples = self._samples[-self._window_size:]

        # Actualizar métricas de Prometheus
        latency_last.set(rtt_ms)
        latency_histogram.observe(rtt_ms)

        if self._samples:
            latency_avg.set(sum(self._samples) / len(self._samples))
            latency_max.set(max(self._samples))


# Instancia global del tracker
latency_tracker = LatencyTracker(window_size=20)


def parse_ffmpeg_output(line: str, metricsParam: FFmpegMetrics) -> FFmpegMetrics:
    """
    Parsea una línea de salida de FFmpeg y actualiza el objeto de métricas.
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
    def __init__(self, stream_id: str):
        self.stream_id = stream_id

    def set_bitrate(self, value: float):
        ffmpeg_bitrate.labels(stream_id=self.stream_id).set(value)
        streaming_bitrate.set(value)

    def set_fps(self, value: float):
        ffmpeg_fps.labels(stream_id=self.stream_id).set(value)
        streaming_fps.set(value)

    def set_speed(self, value: float):
        ffmpeg_speed.labels(stream_id=self.stream_id).set(value)


async def metrics(request):
    uptime.set(time.time() - START_TIME)
    return web.Response(
        body=generate_latest(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


async def read_ffmpeg_stderr(stream):
    """
    Lee stderr de FFmpeg manejando \\r como separador de línea.

    FFmpeg -stats escribe usando \\r (carriage return) para sobrescribir
    la misma línea en terminal. asyncio readline() solo separa por \\n,
    así que las líneas de stats nunca llegan como líneas completas.

    Este generador asíncrono separa tanto por \\r como por \\n.
    """
    buffer = b""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            # Proceso terminó, emitir lo que quede en buffer
            if buffer:
                yield buffer.decode('utf-8', errors='replace').strip()
            break

        buffer += chunk

        # Separar por \r y \n
        while b'\r' in buffer or b'\n' in buffer:
            # Encontrar el delimitador más cercano
            r_pos = buffer.find(b'\r')
            n_pos = buffer.find(b'\n')
            if r_pos == -1:
                r_pos = len(buffer)
            if n_pos == -1:
                n_pos = len(buffer)

            pos = min(r_pos, n_pos)
            line = buffer[:pos]
            # Saltar el delimitador (y \r\n si vienen juntos)
            if pos + 1 < len(buffer) and buffer[pos:pos+2] == b'\r\n':
                buffer = buffer[pos+2:]
            else:
                buffer = buffer[pos+1:]

            decoded = line.decode('utf-8', errors='replace').strip()
            if decoded:
                yield decoded


async def monitor_ffmpeg_stream(process, stream_id: str):
    """
    Monitoriza un proceso FFmpeg existente y exporta sus métricas.
    """
    logging.info("Iniciando monitorización de FFmpeg para stream '%s'", stream_id)
    exporter = PrometheusExporter(stream_id)
    final_metrics = await monitor_ffmpeg_process(process, exporter)
    logging.info("Monitorización finalizada. Últimas métricas: bitrate=%s fps=%s speed=%s",
                 final_metrics.bitrate, final_metrics.fps, final_metrics.speed)


async def monitor_ffmpeg_process(process, metrics_exporter):
    """
    Monitorea el proceso de FFmpeg leyendo stderr con soporte para \\r.
    """
    ffmpeg_metrics = FFmpegMetrics()

    logging.info("monitor_ffmpeg_process: comenzando lectura de stderr")

    async for line_str in read_ffmpeg_stderr(process.stderr):
        try:
            logging.debug("FFmpeg: %s", line_str)

            parse_ffmpeg_output(line_str, ffmpeg_metrics)

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