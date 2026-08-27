"""Writes a working beets configuration on first start.

Beets without a configuration file is not beets without an opinion: it falls
back to its own defaults, and those are wrong for a library that already lives
somewhere. ``directory: ~/Music`` plus ``copy: yes`` makes it copy every
imported track into the container and tag the copy, so the real file never
changes and the copies disappear with the next image update. ``quiet: no``
makes it ask what to do with an album it already knows, and a nightly job has
no terminal to answer with.

None of that is visible from the outside – the log says the import failed, or
says nothing at all – so the file is written once, on first start, rather than
left to the user to discover.

Both files are only ever created, never overwritten: they are the user's from
the moment they exist.
"""
from __future__ import annotations

from pathlib import Path

from .config import cfg

BEETS_DIR = Path("/config/beets")

# Broad enough to describe a library, narrow enough to keep last.fm's tag soup
# ("seen live", "favourites", an artist's own name) out of the genre field.
GENRES = """Indie
Indie Pop
Indie Rock
Indie Folk
Folk
Singer-Songwriter
Acoustic
Country
Americana
Hip-Hop
Rap
Trap
R&B
Soul
Funk
Disco
Electronic
Electro
House
Deep House
Tech House
Techno
Hardtechno
Schranz
Hardgroove
Trance
Progressive House
Drum and Bass
Liquid Drum and Bass
Jungle
Dubstep
Garage
UK Garage
Hardstyle
Hardcore
Hard Dance
Bounce
Future Bass
Synthwave
Lo-Fi
Chillout
Ambient
Downtempo
Trip-Hop
Alternative
Alternative Rock
Punk
Pop Punk
Post-Punk
Metal
Heavy Metal
Death Metal
Black Metal
Thrash Metal
Hardcore Punk
Emo
Grunge
Jazz
Smooth Jazz
Blues
Reggae
Dancehall
Ska
Classical
Orchestral
Soundtrack
Score
World
Latin
Reggaeton
Salsa
EDM
Dance
Pop
Rock
"""

CONFIG = """# Beets, as Nightshift needs it. Created on first start; yours from now on.

# The library is where the media server expects it, and beets tags in place.
# Never set copy or move to yes here: that would duplicate the library inside
# the container and leave the real files untagged.
directory: {music_root}
library: /config/beets/library.db

plugins: lastgenre fetchart embedart

import:
  copy: no
  move: no
  write: yes
  # The downloaders have the metadata already; beets is here to tidy, not to
  # re-identify. Set to yes if you want MusicBrainz matching.
  autotag: no
  # There is no terminal behind the nightly job, so beets must never ask.
  quiet: yes
  # Album folders grow: a track is added to an album imported on an earlier
  # night. Without this beets asks what to do with the duplicate and the run
  # ends with an import error.
  duplicate_action: merge

lastgenre:
  auto: yes
  # album, not artist: last.fm knows most artists by one broad tag only.
  source: album
  count: 1
  # Rather no genre than a wrong one.
  fallback: ''
  force: yes
  whitelist: /config/beets/genres.txt
  min_weight: 10
  title_case: yes

fetchart:
  auto: yes
  minwidth: 500
  enforce_ratio: no
  cover_names: cover front folder

embedart:
  auto: yes
  remove_art_file: no
"""


def ensure() -> None:
    """Creates the beets configuration and genre whitelist if they are absent."""
    try:
        BEETS_DIR.mkdir(parents=True, exist_ok=True)
        genres = BEETS_DIR / "genres.txt"
        if not genres.exists():
            genres.write_text(GENRES, encoding="utf-8")
        config = BEETS_DIR / "config.yaml"
        if not config.exists():
            config.write_text(
                CONFIG.format(music_root=cfg.library.music_root), encoding="utf-8")
    except OSError as error:
        # A read-only or missing /config is the user's to fix; it must not stop
        # the server from starting and saying so in its own log.
        print(f"beets configuration could not be written: {error}")
