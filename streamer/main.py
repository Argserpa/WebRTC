#!/usr/bin/env python3
import os
import asyncio
import shlex
import aiohttp_cors
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaPlayer, MediaRelay

import re
import json
import time
import asyncio
import psutil
import av
import logging

from FFmpegMetrics import monitor_ffmpeg_process, monitor_ffmpeg_stream, webrtc_peers, webrtc_offers, ffmpeg_running, \
    latency, metrics

# Configuración básica para el log
logging.basicConfig(
    level=logging.INFO,  # Establece el nivel más bajo que se registrará (INFO, DEBUG, WARNING, etc.)
    format='%(asctime)s - %(levelname)s - %(message)s',  # Define el formato del mensaje
    handlers=[
        logging.StreamHandler()  # Envía los logs a la consola
    ]
)



# ================== ENV ==================
INPUT = os.getenv("INPUT", "/dev/video0")
USE_NVENC = os.getenv("USE_NVENC", "false").lower() in ("1", "true", "yes")
HLS_DIR = os.getenv("HLS_DIR", "/hls")
RECORD_DIR = os.getenv("RECORD_DIR", "/recordings")
UDP_PORT = int(os.getenv("UDP_PORT", "10001"))
#SCALE = os.getenv("VIDEO_SCALE", "1280:720")
SCALE = os.getenv("VIDEO_SCALE", "640:360")

os.makedirs(HLS_DIR, exist_ok=True)
os.makedirs(RECORD_DIR, exist_ok=True)

FFMPEG_LOOP_RESTART_DELAY = 2

logging.basicConfig(level=logging.INFO)
VIDEO_DEVICE = os.getenv("VIDEO_DEVICE", "/dev/video0")
AUDIO_DEVICE = os.getenv("AUDIO_DEVICE", "plughw:1,0")

# ================== FFMPEG ==================
def pick_encoder():
    """Devuelve el codec y opciones según USE_NVENC"""
    if USE_NVENC:
        return {"codec": "h264_nvenc", "pix_fmt": "yuv420p", "scale": "scale", "extra": ""}
    return {"codec": "libx264", "pix_fmt": "yuv420p", "scale": "scale", "extra": ""}

def v4l2_input(dev):
    """Opciones básicas para cámara V4L2"""
    return (
        f"-f v4l2 "
        f"-video_size {SCALE} "
        f"-framerate 10 "
        f"-i {shlex.quote(dev)}"
    )
    # return (
    #     f"-f v4l2 "
    #     f"-input_format yuyv422 "
    #     f"-video_size 1280x720 "
    #     f"-framerate 10 "
    #     f"-i {shlex.quote(dev)}"
    # )

def build_ffmpeg_cmd():
    enc = pick_encoder()
    if INPUT.startswith("/dev/"):
        video_in = v4l2_input(INPUT)
    else:
        video_in = f"-re -i {shlex.quote(INPUT)}"

    # AUDIO input (ALSA)
    audio_in = "-f alsa -ac 1 -i plughw:1,0"

    scale_filter = f"{enc['scale']}={SCALE}"
    hls_path = f"{HLS_DIR}/index.m3u8"
    udp_target = f"udp://127.0.0.1:{UDP_PORT}"

    tee = (
        f"[f=hls:hls_time=2:hls_list_size=5:hls_flags=delete_segments]{hls_path}"
        f"|[f=mpegts]{udp_target}"
    )

    # cmd = (
    # f"ffmpeg -hide_banner -loglevel warning -y "
    # f"{video_in} {audio_in} "
    # f"-use_wallclock_as_timestamps 1 "
    # f"-fflags nobuffer -flags low_delay "
    # f"-vf \"{scale_filter}\" "
    # f"-pix_fmt {enc['pix_fmt']} "
    # f"-c:v {enc['codec']} {enc['extra']} "
    # f"-preset ultrafast -tune zerolatency -g 30 -b:v 4000k "
    # f"-c:a aac -b:a 128k "
    # f"-map 0:v -map 1:a "
    # f"-bf 0 -probesize 32 -f tee \"{tee}\""
    # )

    # cmd = (
    #     "ffmpeg -hide_banner -loglevel warning -y -stats "
    #     "-f v4l2 -video_size 1280x720 -framerate 10 -i /dev/video0 "
    #     "-f alsa -ac 1 -i plughw:1,0 "
    #     "-vf scale=640:360 "
    #     "-map 0:v:0 -map 1:a:0 "
    #     "-c:v libx264 -preset ultrafast -tune zerolatency "
    #     "-profile:v baseline -level 3.1 -pix_fmt yuv420p "
    #     "-g 20 -keyint_min 20 -sc_threshold 0 "
    #     "-x264-params repeat-headers=1 "
    #     "-bsf:v h264_metadata=aud=insert "
    #     "-c:a aac -ar 48000 -ac 1 "
    #     "-mpegts_flags resend_headers "
    #     "-muxdelay 0 -muxpreload 0 "
    #     "-f mpegts -pkt_size 1316 -progress pipe:1 udp://127.0.0.1:10000"
    # )
    #logging.info("cmd00: %s", cmd)
    cmd = (
        "ffmpeg -hide_banner -loglevel warning -y -stats "
        f"{video_in} "
        "-use_wallclock_as_timestamps 1 "
        f"{audio_in} "
        "-fflags nobuffer "
        "-flags low_delay "
        "-map 0:v:0 -map 1:a:0 "
        "-c:v libx264 -preset ultrafast -tune zerolatency "
        "-profile:v baseline -level 3.1 -pix_fmt yuv420p "
        "-g 10 -keyint_min 10 -sc_threshold 0 "
        "-x264-params repeat-headers=1 "
        "-bsf:v h264_metadata=aud=insert "
        "-c:a aac -ar 48000 -ac 1 "
        "-mpegts_flags resend_headers "
        "-muxdelay 0 -muxpreload 0 "
        "-f mpegts -flush_packets 1 -pkt_size 1316 -progress pipe:1 udp://127.0.0.1:10000"
    )
    #"-f tee \"{tee}\""

    # Imprimir el comando para depuración
    logging.info("Generated FFmpeg command parts: %s", cmd)
    return cmd

