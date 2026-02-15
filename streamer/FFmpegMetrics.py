import re
import logging
import time
from typing import Optional
from dataclasses import dataclass
from aiohttp import web
from prometheus_client import (
    Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST, start_http_server, Info
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
webrtc_peers = Gauge("TFG_webrtc_peers", "Active WebRTC peer connections")
webrtc_offers = Counter("TFG_webrtc_offers_total", "Total WebRTC offers received")
webrtc_errors = Counter("TFG_webrtc_errors_total", "Total WebRTC errors")
ffmpeg_running = Gauge("TFG_ffmpeg_running", "FFmpeg process running (1 = yes, 0 = no)")
uptime = Gauge("TFG_app_uptime_seconds", "Application uptime in seconds")
bitrate = Gauge('TFG_streaming_bitrate', 'Current bitrate in Kbps')
#fps = Gauge('TFG_streaming_fps', 'Current frames per second')
latency = Histogram('TFG_streaming_latency', 'Latency in milliseconds', buckets=[50, 100, 200, 500, 1000, 2000])
ffmpeg_bitrate = Gauge('TFG_ffmpeg_bitrate_kbits', 'Bitrate actual de FFmpeg', ['stream_id'])
ffmpeg_fps = Gauge('TFG_ffmpeg_fps', 'FPS actual de FFmpeg', ['stream_id'])
ffmpeg_speed = Gauge('TFG_ffmpeg_speed', 'Velocidad de procesamiento', ['stream_id'])



def parse_ffmpeg_output(line: str, metricsParam: FFmpegMetrics) -> FFmpegMetrics:
    """
    Parsea una línea de salida de FFmpeg y actualiza el objeto de métricas.

    Args:
        line: Línea de texto de stderr de FFmpeg
        metricsParam: Objeto FFmpegMetrics a actualizar

    Returns:
        El mismo objeto metrics actualizado (para chaining)
    """
    logging.debug("Parsing line: %s", line)
    # Solo procesar si parece ser una línea de progreso
    if not any(key in line for key in ['bitrate=', 'fps=', 'frame=', 'size=']):
        return metricsParam

    try:
        # Extraer bitrate
        if match := FFMPEG_PATTERNS['bitrate'].search(line):
            metricsParam.bitrate = float(match.group(1))
            logging.debug("Bitrate detectado: %.2f kbits/s", metricsParam.bitrate)

        # Extraer FPS
        if match := FFMPEG_PATTERNS['fps'].search(line):
            metricsParam.fps = float(match.group(1))
            logging.debug("FPS detectado: %.2f", metricsParam.fps)

        # Extraer velocidad de procesamiento
        if match := FFMPEG_PATTERNS['speed'].search(line):
            metricsParam.speed = float(match.group(1))
            logging.debug("Speed detectado: %.2fx", metricsParam.speed)

        # Extraer tiempo transcurrido
        if match := FFMPEG_PATTERNS['time'].search(line):
            metricsParam.time = match.group(1)
            logging.debug("Time detectado: %s", metricsParam.time)

        # Extraer número de frame
        if match := FFMPEG_PATTERNS['frame'].search(line):
            metricsParam.frame = int(match.group(1))
            logging.debug("Frame detectado: %d", metricsParam.frame)

        # Extraer tamaño
        if match := FFMPEG_PATTERNS['size'].search(line):
            metricsParam.size = match.group(1)
            logging.debug("Size detectado: %s", metricsParam.size)

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

    def set_fps(self, value: float):
        ffmpeg_fps.labels(stream_id=self.stream_id).set(value)

    def set_speed(self, value: float):
        ffmpeg_speed.labels(stream_id=self.stream_id).set(value)



# Añade METRICS para utilizar prometheus
async def metrics(request):
    uptime.set(time.time() - START_TIME)
    return web.Response(
        body=generate_latest(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )



async def monitor_ffmpeg_stream(process, stream_id: str):
    """
    Monitoriza un proceso FFmpeg existente y exporta sus métricas.

    Args:
        process: Proceso de FFmpeg ya iniciado
        stream_id: Identificador único del stream para las métricas
    """
    logging.info("Iniciando monitorización de ffmpeg para stream %s", stream_id)

    # Crear exportador de métricas
    exporter = PrometheusExporter(stream_id)

    # Monitorizar el proceso
    final_metrics = await monitor_ffmpeg_process(process, exporter)

    logging.info("Monitorización finalizada. Últimas métricas: %s", final_metrics)


async def monitor_ffmpeg_process(process, metrics_exporter):
    """
    Monitorea el proceso de FFmpeg y exporta métricas.

    Args:
        process: Proceso asyncio de FFmpeg
        metrics_exporter: Objeto con métodos set_bitrate() y set_fps()
    """
    metrics = FFmpegMetrics()

    logging.info("monitor_ffmpeg_process")
    async for line in process.stderr:
        try:
            line_str = line.decode('utf-8', errors='replace').strip()

            # Solo loguear en DEBUG para no saturar logs en producción
            logging.debug("FFmpeg stderr: %s", line_str)

            # Parsear la línea
            parse_ffmpeg_output(line_str, metrics)

            # Exportar a Prometheus/Grafana si hay valores válidos
            if metrics.bitrate is not None:
                metrics_exporter.set_bitrate(metrics.bitrate)
            if metrics.fps is not None:
                metrics_exporter.set_fps(metrics.fps)
            if metrics.speed is not None:
                metrics_exporter.set_speed(metrics.speed)


        except Exception as e:
            logging.error("Error procesando línea de FFmpeg: %s", e)
            continue  # Continuar con la siguiente línea en caso de error

    return metrics