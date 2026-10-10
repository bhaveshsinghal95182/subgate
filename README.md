# subgate

Rejects Radarr/Sonarr torrents that have neither English subtitles nor English
audio, before most of the data downloads. Rejected releases are removed through the Arr queue with
blocklisting, so Radarr/Sonarr search for the next release on their own.

## How a torrent is checked

1. Torrent appears in the `radarr` or `tv-sonarr` category → tagged
   `subgate-checking`.
2. Once the magnet's file list arrives, any `.srt/.ass/.ssa/.vtt/.sub/.idx/.sup`
   file named English (`eng`, `English`, `.en.srt`) or with no language in its
   name → **pass**.
3. Otherwise only the largest non-sample video file is downloaded, sequentially
   with first/last piece priority, until its first 4 MiB are on disk.
   `ffprobe` then reads the track list:
   - a non-forced English subtitle track (`eng`, `en`, `en-*`, title "English",
     or `und` when `ACCEPT_UNDETERMINED=true`) → **pass**
   - an English audio track (dub or original; commentary tracks don't count)
     → **pass**. Untagged (`und`) audio, as in YTS mp4s, counts only when
     Radarr/Sonarr say the title's original language is English.
   - **movies only** (Radarr): a Hindi audio track (`hi`, `hin`, title "Hindi"), or
     untagged audio when the movie's original language is Hindi → **pass**
   - neither → **reject**
   - header not readable yet → keep downloading up to 16 MiB (+ last 8 MiB for
     mp4/m4v/mov), then reject as "unreadable header".
4. Pass → original file priorities and download flags are restored, tag
   `subgate-ok`, download continues normally.
5. Reject → Arr queue entry removed with `blocklist=true`, tag
   `subgate-rejected`. If the Arr doesn't know the torrent after
   `QUEUE_GRACE_MINUTES`, the torrent is just stopped.

Torrents already complete when subgate first sees them get `subgate-skipped`
and are left alone. A torrent that can't reach a verdict within
`PROBE_TIMEOUT_MINUTES` of *active* download time (queued/stopped time doesn't
count) is rejected as "timeout".

If no release of a title has English subs, the Arr works through every
candidate and the title stays missing.

## Logs

    ./media.sh logs subgate

    2026-10-04 12:00:00 [abcd1234] Some.Movie.2024.1080p...: checking for English subtitles
    2026-10-04 12:00:45 [abcd1234] Some.Movie.2024.1080p...: PASS: embedded subtitles: eng, spa
    2026-10-04 12:03:10 [ef567890] Other.Movie.2023...: REJECT: found subtitles: tur (none usable English)
    2026-10-04 12:03:10 [ef567890] Other.Movie.2023...: removed via Radarr (blocklisted, searching for next release)

Blocklisted releases show up in Radarr/Sonarr under Activity → Blocklist. To
let one through anyway, remove it there and grab it manually, then add the tag
`subgate-ok` to the torrent in qBittorrent before subgate checks it (or remove
`subgate-rejected` from a stopped one and tag it `subgate-ok`).

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `QBIT_URL` | `http://qbittorrent:8080` | |
| `QBIT_USERNAME` / `QBIT_PASSWORD` | required | qBittorrent WebUI login |
| `RADARR_URL` / `RADARR_API_KEY` | `http://radarr:7878` / required | |
| `SONARR_URL` / `SONARR_API_KEY` | `http://sonarr:8989` / required | |
| `CATEGORY_RADARR` / `CATEGORY_SONARR` | `radarr` / `tv-sonarr` | qBittorrent categories to watch |
| `POLL_SECONDS` | `15` | |
| `STATE_FILE` | `/state/subgate.json` | |
| `MIN_HEAD_MIB` / `HEAD_MIB` / `TAIL_MIB` | `4` / `16` / `8` | bytes fetched before probing |
| `PROBE_TIMEOUT_MINUTES` | `120` | active download time allowed for a verdict |
| `QUEUE_GRACE_MINUTES` | `5` | wait for the Arr queue before just stopping |
| `ACCEPT_UNDETERMINED` | `true` | count untagged (`und`) subtitle tracks as English |
| `DRY_RUN` | `false` | log verdicts and tag `subgate-would-reject`, never remove |

CLI: `python subgate.py --once --dry-run` runs one pass without removing anything.

Tests: `python3 -m unittest -v test_subgate`.
