# openfsd-injector

Plugin injector for a self-hosted [openFSD](https://github.com/renorris/openfsd) server.

It logs extra ATC stations onto your network and keeps them alive. The first plugin is an **ATIS bot**: one FSD connection per airport, text ATIS built from live METAR, replies to `$CQ ATIS` so pilot clients can pull the information letter.

Voice ATIS that pilots can *tune* is a separate problem — openFSD is the FSD data plane, not Audio for VATSIM. The voice hook is in the repo so the next step has a single place to land.

## What v0.1 does

- Connects to openFSD as ATC (`$ID` + `#AA`)
- Sends `%` position updates on the configured ATIS frequency
- Fetches METAR hourly (Aviation Weather by default)
- Advances the ATIS letter when the METAR changes
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

You need an openFSD user (CID + password) created in the web UI. Protocol revision must be 100 or 101.

```bash
git clone https://github.com/JayCommit/openfsd-injector
cd openfsd-injector
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp config.example.yaml config.yaml
cp .env.example .env
# edit CID, password, host, and stations
python -m openfsd_injector -c config.yaml -v
```

Point `server.host` at the FSD port (`6809`), not the web UI. Point `server.api_base` at the web API (`http://host:8000`) so the injector can mint a JWT. If your compose file sets `PLAINTEXT_PASSWORDS=true` you can leave `api_base` empty and the password is sent as the `#AA` token.

## Licence

MIT. Flight simulation only — not for real-world navigation or ATC.
