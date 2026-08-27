# ============================================================
# Nightshift – multi-source music downloader
# Bundles: Python, ffmpeg, yt-dlp, deno, spotDL, beets
# ============================================================
FROM python:3.12-slim

ARG TARGETARCH=amd64

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl unzip gosu tzdata ca-certificates \
        libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# deno – solves YouTube's JS challenges for yt-dlp.
# Baked into the image: prevents the "Requested format is not available"
# class of errors caused by deno missing from PATH.
RUN case "$TARGETARCH" in \
        arm64) DENO_ARCH="aarch64-unknown-linux-gnu" ;; \
        *)     DENO_ARCH="x86_64-unknown-linux-gnu" ;; \
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

# spotDL writes the explicit flag only for M4A, into the rtng atom; its MP3 tag
# table maps "explicit" to "NULL", so an MP3 library never carries it and no
# media server can show it. Navidrome reads TXXX:ITUNESADVISORY for MP3, which
# is what this adds.
#
# Only the explicit case is written. The tag's "2" means clean - the bowdlerised
# edit of a track that also exists uncensored - and not "contains nothing
# offensive", which is all Spotify's explicit: false actually says. An
# instrumental is not a clean version of anything, so it gets no tag at all.
# A no-op once upstream fills the gap itself.
RUN python - <<'PATCH'
import pathlib
from spotdl.utils import metadata

path = pathlib.Path(metadata.__file__)
source = path.read_text()
needle = (
    '    elif encoding == "mp3":\n'
    '        audio_file["tracknumber"] = '
    'f"{str(song.track_number)}/{str(song.tracks_count)}"'
)
replacement = (
    '    elif encoding == "mp3":\n'
    '        if song.explicit is True:\n'
    '            EasyID3.RegisterTXXXKey("explicit", "ITUNESADVISORY")\n'
    '            audio_file["explicit"] = "1"\n'
    '        audio_file["tracknumber"] = '
    'f"{str(song.track_number)}/{str(song.tracks_count)}"'
)
if needle in source and "ITUNESADVISORY" not in source:
    source = source.replace(needle, replacement, 1)
    source = source.replace(
        "from mutagen.id3 import ID3",
        "from mutagen.easyid3 import EasyID3\nfrom mutagen.id3 import ID3", 1)
    path.write_text(source)
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
