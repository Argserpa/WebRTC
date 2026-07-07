#!/usr/bin/env python3
# ─── Servidor de Streaming WebRTC ─────────────────────────────────────────────
# Responsabilidades:
#   1. Runner de FFmpeg   → captura V4L2+ALSA, codifica H.264/AAC, muxea hacia:
#                            a) UDP:10000 (pipe en vivo para el MediaPlayer de aiortc)
#                            b) Ficheros .ts segmentados en /recordings/YYYY-MM-DD/
#   2. Señalización WebRTC → POST /offer → RTCPeerConnection con aiortc
#   3. API de grabaciones  → GET /api/recordings[/{fecha}[/{fichero}/playlist.m3u8]]
#   4. Prometheus          → GET /metrics (bitrate/FPS de FFmpeg, peers, RTT, uptime)
#
# Flujo de medios:
#   V4L2 + ALSA → FFmpeg (libx264 ultrafast / aac) → tee muxer
#     rama A: udp://127.0.0.1:10000 → MediaPlayer → MediaRelay
#                                           ↓ subscribe() por peer
#                                     RTCPeerConnection → navegador
#     rama B: /recordings/YYYY-MM-DD/HH-MM-SS.ts  (nuevo fichero cada SEGMENT_DURATION s)
# ──────────────────────────────────────────────────────────────────────────────
import os
import asyncio
import shlex
import aiohttp_cors
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaPlayer, MediaRelay
from datetime import datetime, date

import json
import time
import re
import psutil
import logging

