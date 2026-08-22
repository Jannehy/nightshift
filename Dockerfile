# ============================================================
# Nightshift – multi-source music downloader
# Bundles: Python, ffmpeg, yt-dlp, deno, spotDL, beets
# ============================================================
FROM python:3.12-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl unzip gosu tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# deno – solves YouTube's JS challenges for yt-dlp.
# Baked into the image: prevents the "Requested format is not available"
# class of errors caused by deno missing from PATH.
# The architecture comes from the machine this step runs on, not from a build
# argument: a default on ARG TARGETARCH masks the value BuildKit provides, and
# the arm64 leg then quietly pulls the x86 binary.
RUN set -eux; \
    case "$(uname -m)" in \
        aarch64) DENO_ARCH="aarch64-unknown-linux-gnu" ;; \
        x86_64)  DENO_ARCH="x86_64-unknown-linux-gnu" ;; \
        *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip" -o /tmp/deno.zip \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && deno --version

# Python dependencies – ONE yt-dlp for both Nightshift AND spotDL
# (prevents version drift between multiple binaries)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# spotDL 4.5.2 hardcodes YTMusic(language="de"). With ytmusicapi 1.12 the German
# locale returns zero results for the "songs" filter, so every search burns three
# retries, logs "YouTube Music returned no usable results" and settles for a
# video result instead of the song. Removing the language halves the time per
# track. The patch is located through the installed module rather than a fixed
# path, and is a no-op once upstream fixes this.
RUN python - <<'PATCH'
import pathlib
from spotdl.providers.audio import ytmusic

path = pathlib.Path(ytmusic.__file__)
source = path.read_text()
needle = 'YTMusic(language="de")'
if needle in source:
    path.write_text(source.replace(needle, "YTMusic()"))
    print(f"patched {path}")
else:
    print(f"no patch needed in {path}")
PATCH

# App
WORKDIR /app
COPY nightshift/ /app/nightshift/
COPY templates/  /app/templates/
COPY static/     /app/static/
COPY config/config.example.yaml /app/config.example.yaml
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Fixed paths inside the container
ENV NIGHTSHIFT_CONFIG=/config/config.yaml \
    BEETSDIR=/config/beets \
    PYTHONUNBUFFERED=1

VOLUME ["/config", "/music"]
EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
    CMD curl -fs http://localhost:8765/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
