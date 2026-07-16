# SunStrong / mySunPower data downloader

This script lets you download your full solar history (whaterver is left, anyway)
from **SunStrong Connect / mySunPower** straight to a CSV file --
**no emulator, no rooting, no browser**. You log in with the same username/password you use in
the app, and the tool pulls data from the same private API the app uses.

Why do this?  You can use the CSV to import the data into the database of your choice (HomeAssistant,InfluxDB, etc). 

## What you can download

| Command  | Scope        | Granularity                    | Identity | History* |
|----------|--------------|--------------------------------|----------|----------|
| `power`  | Whole system | **hourly** (also 5-min/15-min) | site     | back to system start |
| `energy` | Whole system | day / month / year totals      | site     | back to system start |
| `panels` | Per panel    | **hourly** power **and** energy | **microinverter serial** | ~2 years |

`panels` also writes per-panel **daily** totals and the physical **layout** (x/y/rotation/azimuth).

*History depth varies by account. SunPower had some database crashes a few years ago, so some of your 
data may have been wiped out.  Also, panel-level seems to go back only a few years, so YMMV.  

When SunStrong acquired these assests, they reduced system granularity from 5/15 minute intervals to 
1 hour (at least for my system). 

## Setup

```bash
pip install requests
```

Credentials via environment (.env) (or you'll be prompted):

```bash
export SUNSTRONG_USERNAME="you@example.com"
export SUNSTRONG_PASSWORD="your-app-password"     # Windows PowerShell: $env:SUNSTRONG_PASSWORD="..."
```

## Use

**1. Probe first** — logs in, lists your site(s), and confirms the panel date format:

```bash
python sunstrong_downloader.py probe
```

Note your `siteKey` and account timezone from the output. It writes `raw_sample.json` so you can
eyeball the real response shape.

**2. Download.** Examples (adjust dates; your system's install date is a good `--start`):

```bash
# System hourly power/production, full history
python sunstrong_downloader.py power  --start 2019-01-01 --interval hour --out power_hourly.csv

# System energy totals per month (or day/year)
python sunstrong_downloader.py energy --start 2019-01-01 --interval month --out energy_monthly.csv

# Panel-level, hourly power+energy per microinverter serial (last ~2 years)
python sunstrong_downloader.py panels --start 2024-01-01 --out panels_hourly.csv
#   -> panels_hourly.csv, panels_hourly_daily.csv, panels_hourly_site_hourly.csv, panels_hourly_layout.csv
```

Dates are treated as **site-local**. Pick a specific site with `--site <siteKey>` if you have more
than one. If a `power`/`energy` call returns `bad query params`, your interval must be lowercase.

## Output columns

- **power** — `timestamp, production_kw, consumption_kw, storage_kw, grid_kw`
  (at `--interval hour`, each `production_kw` value equals that hour's **kWh**, so this doubles as
  system hourly energy — validated to match the `energy` monthly totals within ~1%)
- **energy** — `bucket_start, production_kwh, consumption_kwh, net_grid_import_export_kwh, net_storage_kwh`
- **panels (hourly)** — `timestamp, serialNumber, power_w, energy_kwh`
- **panels (daily)** — `date, serialNumber, dailyEnergyProduction_kwh, sevenDayAverage, peakTimestamp, peakValue, energyColorCode, lastCommunicationTimestamp`
- **panels (layout)** — `serialNumber, xCoordinate, yCoordinate, rotation, azimuth, orientation`

## What needs a paid subscription

- **Without a subscription:** the `panels` command still works — per-panel and system
  **production** at hourly resolution (last ~2 years). `power` and `energy` return empty/zero.
- **With an active subscription:** `power` (system hourly, full history) and `energy` (totals,
  full history) light up — reaching back to system commissioning, far deeper than panel data.

So a subscription is mainly worth it to get **system history older than the panel-level window**.
Note the tool needs the interval **lowercase** on those two commands (the paid API validates it).

## Notes & caveats

- **Calibration:** dates are sent as UTC. If the very first/last buckets look shifted, your data may
  be keyed to site-local time — check `probe`'s `raw_sample.json` and adjust `--start/--end` by your
  offset. The `panels` `--date-format` can be switched to the ISO fallback if `probe` says so.
- **Be gentle:** downloads run one chunk/day at a time with retries and backoff. Pulling many years
  of 5-minute data is a lot of requests — expect it to take a while. `storage`/`grid` are blank if you
  have no battery/consumption metering.
- **Credentials** are sent only to `edp-api.edp.sunstrongmonitoring.com/v1/auth/okta/signin`; the
  access token stays in memory and is never printed or written to disk.
- **Blending in:** the tool does *not* advertise itself. `curl_cffi` impersonates Chrome, so both the
  TLS handshake and the `User-Agent` look like an ordinary Chrome browser (a UA that can't be blocked
  wholesale). It sends no custom/identifying headers. (This is also required for the GraphQL host,
  which drops Python's default TLS fingerprint outright.)
- Endpoints are undocumented and may change; if a call starts failing, the app may have been updated.
