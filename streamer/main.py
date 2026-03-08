#!/usr/bin/env python3
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
    ffmpeg_running, latency_tracker, metrics
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# ================== ENV ==================
INPUT = os.getenv("INPUT", "/dev/video0")
USE_NVENC = os.getenv("USE_NVENC", "false").lower() in ("1", "true", "yes")
HLS_DIR = os.getenv("HLS_DIR", "/hls")
RECORD_DIR = os.getenv("RECORD_DIR", "/recordings")
UDP_PORT = int(os.getenv("UDP_PORT", "10001"))
SCALE = os.getenv("VIDEO_SCALE", "640:360")
SEGMENT_DURATION = int(os.getenv("SEGMENT_DURATION", "1800"))

os.makedirs(HLS_DIR, exist_ok=True)
os.makedirs(RECORD_DIR, exist_ok=True)

FFMPEG_LOOP_RESTART_DELAY = 2
VIDEO_DEVICE = os.getenv("VIDEO_DEVICE", "/dev/video0")
AUDIO_DEVICE = os.getenv("AUDIO_DEVICE", "plughw:1,0")


# ================== FFMPEG ==================
def get_today_recording_dir():
    today_str = date.today().isoformat()
    today_dir = os.path.join(RECORD_DIR, today_str)
    os.makedirs(today_dir, exist_ok=True)
    return today_dir


def build_ffmpeg_cmd():
    """
    Builds FFmpeg command as a list.
    Uses tee muxer with TWO mpegts outputs:
      - Output 1: UDP for live streaming
      - Output 2: segmented .ts files for recordings

    Both outputs are mpegts, so no format mismatch issues.
    .ts files are always valid — no moov atom, no finalization needed.
    """
    if INPUT.startswith("/dev/"):
        video_input = ["-f", "v4l2", "-video_size", "1280x720", "-framerate", "10", "-i", INPUT]
    else:
        video_input = ["-re", "-i", INPUT]

    audio_input = ["-f", "alsa", "-ac", "1", "-i", "plughw:1,0"]
    rec_dir = get_today_recording_dir()

    tee_output = (
        f"[f=mpegts]udp://127.0.0.1:{UDP_PORT}"
        f"|"
        f"[f=segment"
        f":segment_time={SEGMENT_DURATION}"
        f":segment_format=mpegts"
        f":strftime=1"
        f":reset_timestamps=1"
        f"]{rec_dir}/%H-%M-%S.ts"
    )

    cmd = [
        "ffmpeg", "-hide_banner", "-y", "-stats", "-loglevel", "warning",
        *video_input,
        *audio_input,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-g", "50",
        "-c:a", "aac", "-ar", "48000", "-ac", "1",
        "-f", "tee",
        tee_output,
    ]

    logging.info("Generated FFmpeg command: %s", cmd)
    return cmd


async def ffmpeg_runner():
    """Ejecuta FFmpeg en bucle. Reinicia a medianoche para nuevo directorio."""
    while True:
        cmd = build_ffmpeg_cmd()
        start_date = date.today()

        logging.info("Starting FFmpeg")
        try:
            if cmd[0] != "ffmpeg":
                raise ValueError(f"Invalid FFmpeg command: {cmd}")

            ffmpeg_running.set(1)

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy()
            )
            logging.info("FFmpeg PID %s", process.pid)

            stream_id = "main"
            monitor_task = asyncio.create_task(
                monitor_ffmpeg_stream(process, stream_id)
            )

            while True:
                if process.returncode is not None:
                    break
                if date.today() != start_date:
                    logging.info("Date changed, restarting FFmpeg for new recording directory")
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                    break
                await asyncio.sleep(1)

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
        RTCIceServer(urls=["stun:stun.l.google.com:19302", "stun:stun.cloudflare.com:3478"]),
        RTCIceServer(urls="turn:openrelay.metered.ca:80", username="openrelayproject", credential="openrelayproject")
    ]
)