def command_to_list(command_string):
    """
    Convierte un comando en formato string a una lista de argumentos.

    Args:
        command_string (str): El comando en formato string.

    Returns:
        list: Lista de argumentos separados.
    """
    return shlex.split(command_string)


# ================== GLOBALS ==================
pcs = set()
ffmpeg_process = None   # ← subprocess de FFmpeg
player = None           # ← MediaPlayer (aiortc)
relay = None
ffmpeg_started = asyncio.Event()

async def ffmpeg_runner():
    cmd = build_ffmpeg_cmd()
    #logging.info(" Launching ffmpeg with command: %s", cmd)
    global ffmpeg_process

    ffmpeg_env = os.environ.copy()
    cmd = command_to_list(cmd)

    while True:
        #print(" ".join(cmd))
        logging.info(cmd)

        try:
            # Asegúrate de que ffmpeg_cmd sea una lista y comience con el ejecutable 'ffmpeg'
            if not isinstance(cmd, list) or not cmd[0].endswith('ffmpeg'):
                raise ValueError(f"Invalid FFmpeg command: {cmd}")

            # Establecer que FFmpeg está ejecutándose
            ffmpeg_running.set(1)

            # Iniciar proceso de FFmpeg
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=ffmpeg_env
            )
            logging.info("FFmpeg process started with PID %s", process.pid)
            ffmpeg_process = process   # ← guarda el subprocess aquí

            await asyncio.sleep(1.5)

            logging.info("Procesando salida de error de FFmpeg")
            # Generar un identificador único para este stream (puedes basarlo en timestamp o PID)
#            stream_id = f"stream_{process.pid}_{int(time.time())}"

            # Crear una tarea para monitorizar FFmpeg en paralelo
#            monitor_task = asyncio.create_task(monitor_ffmpeg_stream(process, stream_id))

            if process.returncode is not None:
                logging.error("FFmpeg exited immediately with code %s", process.returncode)
                stderr_output = await process.stderr.read()
                logging.error("FFmpeg stderr: %s", stderr_output.decode(errors='replace'))
                ffmpeg_running.set(0)
                await asyncio.sleep(FFMPEG_LOOP_RESTART_DELAY)
                continue

            logging.info("FFmpeg is running and stable")
            ffmpeg_started.set()

            # Esperar a que el proceso termine
            await process.wait()

            # Cancelar la tarea de monitorización si aún está en ejecución
#            if not monitor_task.done():
#                monitor_task.cancel()
#                try:
#                    await monitor_task
#                except asyncio.CancelledError:
#                    pass

            # Si llegamos aquí, es porque FFmpeg se detuvo
            ffmpeg_running.set(0)
            ffmpeg_started.clear()  # reset para el siguiente ciclo
            #print(f"FFmpeg process exited with code {process.returncode}")
            logging.info("FFmpeg process exited with code %s", process.returncode)

        except Exception as e:
            ffmpeg_running.set(0)
            ffmpeg_started.clear()  # reset para el siguiente ciclo
            #print(f"Error running FFmpeg: {e}")
            logging.exception("Error running FFmpeg")

        # Esperar antes de reiniciar FFmpeg
        await asyncio.sleep(FFMPEG_LOOP_RESTART_DELAY)





# ================== WEBRTC ==================
#local_ip = "192.168.1.45"

# STUN servers (múltiples para redundancia)
# Cloudflare, muy estable
# TURN servers gratuitos (esenciales para NATs difíciles)
config = RTCConfiguration(
    iceServers = [
        RTCIceServer(
            urls=[  "stun:stun.l.google.com:19302" ,
                    "stun:stun1.l.google.com:19302" ,
                  #  "stun:stun2.l.google.com:19302" ,
                    "stun:stun.cloudflare.com:3478"
        ])#,
   #     RTCIceServer(
   #         urls= "turn:openrelay.metered.ca:80",
   #         username= "openrelayproject",
   #         credential= "openrelayproject"
   #     )
        # ,
        # RTCIceServer(
        #     urls= "turn:openrelay.metered.ca:443",
        #     username= "openrelayproject",
        #     credential= "openrelayproject"
        # ),
        # RTCIceServer(
        #     urls= "turn:openrelay.metered.ca:443?transport=tcp",
        #     username= "openrelayproject",
        #     credential= "openrelayproject"
        # )
    ]
)

