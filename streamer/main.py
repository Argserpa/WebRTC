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
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

# ================== ENV ==================
INPUT = os.getenv("INPUT", "/dev/video0")
USE_NVENC = os.getenv("USE_NVENC", "false").lower() in ("1", "true", "yes")
HLS_DIR = os.getenv("HLS_DIR", "/hls")
RECORD_DIR = os.getenv("RECORD_DIR", "/recordings")
UDP_PORT = int(os.getenv("UDP_PORT", "10001"))
SCALE = os.getenv("VIDEO_SCALE", "640:360")

os.makedirs(HLS_DIR, exist_ok=True)
os.makedirs(RECORD_DIR, exist_ok=True)

FFMPEG_LOOP_RESTART_DELAY = 2

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
        f"-video_size 1280x720 "
        f"-framerate 10 "
        f"-i {shlex.quote(dev)}"
    )


def build_ffmpeg_cmd():
    enc = pick_encoder()
    if INPUT.startswith("/dev/"):
        video_in = v4l2_input(INPUT)
    else:
        video_in = f"-re -i {shlex.quote(INPUT)}"

    audio_in = "-f alsa -ac 1 -i plughw:1,0"

    cmd = (
        "ffmpeg -hide_banner -loglevel warning -y -stats "
        f"{video_in} {audio_in} "
        "-map 0:v:0 -map 1:a:0 "
        "-c:v libx264 -preset ultrafast -tune zerolatency "
        "-profile:v baseline -level 3.1 -pix_fmt yuv420p "
        "-g 10 -keyint_min 10 -sc_threshold 0 "
        "-x264-params repeat-headers=1 "
        "-bsf:v h264_metadata=aud=insert "
        "-c:a aac -ar 48000 -ac 1 "
        "-mpegts_flags resend_headers "
        "-muxdelay 0 -muxpreload 0 "
        "-f mpegts -flush_packets 1 -pkt_size 1316 "
        f"udp://127.0.0.1:{UDP_PORT}"
    )

    logging.info("Generated FFmpeg command: %s", cmd)
    return cmd


def command_to_list(command_string):
    """Convierte un comando en formato string a una lista de argumentos."""
    return shlex.split(command_string)


async def ffmpeg_runner():
    """Ejecuta FFmpeg en bucle, reiniciando si se cae."""
    cmd = build_ffmpeg_cmd()
    cmd = command_to_list(cmd)

    while True:
        logging.info("Starting FFmpeg: %s", cmd)
        try:
            if not isinstance(cmd, list) or not cmd[0].endswith('ffmpeg'):
                raise ValueError(f"Invalid FFmpeg command: {cmd}")

            ffmpeg_running.set(1)

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,   # No usamos -progress, evitar bloqueo
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy()
            )
            logging.info("FFmpeg process started with PID %s", process.pid)

            # NO sobrescribir la variable global 'player' con el proceso FFmpeg
            # Usar stream_id estable para evitar label explosion en Prometheus
            stream_id = "main"

            # Monitorizar stderr para extraer métricas
            monitor_task = asyncio.create_task(
                monitor_ffmpeg_stream(process, stream_id)
            )

            await process.wait()

            # Cancelar monitorización si sigue activa
            if not monitor_task.done():
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass

            ffmpeg_running.set(0)
            logging.info("FFmpeg process exited with code %s", process.returncode)

        except Exception:
            ffmpeg_running.set(0)
            logging.exception("Error running FFmpeg")

        await asyncio.sleep(FFMPEG_LOOP_RESTART_DELAY)


# ================== WEBRTC ==================
pcs = set()
player = None
relay = None

config = RTCConfiguration(
    iceServers=[
        RTCIceServer(
            urls=[
                "stun:stun.l.google.com:19302",
                "stun:stun.cloudflare.com:3478"
            ]
        ),
        RTCIceServer(
            urls="turn:openrelay.metered.ca:80",
            username="openrelayproject",
            credential="openrelayproject"
        )
    ]
)


async def offer(request):
    global player, relay

    params = await request.json()
    offer_desc = RTCSessionDescription(
        sdp=params["sdp"],
        type=params["type"],
    )

    logging.info("WebRTC offer received from client")
    logging.info("Current number of WebRTC peers: %s", webrtc_peers._value.get())

    pc = RTCPeerConnection()
    pcs.add(pc)
    webrtc_offers.inc()
    webrtc_peers.inc()

    logging.info("New PeerConnection")

    @pc.on("connectionstatechange")
    async def on_state_change():
        logging.info("Connection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            if webrtc_peers._value.get() > 0:
                webrtc_peers.dec()
            await pc.close()
            pcs.discard(pc)

    await pc.setRemoteDescription(offer_desc)

    if player.video:
        pc.addTrack(relay.subscribe(player.video))
    if player.audio:
        pc.addTrack(relay.subscribe(player.audio))

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.json_response(
        {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }
    )


async def on_shutdown(app):
    await asyncio.gather(*[pc.close() for pc in pcs])
    pcs.clear()
    logging.info("All peer connections closed")


# ================== HTTP APP ==================
async def init_app():
    app = web.Application()
    app.router.add_post("/offer", offer)
    app.router.add_get("/metrics", metrics)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
        )
    })

    for route in list(app.router.routes()):
        cors.add(route)

    app.on_shutdown.append(on_shutdown)
    return app


# ================== MEDIA PLAYER ==================
async def create_player_with_retry(udp_port, max_retries=10, delay=1.0):
    """
    Intenta crear el MediaPlayer con reintentos,
    en vez de un sleep fijo de 4 segundos.
    """
    udp_url = (
        f"udp://127.0.0.1:{udp_port}"
        f"?fifo_size=2000"
        f"&overrun_nonfatal=1"
        f"&buffer_size=32768"
        f"&reuse=1"
        f"&timeout=1000000"
    )

    for attempt in range(1, max_retries + 1):
        try:
            p = MediaPlayer(
                udp_url,
                format="mpegts",
                options={
                    "fflags": "nobuffer+discardcorrupt",
                    "flags": "low_delay",
                    "probesize": "16384",
                    "analyzeduration": "0",
                    "sync": "ext",
                    "max_delay": "0",
                    "thread_type": "slice",
                    "threads": "auto",
                }
            )
            logging.info("MediaPlayer created on attempt %d", attempt)
            return p
        except Exception as e:
            logging.warning("MediaPlayer attempt %d/%d failed: %s", attempt, max_retries, e)
            await asyncio.sleep(delay)

    raise RuntimeError(f"Could not open MediaPlayer after {max_retries} retries")


# ================== MAIN ==================
async def main():
    global player, relay

    # Iniciar FFmpeg en background
    asyncio.create_task(ffmpeg_runner())

    # Crear MediaPlayer con reintentos (reemplaza el sleep(4) fijo)
    player = await create_player_with_retry(UDP_PORT)
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