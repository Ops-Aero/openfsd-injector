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

Create a **dedicated openFSD account for the injector** and give it the lowest network rating your stations need — do not reuse the administrator account your server install seeded, and change any password that install printed. Requested `auth.rating` must be ≤ the rating stored on that CID. Tower ATIS (`facility_type: 4`) needs at least S2 (3).

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
- Plugin-shaped so the next injector (traffic, weather, supervisor tools) is another class, not a rewrite

## What it does not do yet

openFSD does **not** stream VHF audio. On VATSIM, voice ATIS rides AFV, which is a different service from FSD. On a private server you need a radio stack the pilot client can use (TrackAudio against a private AFV-like server, SRS, etc.).

v0.1 therefore:

- Serves **text ATIS** over FSD (works today with vPilot / xPilot / EuroScope / vatSys once they are pointed at your server)
- Leaves a `voice` backend stub (`none` | `file` | `tts`) for the hourly audio job

Do **not** scrape LiveATC (or similar) and rebroadcast it. Those feeds are copyrighted and often geo-restricted. Generate speech from the METAR/ATIS text, or use audio you have rights to.

## Quick start

You need an openFSD user (CID + password) that you created for the injector. Protocol revision must be 100 or 101.

```bash
git clone https://github.com/Ops-Aero/openfsd-injector
cd openfsd-injector
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp config.example.yaml config.yaml    # edit stations
cp .env.example .env                  # edit OPENFSD_CID / OPENFSD_PASSWORD
python -m openfsd_injector -c config.yaml -v
```

Run the tests with `pytest`.

### Credentials

- `OPENFSD_CID` + `OPENFSD_PASSWORD`, or a pre-minted `OPENFSD_TOKEN`, must be supplied by you. There is no built-in fallback: startup fails with an explicit error listing what is missing.
- Keep them in `.env` (git-ignored) or in a `config.yaml` you do not commit. Never commit a filled-in copy of `.env.example` or `config.example.yaml`.
- Use a least-privilege account for the injector, not an administrator one. If a server install printed a default password for a seeded account, rotate it and treat it as public.
- Startup also validates the rest of the config (coordinate ranges, ATIS frequency, timers, duplicate callsigns) and reports every problem at once.

Point `server.host` at the FSD port (`6809`), not a web UI. If you set `server.api_base`, it must be the openfsd HTTP API (`http://127.0.0.1:8010` on OpsAero). Leave `api_base` empty to send the password as the `#AA` token.

## Licence

MIT. Flight simulation only — not for real-world navigation or ATC.
