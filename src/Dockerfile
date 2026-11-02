# A multi-arch base and pure-Python or wheel-only requirements: the same file builds
# linux/amd64 and linux/arm64 (Raspberry Pi 4 and 5), natively or with buildx.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="travelarr" \
      org.opencontainers.image.description="Copy original files from your home Plex server to a travel NAS" \
      org.opencontainers.image.source="https://github.com/alien4u/travelarr" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    TRAVELARR_DATA=/config \
    TRAVELARR_MEDIA=/media \
    TRAVELARR_PORT=8484

WORKDIR /opt/travelarr
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Unraid's nobody:users, so copied files are owned like everything else on
# the shares and stay editable over SMB.
USER 99:100
EXPOSE 8484
CMD ["python", "-m", "app"]
