#!/usr/bin/env python3
"""subgate: reject Radarr/Sonarr torrents with neither English subtitles nor English audio.

For every new torrent in the Radarr/Sonarr qBittorrent categories it looks at
the file list (external .srt/.ass/...) and, failing that, downloads only the
head of the main video file and reads its track list with ffprobe. Releases
without English subtitles are removed through the Arr queue with blocklisting,
which makes Radarr/Sonarr search for the next release.

State lives in qBittorrent tags (visible in its UI) plus a small JSON file.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import tempfile
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TAG_CHECKING = "subgate-checking"
TAG_OK = "subgate-ok"
TAG_REJECTED = "subgate-rejected"
TAG_SKIPPED = "subgate-skipped"
TAG_WOULD_REJECT = "subgate-would-reject"
ALL_TAGS = [TAG_CHECKING, TAG_OK, TAG_REJECTED, TAG_SKIPPED, TAG_WOULD_REJECT]
DONE_TAGS = {TAG_OK, TAG_REJECTED, TAG_SKIPPED, TAG_WOULD_REJECT}

SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".webm", ".mov",
              ".wmv", ".mpg", ".mpeg"}
MP4_FAMILY = {".mp4", ".m4v", ".mov"}
ACTIVE_STATES = {"downloading", "stalledDL", "metaDL", "forcedDL", "forcedMetaDL"}

ENGLISH_TOKENS = {"en", "eng", "english"}
FORCED_TOKENS = {"forced", "signs", "songs"}
# mkvmerge records how many lines a subtitle track has. Signs/foreign-parts
# tracks have a few dozen; full tracks have hundreds to thousands. Some groups
# flag the only, full English track "forced" so it shows by default.
FULL_SUB_MIN_EVENTS = 300

# Language names and 3-letter codes: unambiguous anywhere in a path.
OTHER_LANG_LONG = {
    "spanish", "spa", "latino", "castellano", "french", "fre", "fra", "german",
    "ger", "deu", "italian", "ita", "portuguese", "por", "brazilian",
    "russian", "rus", "chinese", "chi", "zho", "chs", "cht", "japanese", "jpn",
    "korean", "kor", "arabic", "hindi", "hin", "turkish", "tur", "dutch",
    "dut", "nld", "polish", "pol", "swedish", "swe", "danish", "norwegian",
    "nor", "nob", "finnish", "greek", "gre", "hebrew", "heb", "czech", "cze",
    "ces", "hungarian", "hun", "romanian", "rum", "ron", "bulgarian", "bul",
    "croatian", "hrv", "serbian", "srp", "slovenian", "slv", "slovak", "slk",
    "ukrainian", "ukr", "vietnamese", "vie", "thai", "indonesian", "malay",
    "msa", "persian", "fas", "tamil", "telugu", "bengali", "urdu", "urd",
    "malayalam", "kannada", "marathi",
}
# 2-letter codes collide with words in release names ("The.Den" -> "de"),
# so they only count as the final token before the extension.
OTHER_LANG_SHORT = {
    "es", "fr", "de", "it", "pt", "br", "ru", "zh", "ja", "jp", "ko", "ar",
    "hi", "tr", "nl", "pl", "sv", "da", "no", "fi", "el", "he", "cs", "hu",
    "ro", "bg", "hr", "sr", "sl", "sk", "uk", "vi", "th", "id", "ms", "fa",
    "ta", "te", "bn", "ur", "ml", "kn",
}

MIB = 1024 * 1024


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z]+", (text or "").lower())


def _classify_tokens(tokens: list[str], allow_short: bool) -> str | None:
    if "forced" in tokens:
        return "forced"
    # Trailing tokens are the language tag in "Movie.2019.eng.srt" / "3_Spanish";
    # scan from the end so a title word ("The.English.Patient.spa") loses.
    for t in reversed(tokens):
        if t in ENGLISH_TOKENS:
            return "english"
        if t in OTHER_LANG_LONG:
            return "other"
    if allow_short and tokens and tokens[-1] in OTHER_LANG_SHORT:
        return "other"
    return None


def classify_sub_path(path: str) -> str:
    """Return "english", "other", "forced" or "unknown" for a subtitle path.

    The file name decides first. Folder names only count below the torrent's
    root folder (e.g. "Subs/Show.S01E01/"), because the root folder is the
    release title and its words are not language tags.
    """
    parts = path.lower().replace("\\", "/").split("/")
    base = os.path.splitext(parts[-1])[0]
    verdict = _classify_tokens(_tokens(base), allow_short=True)
    if verdict:
        return verdict
    for folder in reversed(parts[1:-1]):
        verdict = _classify_tokens(_tokens(folder), allow_short=False)
        if verdict:
            return verdict
    return "unknown"


def sub_counts_as_english(path: str) -> bool:
    return classify_sub_path(path) in ("english", "unknown")


def pick_target_file(files: list[dict]) -> dict | None:
    """Largest video file that is not a sample."""
    videos = [f for f in files
              if _ext(f["name"]) in VIDEO_EXTS and "sample" not in _tokens(f["name"])]
    if not videos:
        return None
    return max(videos, key=lambda f: f.get("size", 0))


def file_offset(files: list[dict], target: dict) -> int:
    """Byte offset of target inside the torrent (files are in torrent order)."""
    return sum(f["size"] for f in files if f["index"] < target["index"])


def contiguous_head_bytes(offset: int, size: int, piece_size: int,
                          pieces: list[int]) -> int:
    """How many bytes from the start of the file are downloaded without a gap."""
    first = offset // piece_size
    i = first
    while i < len(pieces) and pieces[i] == 2:
        i += 1
    covered_end = i * piece_size  # torrent byte offset where the first gap starts
    return max(0, min(size, covered_end - offset))


def tail_pieces(piece_range, piece_size: int, n_bytes: int) -> list[int]:
    first, last = piece_range
    count = max(1, -(-n_bytes // piece_size))
    return list(range(max(first, last - count + 1), last + 1))


def resolve_paths(progress: float, save_path: str, download_path: str,
                  rel_name: str) -> list[str]:
    bases = []
    if progress < 1 and download_path:
        bases = [download_path, save_path]
    else:
        bases = [save_path, download_path]
    out = []
    for base in bases:
        if not base:
            continue
        p = os.path.join(base, rel_name)
        if p not in out:
            out.append(p)
    return out + [p + ".!qB" for p in out]


def is_active_state(state: str) -> bool:
    return state in ACTIVE_STATES


def _sub_events(stream: dict) -> int | None:
    for k, v in (stream.get("tags") or {}).items():
        if k.upper().split("-")[0] == "NUMBER_OF_FRAMES":
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    return None


def _stream_is_forced(stream: dict) -> bool:
    title = (stream.get("tags") or {}).get("title", "")
    if any(t in FORCED_TOKENS for t in _tokens(title)):
        return True
    if (stream.get("disposition") or {}).get("forced") == 1:
        events = _sub_events(stream)
        return events is None or events < FULL_SUB_MIN_EVENTS
    return False


def _stream_is_english(stream: dict, accept_undetermined: bool) -> bool:
    tags = stream.get("tags") or {}
    lang = (tags.get("language") or "").lower().strip()
    title_tokens = _tokens(tags.get("title", ""))
    if lang in ("en", "eng") or lang.startswith("en-"):
        return True
    if any(t in ("english", "eng") for t in title_tokens):
        return True
    if accept_undetermined and lang in ("", "und"):
        names_other = any(t in OTHER_LANG_LONG for t in title_tokens)
        return not names_other
    return False


def _lang(stream: dict) -> str:
    return ((stream.get("tags") or {}).get("language") or "und").lower()


def _audio_is_commentary(stream: dict) -> bool:
    if (stream.get("disposition") or {}).get("comment") == 1:
        return True
    title = (stream.get("tags") or {}).get("title", "")
    return any(t in ("commentary", "commentaries") for t in _tokens(title))


def _stream_is_hindi(stream: dict) -> bool:
    tags = stream.get("tags") or {}
    lang = (tags.get("language") or "").lower().strip()
    return (lang in ("hi", "hin") or lang.startswith("hi-")
            or "hindi" in _tokens(tags.get("title", "")))


def _audio_is_english(stream: dict, original_english: bool, hindi_ok: bool = False) -> bool:
    # Untagged audio (YTS mp4s are all "und") is only trusted as English when
    # the movie/series was originally made in English.
    if _stream_is_english(stream, accept_undetermined=False):
        return True
    if hindi_ok and _stream_is_hindi(stream):
        return True
    title_tokens = _tokens((stream.get("tags") or {}).get("title", ""))
    return (original_english and _lang(stream) == "und"
            and not any(t in OTHER_LANG_LONG for t in title_tokens))


def decide_streams(probe, accept_undetermined: bool,
                   original_english: bool = False,
                   hindi_ok: bool = False) -> tuple[str, str]:
    """Return ("pass"|"fail"|"inconclusive", details) for ffprobe JSON output.

    Passes on a full English subtitle track or an English audio track
    (Hindi audio too when hindi_ok, which the caller sets for movies).
    """
    if isinstance(probe, (str, bytes)):
        try:
            probe = json.loads(probe)
        except ValueError:
            return "inconclusive", "invalid ffprobe output"
    if not isinstance(probe, dict):
        return "inconclusive", "invalid ffprobe output"
    streams = probe.get("streams") or []
    if not any(s.get("codec_type") == "video" for s in streams):
        return "inconclusive", "no video stream in header"
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    counted = [s for s in subs
               if not _stream_is_forced(s) and _stream_is_english(s, accept_undetermined)]
    if counted:
        return "pass", "English subtitles: " + ", ".join(_lang(s) for s in counted)
    eng_audio = [s for s in audio
                 if not _audio_is_commentary(s)
                 and _audio_is_english(s, original_english, hindi_ok)]
    if eng_audio:
        langs = ", ".join(_lang(s) for s in eng_audio)
        if all(_lang(s) == "und" for s in eng_audio):
            return "pass", f"untagged audio ({langs}; accepted from original language)"
        if hindi_ok and any(_stream_is_hindi(s) for s in eng_audio):
            return "pass", "English/Hindi audio: " + langs
        return "pass", "English audio: " + langs
    sub_desc = ", ".join(_lang(s) + (" forced" if _stream_is_forced(s) else "")
                         for s in subs) or "none"
    audio_desc = ", ".join(_lang(s) for s in audio) or "none"
    return "fail", f"no English subs or audio (subtitles: {sub_desc}; audio: {audio_desc})"


def needs_original_language(probe) -> bool:
    """True when untagged audio could flip a fail into a pass."""
    try:
        streams = (json.loads(probe) if isinstance(probe, (str, bytes)) else probe)["streams"]
    except (ValueError, KeyError, TypeError):
        return False
    return any(s.get("codec_type") == "audio" and _lang(s) == "und"
               and not _audio_is_commentary(s) for s in streams)


# --------------------------------------------------------------------------
# Config / logging
# --------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_num(name: str, default: float) -> float:
    v = os.environ.get(name)
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


class Config:
    def __init__(self, dry_run_flag: bool = False):
        missing = [n for n in ("QBIT_USERNAME", "QBIT_PASSWORD", "RADARR_API_KEY",
                               "SONARR_API_KEY") if not os.environ.get(n)]
        if missing:
            sys.exit("subgate: missing required env: " + ", ".join(missing))
        self.qbit_url = os.environ.get("QBIT_URL", "http://qbittorrent:8080").rstrip("/")
        self.qbit_user = os.environ["QBIT_USERNAME"]
        self.qbit_pass = os.environ["QBIT_PASSWORD"]
        self.apps = {
            os.environ.get("CATEGORY_RADARR", "radarr"): (
                "Radarr", os.environ.get("RADARR_URL", "http://radarr:7878").rstrip("/"),
                os.environ["RADARR_API_KEY"]),
            os.environ.get("CATEGORY_SONARR", "tv-sonarr"): (
                "Sonarr", os.environ.get("SONARR_URL", "http://sonarr:8989").rstrip("/"),
                os.environ["SONARR_API_KEY"]),
        }
        self.poll = _env_num("POLL_SECONDS", 15)
        self.state_file = os.environ.get("STATE_FILE", "/state/subgate.json")
        self.head_bytes = int(_env_num("HEAD_MIB", 16) * MIB)
        self.min_head_bytes = int(_env_num("MIN_HEAD_MIB", 4) * MIB)
        self.tail_bytes = int(_env_num("TAIL_MIB", 8) * MIB)
        self.timeout = _env_num("PROBE_TIMEOUT_MINUTES", 120) * 60
        self.queue_grace = _env_num("QUEUE_GRACE_MINUTES", 5) * 60
        self.accept_und = _env_bool("ACCEPT_UNDETERMINED", True)
        self.dry_run = dry_run_flag or _env_bool("DRY_RUN", False)


def log(h: str | None, name: str | None, msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    if h:
        print(f"{ts} [{h[:8]}] {name}: {msg}", flush=True)
    else:
        print(f"{ts} {msg}", flush=True)


# --------------------------------------------------------------------------
# HTTP clients
# --------------------------------------------------------------------------

class LoginRejected(Exception):
    pass


class QBit:
    LOGIN_BACKOFF = 600

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.logged_in = False
        self.backoff_until = 0.0

    def _raw(self, method: str, path: str, data: dict | None = None):
        url = self.cfg.qbit_url + "/api/v2/" + path
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={"Referer": self.cfg.qbit_url})
        with self.opener.open(req, timeout=30) as r:
            return r.status, r.read()

    def login(self) -> None:
        if time.time() < self.backoff_until:
            raise LoginRejected("login backoff active")
        try:
            status, body = self._raw("POST", "auth/login",
                                     {"username": self.cfg.qbit_user,
                                      "password": self.cfg.qbit_pass})
        except urllib.error.HTTPError as e:
            status, body = e.code, b""
        if status not in (200, 204) or body.strip() == b"Fails.":
            self.backoff_until = time.time() + self.LOGIN_BACKOFF
            raise LoginRejected(f"qBittorrent login rejected (HTTP {status}); "
                                f"backing off {self.LOGIN_BACKOFF // 60} min")
        self.logged_in = True

    def call(self, path: str, data: dict | None = None, method: str | None = None):
        method = method or ("POST" if data is not None else "GET")
        if not self.logged_in:
            self.login()
        try:
            _, body = self._raw(method, path, data)
        except urllib.error.HTTPError as e:
            if e.code != 403:
                raise
            self.logged_in = False
            self.login()
            _, body = self._raw(method, path, data)
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return body.decode(errors="replace")

    def get(self, path: str, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self.call(path + q, method="GET")

    def post(self, path: str, **data):
        return self.call(path, data=data, method="POST")


def arr_request(base: str, key: str, method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"X-Api-Key": key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Gate
# --------------------------------------------------------------------------

class Gate:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.qb = QBit(cfg)
        self.state = load_state(cfg.state_file)
        self.tags_created = False

    # -- qBittorrent helpers ------------------------------------------------

    def _ensure_tags(self) -> None:
        if not self.tags_created:
            self.qb.post("torrents/createTags", tags=",".join(ALL_TAGS))
            self.tags_created = True

    def _set_tag(self, h: str, tag: str) -> None:
        others = [t for t in ALL_TAGS if t != tag]
        self.qb.post("torrents/removeTags", hashes=h, tags=",".join(others))
        self.qb.post("torrents/addTags", hashes=h, tags=tag)

    def _restore(self, t: dict, st: dict) -> None:
        h = t["hash"]
        orig = st.get("orig_priorities") or {}
        by_prio: dict[int, list[str]] = {}
        for idx, prio in orig.items():
            by_prio.setdefault(int(prio), []).append(str(idx))
        for prio, ids in by_prio.items():
            self.qb.post("torrents/filePrio", hash=h, id="|".join(ids), priority=prio)
        info = self._info(h) or t
        if st.get("toggled_seq") and info.get("seq_dl"):
            self.qb.post("torrents/toggleSequentialDownload", hashes=h)
        if st.get("toggled_flp") and info.get("f_l_piece_prio"):
            self.qb.post("torrents/toggleFirstLastPiecePrio", hashes=h)
        st["orig_priorities"] = {}
        st["toggled_seq"] = st["toggled_flp"] = False

    def _info(self, h: str) -> dict | None:
        res = self.qb.get("torrents/info", hashes=h)
        return res[0] if res else None

    def _prepare(self, t: dict, st: dict, files: list[dict], target: dict) -> None:
        h = t["hash"]
        if not st.get("orig_priorities"):
            st["orig_priorities"] = {str(f["index"]): f["priority"] for f in files}
        zero = [str(f["index"]) for f in files
                if f["index"] != target["index"] and f["priority"] != 0]
        if zero:
            self.qb.post("torrents/filePrio", hash=h, id="|".join(zero), priority=0)
        if target["priority"] == 0:
            self.qb.post("torrents/filePrio", hash=h, id=str(target["index"]), priority=1)
        if not t.get("seq_dl"):
            self.qb.post("torrents/toggleSequentialDownload", hashes=h)
            st["toggled_seq"] = True
        if not t.get("f_l_piece_prio"):
            self.qb.post("torrents/toggleFirstLastPiecePrio", hashes=h)
            st["toggled_flp"] = True

    # -- verdicts -------------------------------------------------------------

    def _queue_records(self, t: dict) -> list[dict]:
        app, base, key = self.cfg.apps[t["category"]]
        # Include downloads the Arr can't match by name (common for anime); the default
        # queue hides them, and a rejected one would only be stopped, not blocklisted.
        unknown = "includeUnknownMovieItems" if app == "Radarr" else "includeUnknownSeriesItems"
        queue = arr_request(base, key, "GET", f"/api/v3/queue?page=1&pageSize=1000&{unknown}=true")
        return [r for r in (queue or {}).get("records", [])
                if (r.get("downloadId") or "").upper() == t["hash"].upper()]

    def _original_language(self, t: dict, st: dict) -> str | None:
        """Original language of the movie/series, e.g. "English"; None if unknown yet."""
        if st.get("orig_lang"):
            return st["orig_lang"]
        _, base, key = self.cfg.apps[t["category"]]
        for r in self._queue_records(t):
            if r.get("movieId"):
                item = arr_request(base, key, "GET", f"/api/v3/movie/{r['movieId']}")
            elif r.get("seriesId"):
                item = arr_request(base, key, "GET", f"/api/v3/series/{r['seriesId']}")
            else:
                continue
            name = ((item or {}).get("originalLanguage") or {}).get("name")
            if name:
                st["orig_lang"] = name
                return name
        return None

    def _pass(self, t: dict, st: dict, reason: str) -> None:
        self._restore(t, st)
        self._set_tag(t["hash"], TAG_OK)
        st["done"] = True
        log(t["hash"], t["name"], "PASS: " + reason)

    def _reject(self, t: dict, st: dict, reason: str) -> None:
        h, name = t["hash"], t["name"]
        if self.cfg.dry_run:
            self._restore(t, st)
            self._set_tag(h, TAG_WOULD_REJECT)
            st["done"] = True
            log(h, name, "WOULD REJECT (dry run): " + reason)
            return
        app, base, key = self.cfg.apps[t["category"]]
        if st.get("reason") != reason:
            log(h, name, f"REJECT: {reason}")
        st["reason"] = reason
        ids = [r["id"] for r in self._queue_records(t)]
        if ids:
            arr_request(base, key, "DELETE",
                        "/api/v3/queue/bulk?removeFromClient=true&blocklist=true"
                        "&skipRedownload=false", {"ids": ids})
            self._set_tag(h, TAG_REJECTED)
            st["done"] = True
            log(h, name, f"removed via {app} (blocklisted, searching for next release)")
            return
        now = time.time()
        if not st.get("reject_since"):
            st["reject_since"] = now
        if now - st["reject_since"] >= self.cfg.queue_grace:
            self.qb.post("torrents/stop", hashes=h)
            self._set_tag(h, TAG_REJECTED)
            st["done"] = True
            log(h, name, f"not in {app} queue; stopped torrent")

    # -- per torrent ----------------------------------------------------------

    def _probe_head(self, path: str, n_bytes: int):
        """Probe a temp copy of the file's first n_bytes.

        On the real, mostly-empty file ffprobe goes hunting through gigabytes of
        zeros for the index and times out; a short copy ends cleanly at EOF.
        """
        with tempfile.NamedTemporaryFile(suffix=_ext(path)) as tmp:
            with open(path, "rb") as src:
                remaining = n_bytes
                while remaining > 0:
                    chunk = src.read(min(remaining, MIB))
                    if not chunk:
                        break
                    tmp.write(chunk)
                    remaining -= len(chunk)
            tmp.flush()
            return self._probe(tmp.name)

    def _probe(self, path: str):
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-probesize", "32M", "-analyzeduration", "0",
                 "-show_entries",
                 "stream=index,codec_type,codec_name:stream_tags"
                 ":stream_disposition=forced,comment",
                 "-of", "json", path],
                capture_output=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as e:
            return f"ffprobe failed: {e}"
        return r.stdout.decode(errors="replace")

    def handle(self, t: dict) -> None:
        h, name = t["hash"], t["name"]
        tags = {x.strip() for x in (t.get("tags") or "").split(",") if x.strip()}
        if tags & DONE_TAGS:
            return
        st = self.state.setdefault(h, {"active_seconds": 0, "orig_priorities": {},
                                       "toggled_seq": False, "toggled_flp": False,
                                       "reject_since": None, "reason": None})
        if st.get("done"):
            return
        if TAG_CHECKING not in tags:
            if t.get("progress", 0) >= 1:
                self._set_tag(h, TAG_SKIPPED)
                st["done"] = True
                return
            self._set_tag(h, TAG_CHECKING)
            log(h, name, "checking for English subtitles or audio")

        if st.get("reason"):  # verdict already reached, waiting for the Arr queue
            self._reject(t, st, st["reason"])
            return

        if is_active_state(t.get("state", "")):
            st["active_seconds"] = st.get("active_seconds", 0) + self.cfg.poll
        timed_out = st["active_seconds"] > self.cfg.timeout

        if not t.get("has_metadata"):
            if timed_out:
                self._reject(t, st, "no metadata (timeout)")
            return

        files = self.qb.get("torrents/files", hash=h) or []
        for f in files:
            if _ext(f["name"]) in SUB_EXTS and sub_counts_as_english(f["name"]):
                self._pass(t, st, "external sub " + f["name"])
                return

        target = pick_target_file(files)
        if target is None:
            self._reject(t, st, "no video file")
            return

        self._prepare(t, st, files, target)

        props = self.qb.get("torrents/properties", hash=h) or {}
        piece_size = props.get("piece_size") or 0
        pieces = self.qb.get("torrents/pieceStates", hash=h) or []
        if not piece_size or not pieces:
            if timed_out:
                self._reject(t, st, "timeout")
            return

        size = target["size"]
        have = contiguous_head_bytes(file_offset(files, target), size, piece_size, pieces)
        if have < min(self.cfg.min_head_bytes, size):
            if timed_out:
                self._reject(t, st, "timeout before header downloaded")
            return
        mp4 = _ext(target["name"]) in MP4_FAMILY
        tail = tail_pieces(target["piece_range"], piece_size, self.cfg.tail_bytes)
        tail_done = all(i < len(pieces) and pieces[i] == 2 for i in tail)

        paths = resolve_paths(t.get("progress", 0), props.get("save_path") or t.get("save_path", ""),
                              props.get("download_path") or t.get("download_path", ""),
                              target["name"])
        path = next((p for p in paths if os.path.exists(p)), None)
        if path is None:
            if timed_out:
                self._reject(t, st, "timeout (file not found on disk)")
            return

        # MP4 keeps its track list (moov) at either end and ffprobe seeks to it
        # by atom size, so it reads the real file; everything else gets a head copy.
        head = min(have, max(self.cfg.head_bytes, 32 * MIB))
        out = self._probe(path) if mp4 else self._probe_head(path, head)
        hindi_ok = self.cfg.apps[t["category"]][0] == "Radarr"  # Hindi audio is fine for movies
        verdict, details = decide_streams(out, self.cfg.accept_und, hindi_ok=hindi_ok)
        if verdict == "fail" and needs_original_language(out):
            lang = self._original_language(t, st)
            if lang is None:
                # The Arr queue can lag the grab by a minute; wait before
                # deciding untagged audio isn't English.
                st["lang_wait_since"] = st.get("lang_wait_since") or time.time()
                if time.time() - st["lang_wait_since"] < self.cfg.queue_grace:
                    return
            verdict, details = decide_streams(out, self.cfg.accept_und,
                                              original_english=(lang == "English"
                                                               or (hindi_ok and lang == "Hindi")),
                                              hindi_ok=hindi_ok)
        if verdict == "pass":
            self._pass(t, st, details)
        elif verdict == "fail":
            self._reject(t, st, details)
        else:
            enough = have >= min(self.cfg.head_bytes, size) and (tail_done or not mp4)
            if enough:
                self._reject(t, st, "unreadable header (" + details + ")")
            elif timed_out:
                self._reject(t, st, "timeout (" + details + ")")

    # -- loop -----------------------------------------------------------------

    def poll_once(self) -> None:
        try:
            self._ensure_tags()
        except LoginRejected as e:
            log(None, None, str(e))
            return
        except (urllib.error.URLError, OSError) as e:
            log(None, None, f"qBittorrent unreachable: {e}")
            return
        seen = set()
        for category in self.cfg.apps:
            try:
                torrents = self.qb.get("torrents/info", category=category) or []
            except (LoginRejected, urllib.error.URLError, OSError) as e:
                log(None, None, f"listing {category} failed: {e}")
                return
            for t in torrents:
                seen.add(t["hash"])
                try:
                    self.handle(t)
                except Exception as e:  # one bad torrent must not stop the loop
                    log(t.get("hash"), t.get("name"), f"error: {type(e).__name__}: {e}")
        for h in list(self.state):
            if h not in seen:
                del self.state[h]
        try:
            save_state(self.cfg.state_file, self.state)
        except OSError as e:
            log(None, None, f"could not save state: {e}")

    def run(self, once: bool) -> None:
        log(None, None, f"subgate started (dry_run={self.cfg.dry_run}, "
                        f"categories={', '.join(self.cfg.apps)})")
        while True:
            self.poll_once()
            if once:
                self.unwind()
                return
            time.sleep(self.cfg.poll)

    def unwind(self) -> None:
        """Undo changes on torrents still undecided (used by --once)."""
        for h, st in self.state.items():
            if st.get("done"):
                continue
            t = self._info(h)
            if not t:
                continue
            self._restore(t, st)
            self.qb.post("torrents/removeTags", hashes=h, tags=TAG_CHECKING)
            log(h, t["name"], "undecided; restored and untagged")
        save_state(self.cfg.state_file, self.state)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    ap.add_argument("--dry-run", action="store_true", help="log verdicts, never remove")
    args = ap.parse_args()
    Gate(Config(dry_run_flag=args.dry_run)).run(once=args.once)


if __name__ == "__main__":
    main()
