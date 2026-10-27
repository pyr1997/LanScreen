FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LANSCREEN_DATA=/data HOME=/data
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-impress fonts-noto-cjk fonts-dejavu-core ffmpeg fontconfig \
    && useradd --uid 10001 --create-home --home-dir /home/lanscreen lanscreen \
    && mkdir -p /app /data && chown 10001:10001 /app /data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py render_worker.py conversion_runtime.py ./
COPY web ./web
RUN chmod -R a+rX /app
USER 10001:10001
EXPOSE 8765 18765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8765') + '/api/health', timeout=3)"
CMD ["python", "app.py"]