from FFmpegMetrics import (
    monitor_ffmpeg_stream,
    webrtc_peers, webrtc_offers, webrtc_errors,
    ffmpeg_running, latency_tracker, metrics,
    PrometheusExporter, SegmentBitrateSampler
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# ── Configuración desde variables de entorno ───────────────────────────────────
# Todos los parámetros de ejecución vienen de variables de entorno (definidas en
# docker-compose.yml o en el ConfigMap de k8s). Los valores por defecto permiten
# arrancar localmente sin docker-compose.
USE_NVENC = os.getenv("USE_NVENC", "false").lower() in ("1", "true", "yes")
HLS_DIR = os.getenv("HLS_DIR", "/hls")
RECORD_DIR = os.getenv("RECORD_DIR", "/recordings")
UDP_PORT = int(os.getenv("UDP_PORT", "10001"))          # puerto del pipe interno FFmpeg → aiortc
SEGMENT_DURATION = int(os.getenv("SEGMENT_DURATION", "1800"))  # segundos por fichero de grabación
FFMPEG_LOOP_RESTART_DELAY = 2                           # segundos de espera antes de reiniciar FFmpeg

os.makedirs(HLS_DIR, exist_ok=True)
os.makedirs(RECORD_DIR, exist_ok=True)

# ── Configuración de hardware / dispositivos ───────────────────────────────────
VIDEO_DEVICE = os.getenv("VIDEO_DEVICE", "/dev/video0")
AUDIO_DEVICE = os.getenv("AUDIO_DEVICE", "plughw:1,0")   # dispositivo ALSA: tarjeta 1, subdispositivo 0
SCALE = os.getenv("VIDEO_SCALE", "1280x720")


# ── Pipeline FFmpeg ────────────────────────────────────────────────────────────

def get_today_recording_dir():
    """Devuelve (y crea si no existe) el directorio de grabación del día actual."""
    today_str = date.today().isoformat()   # "YYYY-MM-DD"
    today_dir = os.path.join(RECORD_DIR, today_str)
    os.makedirs(today_dir, exist_ok=True)
    return today_dir


def build_ffmpeg_cmd():
    """
    Construye el comando FFmpeg como una lista.
    Usa el tee muxer con DOS salidas mpegts:
      - Salida 1: UDP para el streaming en vivo
      - Salida 2: ficheros .ts segmentados para las grabaciones

    Las dos salidas son mpegts, así que no hay incompatibilidad de formato.
    Los .ts siempre son válidos — sin moov atom, sin necesidad de finalización.
    """
    # Entrada V4L2 si es un dispositivo real; si no, tratar VIDEO_DEVICE como fichero/URL
    if VIDEO_DEVICE.startswith("/dev/"):
        video_input = ["-f", "v4l2", "-video_size", "1280x720", "-framerate", "10", "-i", VIDEO_DEVICE]
    else:
        video_input = ["-re", "-i", VIDEO_DEVICE]  # -re: leer a velocidad nativa (para ficheros)

    # Entrada de audio ALSA: un solo canal para reducir el ancho de banda
    audio_input = ["-f", "alsa", "-ac", "1", "-i", AUDIO_DEVICE]
    rec_dir = get_today_recording_dir()

    # Tee muxer: envía el mismo stream codificado a varias salidas a la vez.
    # rama A [f=mpegts]: MPEG-TS en bruto por UDP — el MediaPlayer de aiortc lo lee desde aquí
    # rama B [f=segment]: divide en ficheros .ts temporizados; strftime=1 los nombra por la hora del reloj
    tee_output = (
        f"[f=mpegts]udp://127.0.0.1:{UDP_PORT}"
        f"|"
        f"[f=segment"
        f":segment_time={SEGMENT_DURATION}"
        f":segment_format=mpegts"
        f":strftime=1"          # nombra los ficheros como HH-MM-SS.ts según el reloj
        f":reset_timestamps=1"  # cada segmento empieza en timestamp 0 (necesario para reproducción independiente)
        f"]{rec_dir}/%H-%M-%S.ts"
    )

    cmd = [
        "ffmpeg", "-hide_banner", "-y", "-stats", "-loglevel", "warning",
        *video_input,
        *audio_input,
        "-map", "0:v:0", "-map", "1:a:0",             # mapeo explícito: stream de vídeo 0, audio 0
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",  # H.264 de baja latencia
        "-pix_fmt", "yuv420p",                         # formato de pixel con amplia compatibilidad de decodificadores
        "-g", "50",                                    # tamaño de GOP = 50 fotogramas; determina el intervalo de keyframes
        "-c:a", "aac", "-ar", "48000", "-ac", "1",    # audio AAC a 48kHz, mono
        "-f", "tee",
        tee_output,
    ]

    logging.info("Generated FFmpeg command: %s", cmd)
    return cmd


async def ffmpeg_runner():
    """
    Bucle persistente que mantiene FFmpeg corriendo.
    Se reinicia a medianoche para que las grabaciones caigan en el directorio del día nuevo.
    También se reinicia si FFmpeg cae, para recuperarse de errores del dispositivo.
    """
    while True:
        cmd = build_ffmpeg_cmd()
        start_date = date.today()

        logging.info("Starting FFmpeg")
        try:
            if cmd[0] != "ffmpeg":
                raise ValueError(f"Invalid FFmpeg command: {cmd}")

            ffmpeg_running.set(1)   # gauge Prometheus: FFmpeg está corriendo

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,   # capturar stderr para que FFmpegMetrics parsee las stats
                env=os.environ.copy()
            )
            logging.info("FFmpeg PID %s", process.pid)

            # Tarea en background que lee el stderr de FFmpeg y exporta métricas a Prometheus
            stream_id = "main"
            monitor_task = asyncio.create_task(
                monitor_ffmpeg_stream(process, stream_id)
            )

            # Muestreador de bitrate: el muxer tee no reporta bitrate en las stats, así que
            # lo calculamos del crecimiento del fichero .ts de grabación en cada tick de 1 s.
            bitrate_sampler = SegmentBitrateSampler(RECORD_DIR, PrometheusExporter(stream_id))

            # Bucle de vigilancia: comprueba cada segundo si FFmpeg salió o si cambió el día
            while True:
                if process.returncode is not None:
                    break
                if date.today() != start_date:
                    # Medianoche: matar FFmpeg para que la próxima iteración use el directorio del día nuevo
                    logging.info("Date changed, restarting FFmpeg for new recording directory")
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()   # forzar si el terminate no termina en 5 s
                        await process.wait()
                    break
                bitrate_sampler.sample()   # actualiza TFG_streaming_bitrate / TFG_ffmpeg_bitrate_kbits
                await asyncio.sleep(1)

            # Cancelar la tarea de métricas limpiamente
            if not monitor_task.done():
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass

            ffmpeg_running.set(0)   # gauge Prometheus: FFmpeg ha parado
            logging.info("FFmpeg process exited with code %s", process.returncode)

        except Exception:
            ffmpeg_running.set(0)
            logging.exception("Error running FFmpeg")

        # Pausa breve antes de reiniciar para evitar bucles de crash rápidos
        await asyncio.sleep(FFMPEG_LOOP_RESTART_DELAY)


