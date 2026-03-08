FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Install dependencies needed by AIORTC, PyAV, FFmpeg, and pkg-config
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

# Upgrade pip and install compatible versions of setuptools and Cython
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
RUN pip install --no-cache-dir --only-binary av av==16.0.0
#RUN pip install --no-cache-dir "cython<3.0" "setuptools<65"
RUN pip install --no-cache-dir aiohttp aiortc numpy opencv-python-headless

# Copy requirements
COPY requirements.txt /app/requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application code
COPY . /app/

CMD ["python", "main.py"]