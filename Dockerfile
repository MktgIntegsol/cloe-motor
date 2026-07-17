FROM python:3.12-slim

# ffmpeg + libreoffice (PPTX->imagen) + fuentes
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libreoffice fonts-dejavu poppler-utils ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY engine.py worker.py ./

# El worker es un proceso de fondo (no expone puerto)
CMD ["python", "-u", "worker.py"]