# ── Señalización WebRTC ────────────────────────────────────────────────────────
# pcs: conjunto de RTCPeerConnection activas (una por pestaña del navegador conectada)
# player: MediaPlayer único que lee desde la salida UDP de FFmpeg
# relay: MediaRelay multiplexa la salida del player hacia N conexiones peer
pcs = set()
player = None
relay = None


def update_peer_count():
    """
    Fija TFG_webrtc_peers al número de conexiones REALMENTE activas (estado
    'connected'), recalculándolo desde el conjunto pcs en cada cambio.

    Antes se usaba inc()/dec() manual: +1 en cada /offer y -1 solo al llegar a un
    estado terminal. Pero un mismo visor genera varias offers (reintentos/recargas)
    y las PCs que se quedan atascadas en 'connecting' nunca disparan un estado
    terminal, así que su inc() no se deshacía y la métrica se inflaba (marcaba 5
    con 2 visores). Derivar el valor del estado real es a prueba de desincronización:
    cuenta solo peers conectados y nunca puede quedar descuadrado.
    """
    webrtc_peers.set(sum(1 for p in pcs if p.connectionState == "connected"))

# Configuración ICE — tiene que coincidir con la config iceServers de index.html
config = RTCConfiguration(
    iceServers=[
        RTCIceServer(urls=["stun:stun.l.google.com:19302", "stun:stun.cloudflare.com:3478"]),
        RTCIceServer(urls="turn:openrelay.metered.ca:80", username="openrelayproject", credential="openrelayproject")
    ]
)


async def offer(request):
    """
    POST /offer — endpoint de señalización WebRTC offer/answer.

    Flujo:
      1. Parsear la SDP offer del navegador (body JSON: {sdp, type})
      2. Crear una RTCPeerConnection con la config ICE global
      3. Registrar handlers para cambios de estado y el DataChannel de métricas
      4. Establecer la descripción remota (offer del navegador)
      5. Añadir los tracks de vídeo y audio desde el MediaRelay
      6. Crear y establecer la descripción local (answer del servidor)
      7. Devolver la SDP answer como JSON
    """
    global player, relay

    try:
        params = await request.json()
        offer_desc = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    except Exception as e:
        logging.error("Error parsing WebRTC offer: %s", e)
        webrtc_errors.inc()
        return web.json_response({"error": "Invalid offer"}, status=400)

    logging.info("WebRTC offer received from client")

    pc = RTCPeerConnection(configuration=config)
    pcs.add(pc)
    webrtc_offers.inc()
    # OJO: no se cuenta el peer aquí. La PC acaba de nacer (estado 'new'/'connecting')
    # y todavía no es un visor activo; se contará al pasar a 'connected'.

    logging.info("New PeerConnection (total peers: %s)", len(pcs))

    @pc.on("connectionstatechange")
    async def on_state_change():
        """Actualiza la cuenta de peers y limpia la conexión al fallar/cerrarse."""
        logging.info("Connection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            if pc.connectionState == "failed":
                webrtc_errors.inc()
            await pc.close()
            pcs.discard(pc)
        # Recalcular SIEMPRE: cubre tanto la subida a 'connected' (+1) como la
        # bajada por cierre/fallo (-1), y corrige cualquier descuadre previo.
        update_peer_count()

    @pc.on("datachannel")
    def on_datachannel(channel):
        """
        Gestiona el DataChannel 'metrics' que abre el navegador.
        Mensajes del protocolo:
          latency_ping   → devolver como latency_pong (medición de RTT)
          latency_report → registrar el RTT en LatencyTracker para Prometheus
        """
        logging.info("DataChannel '%s' received from client", channel.label)

        @channel.on("message")
        async def on_message(message):
            try:
                data = json.loads(message)
                if data.get("type") == "latency_ping":
                    # Devolver el timestamp del navegador para que pueda calcular el RTT al recibirlo
                    channel.send(json.dumps({"type": "latency_pong", "timestamp": data["timestamp"]}))
                elif data.get("type") == "latency_report":
                    rtt_ms = float(data.get("latency", 0))
                    latency_tracker.record(rtt_ms)   # actualiza los gauges e histograma de Prometheus
            except Exception as e:
                logging.error("Error handling DataChannel message: %s", e)
                webrtc_errors.inc()

    try:
        await pc.setRemoteDescription(offer_desc)
        # Añadir los tracks del relay: cada suscriptor recibe una copia independiente del stream
        if player.video:
            pc.addTrack(relay.subscribe(player.video))
        if player.audio:
            pc.addTrack(relay.subscribe(player.audio))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception as e:
        logging.error("Error during WebRTC negotiation: %s", e)
        webrtc_errors.inc()
        pcs.discard(pc)
        update_peer_count()   # la PC fallida nunca llegó a 'connected', recomputar
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})


