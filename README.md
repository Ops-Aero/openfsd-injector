# openfsd-injector

Plugin injector for a self-hosted [openFSD](https://github.com/renorris/openfsd) server, including the [Ops-Aero/opsaero-main](https://github.com/Ops-Aero/opsaero-main) stack.

It logs extra ATC stations onto your network and keeps them alive. The first plugin is an **ATIS bot**: one FSD connection per airport, text ATIS built from live METAR, replies to `$CQ ATIS` so pilot clients can pull the information letter.

Voice ATIS that pilots can *tune* is a separate problem — openFSD is the FSD data plane, not Audio for VATSIM. The voice hook is in the repo so the next step has a single place to land.

## OpsAero (opsaero-main)

`./install.sh` in opsaero-main publishes:

| Service | Bind | Use from this injector |
|---|---|---|
| FSD protocol | `127.0.0.1:6809` | `server.host` / `server.port` |
| openfsd admin + `/api/v1/fsd-jwt` | `127.0.0.1:8010` | `server.api_base` |
| Laravel website | `127.0.0.1:8000` | **not** an FSD API |

Seeded protocol user: **CID 1** / **opsaeroadmin** (Administrator). Requested `auth.rating` must be ≤ that (default 5 / C1 is fine). Tower ATIS (`facility_type: 4`) needs at least S2.

openFSD always accepts the CID password as the `#AA` token. JWT minting is optional. If you do mint, the token expires in **five minutes** and is checked only at logon — this injector mints immediately before each station connects.

Do not point `api_base` at `:8000`. That is Laravel; JWT mint will 404.

```bash
git clone https://github.com/JayCommit/openfsd-injector
cd openfsd-injector
docker compose up -d --build
docker compose logs -f
```

That talks to host-published FSD `:6809` and openfsd API `:8010` via `host.docker.internal`. No `.env` required (CID 1 / `opsaeroadmin`). Edit stations by copying `config.example.yaml` to `config.yaml` and setting `OPENFSD_CONFIG=./config.yaml`.

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
- Plugin-shaped so the next injector (traffic, weather, supervisor tools) is another class, not a rewrite

## What it does not do yet

openFSD does **not** stream VHF audio. On VATSIM, voice ATIS rides AFV, which is a different service from FSD. On a private server you need a radio stack the pilot client can use (TrackAudio against a private AFV-like server, SRS, etc.).

v0.1 therefore:

- Serves **text ATIS** over FSD (works today with vPilot / xPilot / EuroScope / vatSys once they are pointed at your server)
- Leaves a `voice` backend stub (`none` | `file` | `tts`) for the hourly audio job

Do **not** scrape LiveATC (or similar) and rebroadcast it. Those feeds are copyrighted and often geo-restricted. Generate speech from the METAR/ATIS text, or use audio you have rights to.

## Quick start

You need an openFSD user (CID + password). On a stock OpsAero install that is CID 1. Protocol revision must be 100 or 101.

```bash
git clone https://github.com/JayCommit/openfsd-injector
cd openfsd-injector
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp config.example.yaml config.yaml
cp .env.example .env
python -m openfsd_injector -c config.yaml -v
```

Point `server.host` at the FSD port (`6809`), not a web UI. If you set `server.api_base`, it must be the openfsd HTTP API (`http://127.0.0.1:8010` on OpsAero). Leave `api_base` empty to send the password as the `#AA` token.

## Licence

MIT. Flight simulation only — not for real-world navigation or ATC.
