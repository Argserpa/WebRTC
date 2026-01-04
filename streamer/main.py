#!/usr/bin/env python3
import os
import asyncio
import shlex
from aiohttp import web

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer, MediaRelay

# ================== ENV ==================
INPUT = os.getenv("INPUT", "/dev/video0")
USE_NVENC = os.getenv("USE_NVENC", "false").lower() in ("1", "true", "yes")
HLS_DIR = os.getenv("HLS_DIR", "/hls")
RECORD_DIR = os.getenv("RECORD_DIR", "/recordings")
UDP_PORT = int(os.getenv("UDP_PORT", "10000"))
SCALE = os.getenv("VIDEO_SCALE", "1280:720")

os.makedirs(HLS_DIR, exist_ok=True)
os.makedirs(RECORD_DIR, exist_ok=True)

FFMPEG_LOOP_RESTART_DELAY = 2



# ================== FFMPEG ==================

def pick_encoder():
    if USE_NVENC:
        return {
            "codec": "h264_nvenc",
            "pix_fmt": "yuv420p",
            "scale": "scale",
            "extra": ""
        }
    return {
        "codec": "libx264",
        "pix_fmt": "yuv420p",
        "scale": "scale",
        "extra": ""
    }


def v4l2_input(dev):
    return (
        f"-f v4l2 "
        f"-input_format yuyv422 "
        f"-video_size 1280x720 "
        f"-framerate 10 "
        f"-i {shlex.quote(dev)}"
    )

def build_ffmpeg_cmd():
    enc = pick_encoder()

    if INPUT.startswith("/dev/"):
        input_opts = v4l2_input(INPUT)
    else:
        input_opts = f"-re -i {shlex.quote(INPUT)}"

    scale_filter = f"{enc['scale']}={SCALE}"
    hls_path = f"{HLS_DIR}/index.m3u8"
    udp_target = f"udp://127.0.0.1:{UDP_PORT}?pkt_size=1316"

    tee = (
        f"[f=hls:hls_time=2:hls_list_size=5:hls_flags=delete_segments]{hls_path}"
        f"|[f=mpegts]{udp_target}"
    )

    return (
        f"ffmpeg -hide_banner -loglevel error -y "
        f"{input_opts} "
        f"-vf \"{scale_filter}\" "
        f"-pix_fmt {enc['pix_fmt']} "
        f"-c:v {enc['codec']} {enc['extra']} "
        f"-preset veryfast -g 50 -b:v 4000k "
        f"-f tee -map 0:v \"{tee}\""
    )

async def ffmpeg_runner():
    while True:
        cmd = build_ffmpeg_cmd()
        print("Launching ffmpeg:")
        print(cmd)
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.wait()
        print(f"ffmpeg exited ({proc.returncode}), restarting in {FFMPEG_LOOP_RESTART_DELAY}s")
        await asyncio.sleep(FFMPEG_LOOP_RESTART_DELAY)

# ================== WEBRTC ==================
pcs = set()
player = None
relay = MediaRelay()

async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(
        sdp=params["sdp"],
        type=params["type"]
    )

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_connstatechange():
        print("Connection state:", pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            pcs.discard(pc)

    # ---- ADD TRACKS BEFORE ANSWER (CRITICAL FIX) ----

    if player and player.video:
        pc.addTrack(relay.subscribe(player.video))
    else:
        print("⚠️ No video track available yet")
    if player and player.audio:
        pc.addTrack(relay.subscribe(player.audio))
    else:
        print("⚠️ No audio track available")

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.json_response({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    })

async def on_shutdown(app):
    await asyncio.gather(*[pc.close() for pc in pcs])
    pcs.clear()
    print("All peer connections closed")

# ================== HTTP APP ==================
async def init_app():
    app = web.Application()
    app.router.add_post("/offer", offer)

    async def options_handler(request):
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    app.router.add_route("OPTIONS", "/offer", options_handler)
    app.on_shutdown.append(on_shutdown)
    return app

# ================== MAIN ==================
async def main():
    asyncio.create_task(ffmpeg_runner())
    await asyncio.sleep(2)

    global player
    player = MediaPlayer(
        f"udp://127.0.0.1:{UDP_PORT}?overrun_nonfatal=1&fifo_size=5000000",
        format="mpegts"
    )
    app = await init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8081)
    await site.start()

    print("WebRTC signaling server running on http://0.0.0.0:8081")
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down")