async def offer(request):
    global player, relay

    params = await request.json()
    offer = RTCSessionDescription(
        sdp=params["sdp"],
        type=params["type"],
    )

    logging.info(f"WebRTC offer received from client")
    logging.info(f"Current number of WebRTC peers: {webrtc_peers}")


    pc = RTCPeerConnection(configuration=config)
    pcs.add(pc)
    webrtc_offers.inc()
    webrtc_peers.inc()

    logging.info("New PeerConnection")

    @pc.on("connectionstatechange")
    async def on_state_change():
        logging.info("Connection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected") :
            if  webrtc_peers.collect()[0].samples[0].value > 0:
                webrtc_peers.dec()
            await pc.close()
            pcs.discard(pc)

    await pc.setRemoteDescription(offer)

    # Crear un canal de datos para medir la latencia
    #channel = pc.createDataChannel("metrics")

    # @channel.on("open")
    # async def on_open():
    #     print("DataChannel is open")
    #
    # @channel.on("message")
    # async def on_message(message):
    #     try:
    #         data = json.loads(message)
    #         if data.get("type") == "latency_ping":
    #             # Responder inmediatamente con un pong
    #             await channel.send(json.dumps({
    #                 "type": "latency_pong",
    #                 "timestamp": data["timestamp"]
    #             }))
    #         elif data.get("type") == "latency_report":
    #             # Registrar latencia reportada por el cliente
    #             latency_value = float(data.get("latency", 0))
    #             latency.observe(latency_value)
    #     except Exception as e:
    #         print(f"Error handling data channel message: {e}")


    if player.video:
        pc.addTrack(relay.subscribe(player.video))
    if player.audio:
        pc.addTrack(relay.subscribe(player.audio))

    gathering_complete = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def on_ice_gathering_state_change():
        if pc.iceGatheringState == "complete":
            gathering_complete.set()

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)



    #answer = await pc.createAnswer()
    #await pc.setLocalDescription(answer)

    await gathering_complete.wait()

    return web.json_response(
        {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }
    )

async def on_shutdown(app):
    await asyncio.gather(*[pc.close() for pc in pcs])
    pcs.clear()
    if ffmpeg_process and ffmpeg_process.returncode is None:
        ffmpeg_process.terminate()
    logging.info("All peer connections closed")


# ================== HTTP APP ==================
async def init_app():
    app = web.Application()
    app.router.add_post("/offer", offer)
    app.router.add_get("/metrics", metrics)

    # Configura CORS para TODAS las rutas automáticamente
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
        )
    })

    # Añade CORS a todas las rutas registradas
    for route in list(app.router.routes()):
        cors.add(route)


    app.on_shutdown.append(on_shutdown)
    return app


async def wait_for_udp(port: int, timeout: int = 10):
    """Espera hasta que FFmpeg empiece a enviar datos UDP"""
    import socket
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", port))
            sock.close()
            await asyncio.sleep(0.5)  # puerto libre aún, FFmpeg no ha arrancado
        except OSError:
            logging.info("UDP port %d is active, FFmpeg is running", port)
            return
        await asyncio.sleep(0.2)
    raise TimeoutError("FFmpeg did not start in time")


# ================== MAIN ==================
async def main():
    # iniciar FFmpeg en background
    asyncio.create_task(ffmpeg_runner())

    # Esperar a que FFmpeg arranque y sea estable (máx 15s)
    logging.info("Waiting for FFmpeg to start...")
    try:
        await asyncio.wait_for(ffmpeg_started.wait(), timeout=15.0)
        logging.info("FFmpeg ready, starting MediaPlayer")
    except asyncio.TimeoutError:
        logging.error("FFmpeg did not start within 15 seconds, check your devices")
        raise

    # Pequeña pausa extra para que el buffer UDP tenga datos
    await asyncio.sleep(0.5)

    global player, relay
    player = MediaPlayer(
        f"udp://127.0.0.1:{UDP_PORT}"
        f"?fifo_size=35000"            # buffer mínimo para no perder paquetes
        f"&overrun_nonfatal=1"
        f"&timeout=500000",           # 0.5s timeout si no llegan datos
        format="mpegts",
        options={
            "fflags": "nobuffer",
            "flags": "low_delay",
            "probesize": "64",        # mínimo posible
            "analyzeduration": "0",   # sin análisis inicial
            "reorder_queue_size": "0",
            "sync": "ext",
            "thread_type": "slice",
            "threads": "auto"
        }
    )
    relay = MediaRelay()

    app = await init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8081)
    await site.start()

    logging.info("WebRTC signaling server running on http://0.0.0.0:8081")
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down")