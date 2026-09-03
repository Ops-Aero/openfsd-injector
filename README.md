# openfsd-injector

Plugin injector for a self-hosted [openFSD](https://github.com/renorris/openfsd) server, including the [Ops-Aero/opsaero-main](https://github.com/Ops-Aero/opsaero-main) stack.

It logs extra ATC stations onto your network and keeps them alive. The first plugin is an **ATIS bot**: one FSD connection per airport, text ATIS built from live METAR, replies to `$CQ ATIS` so pilot clients can pull the information letter.

Voice ATIS that pilots can *tune* is a separate problem — openFSD is the FSD data plane, not Audio for VATSIM. This repo can generate looping WAV/OGG from the text ATIS; playing that file on frequency is a radio sidecar (see below).

## OpsAero (opsaero-main)

`./install.sh` in opsaero-main publishes:

| Service | Bind | Use from this injector |
|---|---|---|
| FSD protocol | `127.0.0.1:6809` | `server.host` / `server.port` |
| openfsd admin + `/api/v1/fsd-jwt` | `127.0.0.1:8010` | `server.api_base` |
| Laravel website | `127.0.0.1:8000` | **not** an FSD API |

Create a **least-privilege openFSD user for the bot** — a dedicated ATIS identity (`OPENFSD_CID` / `OPENFSD_PASSWORD`), never the bootstrap administrator and never CID 1. Give it the lowest network rating the stations need: S2 (3) for tower ATIS (`facility_type: 4`), and no higher than C1 (5). Requested `auth.rating` must be ≤ the rating stored on that CID.

Startup **fails** if `auth.rating` is Administrator (12) unless you set `auth.allow_administrator` or `OPENFSD_ALLOW_ADMINISTRATOR=1`. Change any password a server install printed.

This repo ships **no credentials and no working defaults**. The injector exits at startup with a clear error if no credential is configured.

openFSD always accepts the CID password as the `#AA` token. JWT minting is optional. If you do mint, the token expires in **five minutes** and is checked only at logon — this injector mints immediately before each station connects.

Do not point `api_base` at `:8000`. That is Laravel; JWT mint will 404.

```bash
git clone https://github.com/Ops-Aero/openfsd-injector
cd openfsd-injector
cp .env.example .env      # then edit .env with your own CID + password
docker compose up -d --build
docker compose logs -f
```

That talks to host-published FSD `:6809` and openfsd API `:8010` via `host.docker.internal`. `.env` is **required** and git-ignored: compose will not start without it, and the injector refuses to run without a credential. Edit stations by copying `config.example.yaml` to `config.yaml` and setting `OPENFSD_CONFIG=./config.yaml`.

After the image is on GHCR:

```bash
docker compose pull
docker compose up -d
```

To attach to the OpsAero compose network instead:

```yaml
# extra keys on this repo's docker-compose.yml
networks:
  default:
    name: opsaero_default
    external: true
```

```
OPENFSD_HOST=fsd
OPENFSD_API_BASE=http://fsdweb:8010
```

## What v0.1 does

- Connects to openFSD as ATC (`$ID` + `#AA`)
- Sends `%` position updates on the configured ATIS frequency
- Fetches METAR hourly (Aviation Weather by default)
- Advances the ATIS letter when the METAR changes and broadcasts `$CQ … NEWATIS`
- Answers `$CQ ATIS` with `V` / `T` / `E` lines the same way VATSIM ATIS stations do
- Answers `RN` and `CAPS` queries
- Rate-limits `$CQ ATIS` replies per requester so a station cannot be used to amplify traffic
- Supervises each station's background tasks and reconnects it with backoff when the link drops
- Optional TTS: looping WAV/OGG from the text ATIS, cached until the information letter changes
- Plugin-shaped so the next injector (traffic, weather, supervisor tools) is another class, not a rewrite

## What it does not do yet

openFSD does **not** stream VHF audio. On VATSIM, voice ATIS rides AFV, which is a different service from FSD. On a private server you need a radio stack the pilot client can use (TrackAudio against a private AFV-like server, SRS, etc.).

v0.1 therefore:

- Serves **text ATIS** over FSD (works today with vPilot / xPilot / EuroScope / vatSys once they are pointed at your server)
- Generates looping WAV/OGG from the current text ATIS when `voice.backend` is `tts` (cache `audio/cache/{icao}.wav`, refresh only when the information letter changes)

Do **not** scrape LiveATC (or similar) and rebroadcast it. Those feeds are copyrighted and often geo-restricted. `voice.scrape_url` is ignored.

**Radio egress is a separate sidecar.** This process does not play audio on the station frequency. Pilots only hear the WAV if you run an AFV-compatible stack (TrackAudio) or SRS alongside openFSD — see [issue #2](https://github.com/Ops-Aero/openfsd-injector/issues/2). The FSD side already advertises the frequency in `%` position packets.

## Quick start

You need a dedicated ATIS identity (CID + password) that you created for the injector — not CID 1, not Administrator. Protocol revision must be 100 or 101.

```bash
git clone https://github.com/Ops-Aero/openfsd-injector
cd openfsd-injector
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,tts]"   # omit ,tts if you do not need edge-tts

cp config.example.yaml config.yaml    # edit stations
cp .env.example .env                  # edit OPENFSD_CID / OPENFSD_PASSWORD
python -m openfsd_injector -c config.yaml -v
```

Run the tests with `pytest`.

### Credentials

- `OPENFSD_CID` + `OPENFSD_PASSWORD`, or a pre-minted `OPENFSD_TOKEN`, must be supplied by you. There is no built-in fallback: startup fails with an explicit error listing what is missing.
- Keep them in `.env` (git-ignored) or in a `config.yaml` you do not commit. Never commit a filled-in copy of `.env.example` or `config.example.yaml`.
- Use a least-privilege ATIS account, never the bootstrap admin / CID 1. Rating 12 is rejected at startup unless `OPENFSD_ALLOW_ADMINISTRATOR=1`. If a server install printed a default password for a seeded account, rotate it and treat it as public.
- Startup also validates the rest of the config (coordinate ranges, ATIS frequency, timers, duplicate callsigns) and reports every problem at once.

Point `server.host` at the FSD port (`6809`), not a web UI. If you set `server.api_base`, it must be the openfsd HTTP API (`http://127.0.0.1:8010` on OpsAero). Leave `api_base` empty to send the password as the `#AA` token.

### Voice TTS

Set `plugins.atis.voice.enabled: true` and `backend: tts`. The hourly ATIS job then:

1. Speaks the current text lines with **edge-tts** (no GPU; needs network). **piper** is optional if you set `engine: piper` and `piper_model` to an ONNX voice.
2. Writes `audio/cache/{icao}.wav` (and `{icao}.ogg` when `ffmpeg` can transcode).
3. Keeps a `{icao}.letter` sidecar and skips synthesis until the information letter changes.

`backend: file` still uses a WAV you drop in `cache_dir`. `backend: none` is silent.

Playing the cached file on 128.080 (or whatever you configured) is **not** this injector — that is the AFV/SRS sidecar in [issue #2](https://github.com/Ops-Aero/openfsd-injector/issues/2).

## Licence

MIT. Flight simulation only — not for real-world navigation or ATC.