async def on_shutdown(app):
    """Cierra todas las conexiones peer limpiamente al apagar el servidor."""
    await asyncio.gather(*[pc.close() for pc in pcs])
    pcs.clear()
    update_peer_count()   # sin conexiones → gauge a 0
    logging.info("All peer connections closed")


# ── API REST de grabaciones ────────────────────────────────────────────────────

async def api_recording_dates(request):
    """
    GET /api/recordings
    Devuelve todas las fechas que tienen al menos un fichero .ts de grabación,
    ordenadas de más reciente a más antigua.
    Respuesta: { dates: [{date, count}] }
    """
    dates = []
    try:
        for entry in sorted(os.listdir(RECORD_DIR), reverse=True):
            full_path = os.path.join(RECORD_DIR, entry)
            if os.path.isdir(full_path) and re.match(r'^\d{4}-\d{2}-\d{2}$', entry):
                ts_count = len([f for f in os.listdir(full_path) if f.endswith('.ts')])
                if ts_count > 0:
                    dates.append({"date": entry, "count": ts_count})
    except Exception as e:
        logging.error("Error listing recording dates: %s", e)
    return web.json_response({"dates": dates})


async def api_recordings_for_date(request):
    """
    GET /api/recordings/{fecha}
    Devuelve todos los ficheros .ts de la fecha solicitada con sus metadatos.
    Respuesta: { files: [{name, display_time, size_mb, url, download_url}] }
      url          → playlist HLS VOD para reproducción con hls.js
      download_url → URL directa al .ts para descarga en el navegador
    """
    date_str = request.match_info['date']
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return web.json_response({"error": "Invalid date format"}, status=400)

    dir_path = os.path.join(RECORD_DIR, date_str)
    if not os.path.isdir(dir_path):
        return web.json_response({"files": []})

    files = []
    for f in sorted(os.listdir(dir_path)):
        if f.endswith('.ts'):
            full = os.path.join(dir_path, f)
            try:
                stat = os.stat(full)
                # Los ficheros se llaman HH-MM-SS.ts — convertir a HH:MM:SS para mostrar
                time_match = re.match(r'^(\d{2})-(\d{2})-(\d{2})\.ts$', f)
                display_time = f"{time_match.group(1)}:{time_match.group(2)}:{time_match.group(3)}" if time_match else f
                files.append({
                    "name": f,
                    "display_time": display_time,
                    "size_mb": round(stat.st_size / (1024 * 1024), 1),
                    # URL al wrapper .m3u8 (para reproducción con hls.js)
                    "url": f"/api/recordings/{date_str}/{f}/playlist.m3u8",
                    # URL directa al .ts (para descarga)
                    "download_url": f"/recordings/{date_str}/{f}",
                })
            except OSError:
                continue
    return web.json_response({"files": files})