async def offer(request):
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
    webrtc_peers.inc()

    logging.info("New PeerConnection (total peers: %s)", len(pcs))

    @pc.on("connectionstatechange")
    async def on_state_change():
        logging.info("Connection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            if webrtc_peers._value.get() > 0:
                webrtc_peers.dec()
            if pc.connectionState == "failed":
                webrtc_errors.inc()
            await pc.close()
            pcs.discard(pc)

    @pc.on("datachannel")
    def on_datachannel(channel):
        logging.info("DataChannel '%s' received from client", channel.label)

        @channel.on("message")
        async def on_message(message):
            try:
                data = json.loads(message)
                if data.get("type") == "latency_ping":
                    channel.send(json.dumps({"type": "latency_pong", "timestamp": data["timestamp"]}))
                elif data.get("type") == "latency_report":
                    rtt_ms = float(data.get("latency", 0))
                    latency_tracker.record(rtt_ms)
            except Exception as e:
                logging.error("Error handling DataChannel message: %s", e)
                webrtc_errors.inc()

    try:
        await pc.setRemoteDescription(offer_desc)
        if player.video:
            pc.addTrack(relay.subscribe(player.video))
        if player.audio:
            pc.addTrack(relay.subscribe(player.audio))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
    except Exception as e:
        logging.error("Error during WebRTC negotiation: %s", e)
        webrtc_errors.inc()
        if webrtc_peers._value.get() > 0:
            webrtc_peers.dec()
        pcs.discard(pc)
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})


async def on_shutdown(app):
    await asyncio.gather(*[pc.close() for pc in pcs])
    pcs.clear()
    logging.info("All peer connections closed")


# ================== RECORDINGS API ==================
async def api_recording_dates(request):
    """GET /api/recordings → lista de fechas con grabaciones"""
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
    """GET /api/recordings/{date} → lista de ficheros para esa fecha"""
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
                time_match = re.match(r'^(\d{2})-(\d{2})-(\d{2})\.ts$', f)
                display_time = f"{time_match.group(1)}:{time_match.group(2)}:{time_match.group(3)}" if time_match else f
                files.append({
                    "name": f,
                    "display_time": display_time,
                    "size_mb": round(stat.st_size / (1024 * 1024), 1),
                    # URL to the .m3u8 wrapper (for hls.js playback)
                    "url": f"/api/recordings/{date_str}/{f}/playlist.m3u8",
                    # Direct .ts URL (for download)
                    "download_url": f"/recordings/{date_str}/{f}",
                })
            except OSError:
                continue
    return web.json_response({"files": files})


async def api_recording_playlist(request):
    """
    GET /api/recordings/{date}/{file}/playlist.m3u8
    Generates a minimal HLS VOD playlist pointing to the .ts file.
    This lets hls.js play the .ts in the browser.
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

    # Get approximate duration from file size and bitrate estimate
    # Better: use a large duration, hls.js will handle the actual end
    file_size = os.path.getsize(file_path)
    # Estimate: ~500kbps total → duration ≈ size / 62500
    estimated_duration = max(int(file_size / 62500), SEGMENT_DURATION)

    ts_url = f"/recordings/{date_str}/{filename}"

    playlist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{estimated_duration}\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
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


# ================== HTTP APP ==================
async def init_app():
    app = web.Application()
    app.router.add_post("/offer", offer)
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/api/recordings", api_recording_dates)
    app.router.add_get("/api/recordings/{date}", api_recordings_for_date)
    app.router.add_get("/api/recordings/{date}/{file}/playlist.m3u8", api_recording_playlist)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")
    })
    for route in list(app.router.routes()):
        cors.add(route)

    app.on_shutdown.append(on_shutdown)
    return app


# ================== MEDIA PLAYER ==================
async def create_player_with_retry(udp_port, max_retries=10, delay=1.0):
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


# ================== MAIN ==================
async def main():
    global player, relay
    asyncio.create_task(ffmpeg_runner())
    player = await create_player_with_retry(UDP_PORT)
    relay = MediaRelay()

    app = await init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8081)
    await site.start()

    logging.info("WebRTC signaling server running on http://0.0.0.0:8081")
    logging.info("Recordings directory: %s", RECORD_DIR)
    logging.info("Segment duration: %d seconds", SEGMENT_DURATION)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Shutting down")