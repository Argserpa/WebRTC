#!/usr/bin/env python3
import os
import asyncio
import shlex
import time
import json
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaPlayer

INPUT = os.getenv("INPUT", "/dev/video0")
USE_NVENC = os.getenv("USE_NVENC", "true").lower() in ("1", "true", "yes")
HLS_DIR = os.getenv("HLS_DIR", "/hls")
RECORD_DIR = os.getenv("RECORD_DIR", "/recordings")
UDP_PORT = int(os.getenv("UDP_PORT", "10000"))
SCALE = os.getenv("VIDEO_SCALE", "1280:720")

os.makedirs(HLS_DIR, exist_ok=True)
os.makedirs(RECORD_DIR, exist_ok=True)

FFMPEG_LOOP_RESTART_DELAY = 2

def build_ffmpeg_cmd():
    # choose encoder
    if USE_NVENC:
        codec = "h264_nvenc"
        scale_filter = f"scale_cuda={SCALE}"
        preset = "p1"
    else:
        codec = "libx264"
        scale_filter = f"scale={SCALE}"
        preset = "veryfast"

    # Use tee muxer to write HLS, MP4 (timestamped) and UDP mpegts for local WebRTC reader.
    # Use bash -lc to let $(date +%s) expand in recording filename.
    hls_path = f"{HLS_DIR}/index.m3u8"
    recordings_pattern = f"{RECORD_DIR}/recording-$(date +%s).mp4"
    udp_target = f"udp://127.0.0.1:{UDP_PORT}"

    tee_outputs = (
        f"[f=hls:hls_time=2:hls_list_size=5:hls_flags=delete_segments]{hls_path}"
        f"|[f=mp4]{recordings_pattern}"
        f"|[f=mpegts]{udp_target}"
    )

    # If the input is a device (v4l2) use -f v4l2, if rtsp or file rely on auto-detect
    if INPUT.startswith("/dev/"):
        input_opts = f"-f v4l2 -i {shlex.quote(INPUT)}"
    else:
        input_opts = f"-re -i {shlex.quote(INPUT)}"

    ffmpeg_cmd = (
        f"ffmpeg -hide_banner -y {input_opts} "
        f"-vf \"{scale_filter}\" "
        f"-c:v {codec} -preset {preset} -g 50 -b:v 4000k "
        f"-f tee -map 0:v \"{tee_outputs}\""
    )
    return ffmpeg_cmd

async def ffmpeg_runner():
    while True:
        cmd = build_ffmpeg_cmd()
        print("Launching ffmpeg with command:")
        print(cmd)
        # run under shell (bash -lc) so tee & date expansion works
        proc = await asyncio.create_subprocess_shell(cmd)
        await proc.wait()
        print(f"ffmpeg exited with {proc.returncode}. Restarting in {FFMPEG_LOOP_RESTART_DELAY}s...")
        await asyncio.sleep(FFMPEG_LOOP_RESTART_DELAY)

# ---------- WebRTC signaling server ----------
pcs = set()

async def offer(request):
    params = await request.json()
    sdp = params["sdp"]
    type_ = params["type"]
    offer = RTCSessionDescription(sdp=sdp, type=type_)

    pc = RTCPeerConnection()
    pcs.add(pc)

    # For cleanup: close after peer disconnect
    @pc.on("connectionstatechange")
    async def on_connstatechange():
        print("Connection state is", pc.connectionState)
        if pc.connectionState == "failed" or pc.connectionState == "closed":
            await pc.close()
            pcs.discard(pc)

    # Create a MediaPlayer that reads the UDP mpegts stream
    # Use av/ffmpeg to open udp
    player = MediaPlayer(f"udp://127.0.0.1:{UDP_PORT}", format="mpegts", options={"stimeout": "5000000"})

    # Add tracks from the player (video/audio if present)
    if player.video:
        pc.addTrack(player.video)
    if player.audio:
        pc.addTrack(player.audio)

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    print("Created answer, returning to client")
    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})

async def on_shutdown(app):
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    print("Closed peer connections")

async def init_app():
    app = web.Application()
    app.router.add_post("/offer", offer)
    app.on_shutdown.append(on_shutdown)
    # Basic CORS preflight support
    async def options_handler(request):
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })
    app.router.add_route("OPTIONS", "/offer", options_handler)
    return app

async def main():
    # start ffmpeg runner in background
    ffmpeg_task = asyncio.create_task(ffmpeg_runner())

    # start aiohttp server for WebRTC signaling
    app = await init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8081)
    await site.start()
    print("Signaling server running on :8081")

    await ffmpeg_task  # normally runs forever

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down")