async def api_recording_playlist(request):
    """
    GET /api/recordings/{fecha}/{fichero}/playlist.m3u8
    Genera un playlist HLS VOD mínimo que envuelve un único fichero .ts.
    Esto permite que hls.js reproduzca la grabación en el navegador sin necesitar
    un segmentador HLS real — el .ts completo se trata como un único segmento.

    La duración se estima a partir del tamaño del fichero porque leer la duración
    real requiere demuxear el fichero (costoso). hls.js gestiona bien el fin del
    stream aunque la duración declarada no sea exacta.
    """
    date_str = request.match_info['date']
    filename = request.match_info['file']

    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return web.Response(text="Invalid date", status=400)
    if not filename.endswith('.ts'):
        return web.Response(text="Invalid file", status=400)

    file_path = os.path.join(RECORD_DIR, date_str, filename)
    if not os.path.isfile(file_path):
        return web.Response(text="Not found", status=404)

    # Estimar la duración a partir del tamaño del fichero (más rápido que demuxear).
    # A ~500 kbps de bitrate total: duración ≈ tamaño_bytes / 62500
    # Mínimo SEGMENT_DURATION para que los ficheros cortos/incompletos tengan un playlist válido.
    file_size = os.path.getsize(file_path)
    estimated_duration = max(int(file_size / 62500), SEGMENT_DURATION)

    ts_url = f"/recordings/{date_str}/{filename}"

    # Playlist HLS VOD mínimo: un segmento, con marcador ENDLIST explícito
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{estimated_duration}\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"   # VOD: el playlist es completo y nunca cambia
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        f"#EXTINF:{estimated_duration},\n"
        f"{ts_url}\n"
        "#EXT-X-ENDLIST\n"
    )

    return web.Response(
        text=playlist,
        content_type="application/vnd.apple.mpegurl",
        headers={"Access-Control-Allow-Origin": "*"}
    )


# ── Configuración de la aplicación HTTP ───────────────────────────────────────

async def init_app():
    """Registra todas las rutas y configura CORS para las peticiones cross-origin del navegador."""
    app = web.Application()
    app.router.add_post("/offer", offer)
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/api/recordings", api_recording_dates)
    app.router.add_get("/api/recordings/{date}", api_recordings_for_date)
    app.router.add_get("/api/recordings/{date}/{file}/playlist.m3u8", api_recording_playlist)

    # CORS en todas las rutas para que el navegador pueda llamar a /offer y /api/*
    # desde http://192.168.x.x:8080 (nginx) mientras la API está en :8081 (aiortc)
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")
    })
    for route in list(app.router.routes()):
        cors.add(route)

    app.on_shutdown.append(on_shutdown)
    return app


# ── Factory del MediaPlayer ────────────────────────────────────────────────────

async def create_player_with_retry(udp_port, max_retries=10, delay=1.0):
    """
    Abre un MediaPlayer de aiortc leyendo desde el pipe UDP de FFmpeg.
    Reintenta porque FFmpeg puede tardar un momento en empezar a escribir en el UDP
    después de arrancar (condición de carrera entre el subproceso y el primer paquete).

    Opciones del UDP ajustadas para baja latencia en streaming en vivo:
      fflags=nobuffer+discardcorrupt → sin buffer, descartar fotogramas corruptos
      analyzeduration=0, probesize pequeño → detectar el formato lo antes posible
      max_delay=0, sync=ext → desactivar el buffer de sincronización AV del lector
    """
    udp_url = (
        f"udp://127.0.0.1:{udp_port}"
        f"?fifo_size=2000&overrun_nonfatal=1&buffer_size=32768&reuse=1&timeout=1000000"
    )
    for attempt in range(1, max_retries + 1):
        try:
            p = MediaPlayer(
                udp_url, format="mpegts",
                options={
                    "fflags": "nobuffer+discardcorrupt", "flags": "low_delay",
                    "probesize": "16384", "analyzeduration": "0",
                    "sync": "ext", "max_delay": "0",
                    "thread_type": "slice", "threads": "auto",
                }
            )
            logging.info("MediaPlayer created on attempt %d", attempt)
            return p
        except Exception as e:
            logging.warning("MediaPlayer attempt %d/%d failed: %s", attempt, max_retries, e)
            await asyncio.sleep(delay)
    raise RuntimeError(f"Could not open MediaPlayer after {max_retries} retries")


# ── Punto de entrada ───────────────────────────────────────────────────────────

async def main():
    global player, relay
    # Lanzar FFmpeg como tarea en background — corre en el mismo event loop
    asyncio.create_task(ffmpeg_runner())
    # Esperar a que FFmpeg empiece a escribir en el UDP antes de abrir el MediaPlayer
    player = await create_player_with_retry(UDP_PORT)
    # MediaRelay distribuye el stream del MediaPlayer único a todos los peers conectados
    relay = MediaRelay()

    app = await init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8081)   # escuchar en todas las interfaces
    await site.start()

    logging.info("WebRTC signaling server running on http://0.0.0.0:8081")
    logging.info("Recordings directory: %s", RECORD_DIR)
    logging.info("Segment duration: %d seconds", SEGMENT_DURATION)
    await asyncio.Event().wait()   # correr indefinidamente hasta que se interrumpa


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down")
