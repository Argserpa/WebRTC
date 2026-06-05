# ─── Imagen del streamer ──────────────────────────────────────────────────────
# Servicio Python 3.10 que:
#   1. Lanza FFmpeg (entrada V4L2 + ALSA → libx264/aac → tee muxer)
#   2. Expone la señalización WebRTC via aiortc (servidor aiohttp en :8081)
#   3. Sirve la API REST de grabaciones
#   4. Exporta métricas Prometheus en /metrics
#
# Nota de build: av (PyAV) se instala desde un wheel binario precompilado porque
# compilarlo desde fuente requiere que las cabeceras de FFmpeg del sistema coincidan
# exactamente con la versión instalada, y suele dar problemas.
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# ── Dependencias del sistema ───────────────────────────────────────────────────
# ffmpeg: el núcleo del pipeline multimedia (captura, codificación, muxing, segmentación)
# libav*: cabeceras de desarrollo que necesita PyAV (bindings Python de FFmpeg)
# alsa-utils, libv4l*: herramientas de usuario para captura de audio ALSA y vídeo V4L2
# libssl-dev, libffi-dev, pkg-config: requeridos por aiortc (DTLS, ICE, fingerprinting)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libssl-dev \
    libffi-dev \
    pkg-config \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libavfilter-dev \
    libswscale-dev \
    libswresample-dev \
    alsa-utils \
    v4l-utils \
    libv4l-0 \
    libv4l-dev \
    v4l-utils \
    libv4l-0t64 \
    libv4l2rds0t64 \
    libv4lconvert0t64 \
    && rm -rf /var/lib/apt/lists/*

# ── Dependencias Python — capa base ───────────────────────────────────────────
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Instalar av (PyAV) desde wheel binario — evita compilar contra las cabeceras del sistema.
# Versión fijada a 16.0.0 por compatibilidad con los internos de MediaPlayer de aiortc.
RUN pip install --no-cache-dir --only-binary av av==16.0.0
#RUN pip install --no-cache-dir "cython<3.0" "setuptools<65"   # alternativa si no hay wheel disponible

# Runtime principal: aiohttp (servidor HTTP), aiortc (WebRTC), numpy/opencv (procesado de imagen)
RUN pip install --no-cache-dir aiohttp aiortc numpy opencv-python-headless

# ── Dependencias de la aplicación ─────────────────────────────────────────────
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ── Código de la aplicación ────────────────────────────────────────────────────
COPY . /app/

CMD ["python", "main.py"]
