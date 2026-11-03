FROM python:3.13-alpine

ARG VERSION=2.1.0
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Sal0 AutoStart" \
      org.opencontainers.image.description="Recuperacao automatica de conteineres com painel local para Docker e ZimaOS" \
      org.opencontainers.image.source="https://github.com/Sal0-Apps/Sal0-AutoStart" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/config \
    WEB_PORT=8788
WORKDIR /app
COPY app.py storage_guard.py index.html VERSION ./
COPY assets/icon.svg ./assets/icon.svg
RUN mkdir -p /config
EXPOSE 8788
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('WEB_PORT','8788')+'/health',timeout=4).read()"
CMD ["python", "-u", "app.py"]
