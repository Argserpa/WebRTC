#!/usr/bin/env python3
import os
import asyncio
import shlex
import aiohttp_cors
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaPlayer, MediaRelay
from prometheus_client import (
    Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
)
import time
import av
import logging
# ================== ENV ==================
INPUT = os.getenv("INPUT", "/dev/video0")
USE_NVENC = os.getenv("USE_NVENC", "false").lower() in ("1", "true", "yes")
HLS_DIR = os.getenv("HLS_DIR", "/hls")
RECORD_DIR = os.getenv("RECORD_DIR", "/recordings")
UDP_PORT = int(os.getenv("UDP_PORT", "10000"))
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
        f"-video_size 1280x720 "
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

    cmd = (
        "ffmpeg -hide_banner -loglevel warning -y "
        "-f v4l2 -video_size 1280x720 -framerate 10 -i /dev/video0 "
        "-f alsa -ac 1 -i plughw:1,0 "
        "-vf scale=640:360 "
        "-map 0:v:0 -map 1:a:0 "
        "-c:v libx264 -preset ultrafast -tune zerolatency "
        "-profile:v baseline -level 3.1 -pix_fmt yuv420p "
        "-g 20 -keyint_min 20 -sc_threshold 0 "
        "-x264-params repeat-headers=1 "
        "-bsf:v h264_metadata=aud=insert "
        "-c:a aac -ar 48000 -ac 1 "
        "-mpegts_flags resend_headers "
        "-muxdelay 0 -muxpreload 0 "
        "-f mpegts -pkt_size 1316 udp://127.0.0.1:10000"
    )
    logging.info("cmd00: %s", cmd)
    cmd = (
        "ffmpeg -hide_banner -loglevel warning -y "
        f"{video_in} {audio_in} "
        "-map 0:v:0 -map 1:a:0 "
        "-c:v libx264 -preset ultrafast -tune zerolatency "
        "-profile:v baseline -level 3.1 -pix_fmt yuv420p "
        "-g 10 -keyint_min 20 -sc_threshold 0 "
        "-x264-params repeat-headers=1 "
        "-bsf:v h264_metadata=aud=insert "
        "-c:a aac -ar 48000 -ac 1 "
        "-mpegts_flags resend_headers "
        "-muxdelay 0 -muxpreload 0 "
        "-f mpegts -flush_packets 1 -pkt_size 1316 udp://127.0.0.1:10000 "
    )
    #"-f tee \"{tee}\""

    logging.info("cmd01: %s", cmd)

    #INFO:root:Launching ffmpeg with command: ffmpeg -hide_banner -loglevel warning -y
    # -f v4l2 -input_format yuyv422 -video_size 1280x720 -framerate 10 -i /dev/video0
    # -f alsa -ac 1 -i plughw:1,0
    # -vf "scale=1920:1080"
    # -map 0:v:0 -map 1:a:0
    # -c:v libx264 -preset ultrafast -tune zerolatency
    # -profile:v baseline -level 3.1 -pix_fmt yuv420p
    # -g 20 -keyint_min 20 -sc_threshold 0
    # -x264-params repeat-headers=1
    # -bsf:v h264_metadata=aud=insert
    # -c:a aac -ar 48000 -ac 1
    # -mpegts_flags resend_headers
    # -muxdelay 0 -muxpreload 0
    # -f mpegts -pkt_size 1316 udp://127.0.0.1:10000
    # -bf 0 -probesize 32 -f tee "[f=hls:hls_time=2:hls_list_size=5:hls_flags=delete_segments]/var/www/hls/index.m3u8|[f=mpegts]udp://127.0.0.1:10000"

    return cmd

async def ffmpeg_runner():
    while True:
        ffmpeg_running.set(1)
        cmd = build_ffmpeg_cmd()
        logging.info("Launching ffmpeg with command: %s", cmd)
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.wait()
        print(f"ffmpeg exited ({proc.returncode}), restarting in {FFMPEG_LOOP_RESTART_DELAY}s...")
        ffmpeg_running.set(0)
        await asyncio.sleep(FFMPEG_LOOP_RESTART_DELAY)

# ================== WEBRTC ==================
pcs = set()
player = None
relay = None
#local_ip = "192.168.1.45"

#config = RTCConfiguration(
#    iceServers=[
#        RTCIceServer(urls=["stun:stun.l.google.com:19302"]) # Servidor STUN gratuito
#    ]
#)
async def offer(request):
    global player, relay

    params = await request.json()
    offer = RTCSessionDescription(
        sdp=params["sdp"],
        type=params["type"],
    )

    pc = RTCPeerConnection()
    pcs.add(pc)
    webrtc_offers.inc()
    webrtc_peers.inc()

    logging.info("New PeerConnection")

    @pc.on("connectionstatechange")
    async def on_state_change():
        logging.info("Connection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            webrtc_peers.dec()
            await pc.close()
            pcs.discard(pc)

    await pc.setRemoteDescription(offer)

    # 👉 AQUÍ ESTÁ LA CLAVE
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
    print("All peer connections closed")

# ================== METRICS ==================
START_TIME = time.time()

webrtc_peers = Gauge(
    "webrtc_peers",
    "Active WebRTC peer connections"
)

webrtc_offers = Counter(
    "webrtc_offers_total",
    "Total WebRTC offers received"
)

webrtc_errors = Counter(
    "webrtc_errors_total",
    "Total WebRTC errors"
)

ffmpeg_running = Gauge(
    "ffmpeg_running",
    "FFmpeg process running (1 = yes, 0 = no)"
)

uptime = Gauge(
    "app_uptime_seconds",
    "Application uptime in seconds"
)

# Añade METRICS para utilizar prometheus
async def metrics(request):
    uptime.set(time.time() - START_TIME)
    return web.Response(
        body=generate_latest(),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


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


# ================== MAIN ==================
async def main():
    # iniciar FFmpeg en background
    asyncio.create_task(ffmpeg_runner())
    await asyncio.sleep(4)  # dar tiempo a FFmpeg para abrir UDP

    global player, relay
    player = MediaPlayer(
        f"udp://127.0.0.1:{UDP_PORT}?fifo_size=50000&overrun_nonfatal=1&buffer_size=65535",
        format="mpegts",
        options={"fflags": "nobuffer",
                 "flags": "low_delay",
                 "probesize": "100000",
                 "analyzeduration": "0",
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