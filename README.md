# openfsd-injector

Plugin injector for a self-hosted [openFSD](https://github.com/renorris/openfsd) server, including the [Ops-Aero/opsaero-main](https://github.com/Ops-Aero/opsaero-main) stack. First plugin: an **ATIS bot** — **one container, one FSD CID, many stations**. Text ATIS from live METAR, `$CQ ATIS` replies, TTS WAV/OGG, and an HTTP audio index for opsaero-main / a radio client.

## Easy setup

Dedicated ATIS user only — not CID 1, not Administrator. Rating 3 (S2).

```bash
git clone https://github.com/Ops-Aero/openfsd-injector
cd openfsd-injector
cp .env.example .env
# set OPENFSD_CID and OPENFSD_PASSWORD (your dedicated ATIS user, not CID 1)
docker compose up -d --build
```

`.env` is git-ignored. No password → startup fails. Default `ATIS_ICAOS` is the UK + EU + US majors list (see below). `config.yaml` stations override that list. In Docker, voice defaults to `tts` (or set `VOICE_BACKEND=tts`). No `docker.env`.

## One container, majors list

One process, one FSD CID, many `_ATIS` callsigns. Jay already confirmed two stations on one CID works; the built-in table now covers:

**UK:** EGLL EGKK EGSS EGLC EGGW EGCC EGGP EGBB EGNX EGPH EGPF EGPD EGAA EGHH EGGD EGNT

**EU:** EHAM LFPG LFPO EDDF EDDM LEMD LEBL LIRF LSZH EBBR EKCH ENGM EIDW

**US:** KATL KORD KLAX KDFW KDEN KJFK KSFO KSEA KLAS KMIA KPHX KBOS KIAD KEWR

Those ICAOs resolve from `src/openfsd_injector/airports.py` (published ATIS COM frequency, ARP lat/lon, 60 NM vis range). Unknown ICAOs fail fast — add a station block to `config.yaml` instead of guessing.

## Audio index for opsaero-main / client

TTS writes `audio/cache/{icao}.wav` (and `.ogg` when ffmpeg can transcode). This container also serves them on an internal port:

| URL | What |
| --- | --- |
| `http://injector:8091/atis/index.json` | icao, callsign, frequency, letter, audio URL |
| `http://injector:8091/atis/egll.wav` | cached WAV |
| `http://injector:8091/atis/egll.ogg` | cached OGG if present |

No auth on the compose network. The process binds `0.0.0.0:8091` **inside** the container. **Do not publish 8091 on the host** unless you opt in (`AUDIO_HTTP_PUBLISH=1` and/or uncomment the `ports:` block in `docker-compose.yml`, preferably `127.0.0.1:8091:8091`).

The index refreshes when an information letter / wav changes. Do **not** scrape LiveATC.

Pointing a radio client at that index is the first-party Linux/Docker path. Playing on frequency (AFV / SRS transmit) is still [issue #2](https://github.com/Ops-Aero/openfsd-injector/issues/2). `SRS_HOST` is reserved and default-off; there is no Windows-only `DCS-SR-ExternalAudio.exe` in this image.

## Plug into opsaero-main

Create a dedicated openFSD account (rating 3). Then either stay on the host network / `extra_hosts` (default compose: `OPENFSD_HOST=host.docker.internal`, API `http://host.docker.internal:8010`) or join the `opsaero` compose network:

```yaml
# bottom of docker-compose.yml
networks:
  default:
    name: opsaero
    external: true
```

```
OPENFSD_HOST=fsd
OPENFSD_API_BASE=http://fsdweb:8010
```

Audio index from another service on that network: `http://injector:8091/atis/index.json`.

Do not point the API at Laravel `:8000`. Rating 12 is refused unless `OPENFSD_ALLOW_ADMINISTRATOR=1`.

`./install.sh` in opsaero-main publishes FSD `:6809`, openfsd admin `:8010` (JWT mint), and Laravel `:8000` (not an FSD API).

## What v0.1 does

- Connects to openFSD as ATC (`$ID` + `#AA`) — one CID, one connection per station callsign
- Sends `%` position updates on the configured ATIS frequency
- Fetches METAR hourly (Aviation Weather by default)
- Advances the ATIS letter when the METAR changes and broadcasts `$CQ … NEWATIS`
- Answers `$CQ ATIS` with `V` / `T` / `E` lines the same way VATSIM ATIS stations do
- Answers `RN` and `CAPS` queries
- Rate-limits `$CQ ATIS` replies per requester so a station cannot be used to amplify traffic
- Supervises each station's background tasks and reconnects it with backoff when the link drops
- Optional TTS: looping WAV/OGG from the text ATIS, cached until the information letter changes
- Serves the audio index over HTTP for opsaero-main / a radio client
- Plugin-shaped so the next injector (traffic, weather, supervisor tools) is another class, not a rewrite

## What it does not do yet

openFSD does **not** stream VHF audio. On VATSIM, voice ATIS rides AFV. On a private server you need a radio stack the pilot client can use (TrackAudio / SRS).

- Serves **text ATIS** over FSD (vPilot / xPilot / EuroScope / vatSys once pointed at your server)
- Generates looping WAV/OGG when `voice.backend` is `tts` (`audio/cache/{icao}.wav`, refresh only when the information letter changes)
- Serves those files at `http://injector:8091/atis/…`

Do **not** scrape LiveATC. `voice.scrape_url` is ignored.

**Radio egress** (actually keying the frequency) remains [issue #2](https://github.com/Ops-Aero/openfsd-injector/issues/2).

## Host / Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,tts]"   # omit ,tts if you do not need edge-tts

cp config.example.yaml config.yaml    # optional station override
cp .env.example .env                  # OPENFSD_CID / OPENFSD_PASSWORD
python -m openfsd_injector -c config.yaml -v
```

`pytest` runs the tests.

- `OPENFSD_CID` + `OPENFSD_PASSWORD`, or `OPENFSD_TOKEN`, are required. There is no built-in fallback.
- `config.yaml` stations override `ATIS_ICAOS`. Unknown ICAOs in `ATIS_ICAOS` fail fast — add a station block with lat/lon/frequency.
- Rating 12 is rejected unless `OPENFSD_ALLOW_ADMINISTRATOR=1`. Rotate any password a server install printed.
- Host Python binds the audio index to `127.0.0.1:8091` unless `AUDIO_HTTP_PUBLISH=1` or `AUDIO_HTTP_HOST` is set.

JWT minting is optional (`OPENFSD_API_BASE`). Tokens expire in five minutes and are minted immediately before each `#AA`. Leave `api_base` empty to send the CID password as the FSD token.

### Voice TTS

`VOICE_BACKEND=tts`, or unset in Docker, enables TTS without extra audio files. The hourly job speaks the text ATIS with **edge-tts** (piper if `engine: piper` and `piper_model` is set), writes `audio/cache/{icao}.wav`, and skips synthesis until the information letter changes.

`backend: file` uses a WAV you drop in `cache_dir`. `backend: none` is silent.

## Licence

MIT. Flight simulation only — not for real-world navigation or ATC.
