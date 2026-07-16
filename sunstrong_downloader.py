#!/usr/bin/env python3
"""
SunStrong / mySunPower historical data downloader (headless, no emulator).

  Auth : POST https://edp-api.edp.sunstrongmonitoring.com/v1/auth/okta/signin
         {"username": ..., "password": ...}  ->  {access_token, refresh_token, id_token}
  Data : POST https://edp-api-graphql.mysunstrong.com/graphql
         Authorization: Bearer <access_token>

Requires: Python 3.9+ and `requests`  (pip install requests)

Credentials are read from env vars SUNSTRONG_USERNAME / SUNSTRONG_PASSWORD,
or prompted interactively. Tokens are never printed or written to disk.

Usage:
  python sunstrong_downloader.py probe
  python sunstrong_downloader.py sites
  python sunstrong_downloader.py power --start 2015-01-01 --end 2026-07-15 \
         --interval FIVE_MINUTE --out power_history.csv
"""
from __future__ import annotations

import argparse
import base64
import csv
import getpass
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

# The GraphQL host's CDN fingerprints the TLS ClientHello and drops Python's default
# (OpenSSL) handshake, so we use curl_cffi (impersonates a real client). Falls back to
# requests only if curl_cffi is unavailable (auth works, but GraphQL likely won't).
_USE_CURL_CFFI = True
try:
    from curl_cffi import requests as _http
except ImportError:
    _USE_CURL_CFFI = False
    try:
        import requests as _http
    except ImportError:
        sys.exit("This tool needs curl_cffi.  Run:  pip install curl_cffi")

AUTH_BASE = "https://edp-api.edp.sunstrongmonitoring.com/v1"
GRAPHQL_URL = "https://edp-api-graphql.mysunstrong.com/graphql"
# Do NOT advertise ourselves. With curl_cffi we inherit a genuine Chrome User-Agent that
# matches the impersonated TLS fingerprint, so traffic blends into ordinary browser requests.
# This realistic UA is only a fallback for the plain-requests path (keeps it off 'python-requests').
FALLBACK_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0.0.0 Mobile Safari/537.36")

# Seconds per interval, for intervals that map to a fixed duration.
# MONTH/YEAR are calendar-based and are only meaningful for aggregate energy, not power.
INTERVAL_SECONDS = {
    "FIVE_MINUTE": 300,
    "QUARTER_HOUR": 900,
    "HOUR": 3600,
    "DAY": 86400,
    "WEEK": 604800,
}

# ---- verbatim query bodies extracted from the app bundle -------------------

Q_USER_SITES = """
query FetchUserSites($partyId: String!) {
  party(partyId: $partyId) {
    firstName
    lastName
    displayName
    email
    userConfigs { panelDataLicenseAccepted siteKey }
    sites {
      siteKey
      address1
      timezone
      hasMI
      isEnabledInNightvision
      hasPanelLayout
      hasPanelsCheck
    }
  }
}
""".strip()

Q_POWER = """
query FetchPowerData($interval: String!, $end: String!, $start: String!, $siteKey: String!) {
  power(interval: $interval, end: $end, start: $start, siteKey: $siteKey) {
    powerDataSeries {
      powerProduction: production
      powerConsumption: consumption
      powerStorage: storage
      powerGrid: grid
    }
  }
}
""".strip()

Q_CURRENT_POWER = """
query FetchCurrentPower($siteKey: String!) {
  currentPower(siteKey: $siteKey) {
    production
    consumption
    storage
    grid
    timestamp
  }
}
""".strip()

# Panel-level (per microinverter serial), fetched one DAY at a time.
# Per-panel granularity is hourly (hourlyData); plus per-panel daily totals + layout.
Q_PANELS = """
query Panels($date: String!, $siteKey: String!) {
  panels(date: $date, siteKey: $siteKey) {
    hasPanelLayout
    siteDailyEnergyProduction { timestamp value }
    siteHourlyPowerProduction { timestamp value }
    panels {
      serialNumber
      dailyEnergyProduction
      sevenDayAverage
      lastCommunicationTimestamp
      energyColorCode
      peakPowerProduction { timestamp value }
      hourlyData { timestamp power energy powerColorCode }
      layout { xCoordinate yCoordinate rotation azimuth orientation }
    }
  }
}
""".strip()

# System energy totals over a range (single aggregate).
Q_ENERGY_AGG = """
query FetchAggregationEnergy($interval: String!, $start: String!, $end: String!, $siteKey: String!) {
  energy(interval: $interval, start: $start, end: $end, siteKey: $siteKey) {
    totalProduction
    totalConsumption
    netGridImportExport
    netStorageChargedDischarged
  }
}
""".strip()

# System energy per-bucket timeseries (production/consumption/storage/grid totals per interval).
Q_ANALYZE = """
query FetchAnalyzeData($interval: String!, $end: String!, $start: String!, $siteKey: String!) {
  energy(interval: $interval, start: $start, end: $end, siteKey: $siteKey) {
    tooltipProductionTotals { total dateString }
    tooltipConsumptionTotals { total dateString }
    tooltipStorageTotals { total dateString }
    tooltipGridTotals { total dateString }
  }
}
""".strip()


# ---- helpers ---------------------------------------------------------------

def _b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload WITHOUT verifying the signature (we only read claims)."""
    try:
        payload = token.split(".")[1]
        return json.loads(_b64url_decode(payload))
    except Exception as e:
        raise RuntimeError(f"Could not decode token claims: {e}")


def iso_z(dt: datetime) -> str:
    """ISO8601 in UTC with milliseconds and a trailing Z, matching the app format."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fmt_local(dt: datetime) -> str:
    """Naive site-local time 'YYYY-MM-DDTHH:MM:SS' — the format power/energy queries expect."""
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# Max time window the API accepts per request, by interval (empirically found).
# The 'power'/'energy' interval value must be sent LOWERCASE.
POWER_INTERVAL_CHUNK = {
    "five_minute": timedelta(days=1),
    "quarter_hour": timedelta(days=1),
    "hour": timedelta(days=3),    # tested OK to 7d, fails ~31d; 3d gives a wide safety margin
    "day": timedelta(days=90),    # tested OK to ~3mo, fails ~6mo
}


def parse_date(s: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Unrecognized date: {s!r} (use YYYY-MM-DD)")


# ---- API client ------------------------------------------------------------

class SunStrong:
    def __init__(self, verbose: bool = True):
        if _USE_CURL_CFFI:
            # impersonate sets a real Chrome User-Agent + matching TLS/headers; don't override the UA.
            self.s = _http.Session(impersonate="chrome")
            self.s.headers.update({"Accept": "application/json"})
        else:
            self.s = _http.Session()
            self.s.headers.update({"User-Agent": FALLBACK_UA, "Accept": "application/json"})
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.id_token: str | None = None
        self.claims: dict = {}
        self.verbose = verbose

    def log(self, *a):
        if self.verbose:
            print(*a, file=sys.stderr)

    # -- auth --
    def login(self, username: str, password: str) -> None:
        r = self.s.post(
            f"{AUTH_BASE}/auth/okta/signin",
            json={"username": username, "password": password},
            timeout=30,
        )
        if r.status_code != 200:
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text[:200]}
            raise RuntimeError(f"Login failed ({r.status_code}): {body}")
        data = r.json()
        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token")
        self.id_token = data.get("id_token")
        if not self.access_token:
            raise RuntimeError(f"Login response had no access_token. Keys: {list(data)}")
        self.claims = decode_jwt_claims(self.access_token)
        self.log("Logged in. Token claims:", {k: self.claims.get(k) for k in
                 ("partyId", "sub", "email", "exp") if k in self.claims})

    def try_refresh(self) -> bool:
        if not self.refresh_token:
            return False
        r = self.s.post(f"{AUTH_BASE}/auth/okta/refresh",
                        json={"refresh_token": self.refresh_token}, timeout=30)
        if r.status_code == 200 and r.json().get("access_token"):
            d = r.json()
            self.access_token = d["access_token"]
            self.refresh_token = d.get("refresh_token", self.refresh_token)
            self.claims = decode_jwt_claims(self.access_token)
            return True
        return False

    @property
    def party_id(self) -> str:
        pid = self.claims.get("partyId") or self.claims.get("party_id")
        if not pid:
            raise RuntimeError(f"No partyId claim in token. Claims: {list(self.claims)}")
        return pid

    # -- graphql --
    def gql(self, query: str, variables: dict, operation: str | None = None, _retry=True) -> dict:
        headers = {"Authorization": f"Bearer {self.access_token}",
                   "Content-Type": "application/json"}
        payload = {"query": query, "variables": variables}
        if operation:
            payload["operationName"] = operation
        r = self.s.post(GRAPHQL_URL, json=payload, headers=headers, timeout=60)
        if r.status_code in (401, 403) and _retry and self.try_refresh():
            return self.gql(query, variables, operation, _retry=False)
        try:
            body = r.json()
        except Exception:
            raise RuntimeError(f"GraphQL non-JSON response ({r.status_code}): {r.text[:200]}")
        if body.get("errors"):
            raise RuntimeError(f"GraphQL errors: {json.dumps(body['errors'])[:400]}")
        return body.get("data", {})

    # -- domain calls --
    def sites(self) -> list[dict]:
        data = self.gql(Q_USER_SITES, {"partyId": self.party_id}, "FetchUserSites")
        party = data.get("party") or {}
        return party.get("sites") or []

    def power(self, site_key: str, start: datetime, end: datetime, interval: str) -> dict:
        # interval must be lowercase; start/end must be naive site-local time.
        return self.gql(
            Q_POWER,
            {"siteKey": site_key, "interval": interval.lower(),
             "start": fmt_local(start), "end": fmt_local(end)},
            "FetchPowerData",
        )

    def panels(self, site_key: str, date_str: str) -> dict:
        return self.gql(Q_PANELS, {"siteKey": site_key, "date": date_str}, "Panels")

    def energy_total(self, site_key: str, start: datetime, end: datetime, interval: str) -> dict:
        return self.gql(
            Q_ENERGY_AGG,
            {"siteKey": site_key, "interval": interval.lower(),
             "start": fmt_local(start), "end": fmt_local(end)},
            "FetchAggregationEnergy",
        )


# ---- commands --------------------------------------------------------------

def load_dotenv(path=".env"):
    """Minimal .env loader (no dependency): KEY=VALUE lines, does not overwrite real env."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


def get_credentials(args) -> tuple[str, str]:
    load_dotenv()
    user = args.username or os.environ.get("SUNSTRONG_USERNAME")
    pw = os.environ.get("SUNSTRONG_PASSWORD")
    if not user:
        user = input("SunStrong/mySunPower email: ").strip()
    if not pw:
        pw = getpass.getpass("Password (hidden): ")
    return user, pw


def _resolve_site(api, args):
    sites = api.sites()
    if not sites:
        sys.exit("No sites on this account.")
    if args.site:
        return args.site
    return sites[0]["siteKey"]


def cmd_probe(args):
    """Validate login + dump raw power AND panel responses so we can calibrate formats."""
    api = SunStrong()
    api.login(*get_credentials(args))
    sites = api.sites()
    print(f"\npartyId: {api.party_id}")
    print(f"Found {len(sites)} site(s):")
    for s in sites:
        print(f"  - siteKey={s.get('siteKey')}  tz={s.get('timezone')}  "
              f"addr={s.get('address1')!r}  hasMI={s.get('hasMI')}  "
              f"hasPanelLayout={s.get('hasPanelLayout')}")
    if not sites:
        return
    site_key = args.site or sites[0]["siteKey"]
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)

    # 1) system 5-min power sample
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=2)
    print(f"\n[power] FIVE_MINUTE {iso_z(start)} .. {iso_z(end)}")
    try:
        pdata = api.power(site_key, start, end, "FIVE_MINUTE")
        series = (pdata.get("power") or {}).get("powerDataSeries") or {}
        lengths = {k: (len(v) if isinstance(v, list) else v) for k, v in series.items()}
        print(f"  powerDataSeries lengths: {lengths}  (expected ~{int((end-start).total_seconds()//300)})")
    except Exception as e:
        pdata = {"error": str(e)}
        print(f"  power error: {e}")

    # 2) panel-level sample — try a couple of date formats to see which the API accepts
    pan_data, used_fmt = None, None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT00:00:00.000Z"):
        ds = yesterday.strftime(fmt)
        try:
            d = api.panels(site_key, ds)
            if d.get("panels") is not None:
                pan_data, used_fmt = d, fmt
                break
        except Exception as e:
            print(f"  panels(date={ds!r}) error: {e}")
    if pan_data:
        p = pan_data.get("panels") or {}
        plist = p.get("panels") or []
        print(f"[panels] date format {used_fmt!r} works: {len(plist)} panel(s)")
        if plist:
            ex = plist[0]
            hd = ex.get("hourlyData") or []
            print(f"  e.g. serial={ex.get('serialNumber')}  dailyEnergy={ex.get('dailyEnergyProduction')}  "
                  f"hourlyData points={len(hd)}")
    else:
        print("[panels] could not fetch panel data with tried date formats (see errors above)")

    out = os.path.abspath("raw_sample.json")
    with open(out, "w") as f:
        json.dump({"power": pdata, "panels_date_format": used_fmt, "panels": pan_data}, f, indent=2)
    print(f"\nRaw sample written to {out}")


def _next_bucket(dt: datetime, interval: str) -> datetime:
    if interval == "day":
        return dt + timedelta(days=1)
    if interval == "month":
        return datetime(dt.year + (dt.month // 12), (dt.month % 12) + 1, 1)
    if interval == "year":
        return datetime(dt.year + 1, 1, 1)
    raise ValueError(interval)


def cmd_energy(args):
    """System energy TOTALS per bucket (day/month/year) — one API call per bucket."""
    api = SunStrong()
    api.login(*get_credentials(args))
    site_key = _resolve_site(api, args)
    interval = args.interval.lower()
    start = args.start.replace(tzinfo=None)
    end = (args.end or datetime.now(timezone.utc)).replace(tzinfo=None)
    api.log(f"Downloading {interval} energy totals for {site_key} {start.date()}..{end.date()}")
    n = 0
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bucket_start", "production_kwh", "consumption_kwh",
                    "net_grid_import_export_kwh", "net_storage_kwh"])
        cursor = start
        while cursor < end:
            nxt = _next_bucket(cursor, interval)
            for attempt in range(4):
                try:
                    e = (api.energy_total(site_key, cursor, nxt, interval).get("energy") or {})
                    break
                except Exception as ex:
                    api.log(f"  {cursor.date()} error: {ex} (retry)")
                    time.sleep(2 ** attempt)
            else:
                e = {}
            w.writerow([cursor.strftime("%Y-%m-%d"), e.get("totalProduction", ""),
                        e.get("totalConsumption", ""), e.get("netGridImportExport", ""),
                        e.get("netStorageChargedDischarged", "")])
            n += 1
            if n % 25 == 0:
                api.log(f"  {cursor.date()}: {n} buckets")
            cursor = nxt
    print(f"Done. Wrote {n} energy buckets to {os.path.abspath(args.out)}")


def cmd_panels(args):
    api = SunStrong()
    api.login(*get_credentials(args))
    site_key = _resolve_site(api, args)
    start = args.start
    end = (args.end or datetime.now(timezone.utc)).date()
    date_fmt = args.date_format

    base = os.path.splitext(args.out)[0]
    hourly_path = args.out
    daily_path = base + "_daily.csv"
    layout_path = base + "_layout.csv"
    site_path = base + "_site_hourly.csv"

    api.log(f"Downloading panel-level data for {site_key} {start.date()}..{end} (per-day)")
    n_hourly = n_daily = n_site = 0
    layout_written = set()
    with open(hourly_path, "w", newline="") as fh, \
         open(daily_path, "w", newline="") as fd, \
         open(layout_path, "w", newline="") as fl, \
         open(site_path, "w", newline="") as fs:
        wh = csv.writer(fh); wh.writerow(["timestamp", "serialNumber", "power_w", "energy_kwh"])
        wd = csv.writer(fd); wd.writerow(["date", "serialNumber", "dailyEnergyProduction_kwh",
                                          "sevenDayAverage", "peakTimestamp", "peakValue",
                                          "energyColorCode", "lastCommunicationTimestamp"])
        wl = csv.writer(fl); wl.writerow(["serialNumber", "xCoordinate", "yCoordinate",
                                          "rotation", "azimuth", "orientation"])
        ws = csv.writer(fs); ws.writerow(["date", "timestamp", "site_power_kw",
                                          "system_production_kwh_sum_of_panels"])
        day = start.date()
        while day <= end:
            ds = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).strftime(date_fmt)
            for attempt in range(4):
                try:
                    data = api.panels(site_key, ds)
                    break
                except Exception as e:
                    wait = 2 ** attempt
                    api.log(f"  {day} error: {e} (retry in {wait}s)")
                    time.sleep(wait)
            else:
                api.log(f"  giving up on {day}")
                day += timedelta(days=1); continue

            panels_obj = data.get("panels") or {}
            plist = panels_obj.get("panels") or []
            day_prod_sum = 0.0
            for p in plist:
                serial = p.get("serialNumber")
                for h in p.get("hourlyData") or []:
                    wh.writerow([h.get("timestamp"), serial, h.get("power"), h.get("energy")])
                    n_hourly += 1
                dep = p.get("dailyEnergyProduction")
                if isinstance(dep, (int, float)):
                    day_prod_sum += dep
                peak = p.get("peakPowerProduction") or {}
                wd.writerow([day, serial, dep, p.get("sevenDayAverage"),
                             peak.get("timestamp"), peak.get("value"), p.get("energyColorCode"),
                             p.get("lastCommunicationTimestamp")])
                n_daily += 1
                lay = p.get("layout")
                if lay and serial not in layout_written:
                    wl.writerow([serial, lay.get("xCoordinate"), lay.get("yCoordinate"),
                                 lay.get("rotation"), lay.get("azimuth"), lay.get("orientation")])
                    layout_written.add(serial)
            # site-level hourly power (free, comes bundled in the Panels response)
            site_hourly = panels_obj.get("siteHourlyPowerProduction") or []
            for i, pt in enumerate(site_hourly):
                ws.writerow([day, pt.get("timestamp"), pt.get("value"),
                             round(day_prod_sum, 3) if i == 0 else ""])
                n_site += 1
            api.log(f"  {day}: {len(plist)} panels, {day_prod_sum:.2f} kWh  (hourly rows {n_hourly})")
            day += timedelta(days=1)
    print(f"Done. Panel hourly -> {os.path.abspath(hourly_path)} ({n_hourly} rows)")
    print(f"      Panel daily  -> {os.path.abspath(daily_path)} ({n_daily} rows)")
    print(f"      Panel layout -> {os.path.abspath(layout_path)} ({len(layout_written)} panels)")
    print(f"      Site hourly  -> {os.path.abspath(site_path)} ({n_site} rows)")


def cmd_sites(args):
    api = SunStrong()
    api.login(*get_credentials(args))
    for s in api.sites():
        print(json.dumps(s))


def _parse_power_series(series: dict) -> dict:
    """powerDataSeries fields are lists of [timestamp, value, quality]; merge by timestamp."""
    aliases = {"production": "powerProduction", "consumption": "powerConsumption",
               "storage": "powerStorage", "grid": "powerGrid"}
    rows = {}  # timestamp -> {field: value}
    for field, key in aliases.items():
        for pt in series.get(key) or []:
            if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                continue
            ts, val = pt[0], pt[1]
            rows.setdefault(ts, {})[field] = val
    return rows


def cmd_power(args):
    """System power/production timeseries. Default interval=hour (data back to ~2019);
    five_minute exists but is padded with zeros for hourly-reporting systems."""
    api = SunStrong()
    api.login(*get_credentials(args))
    site_key = _resolve_site(api, args)
    interval = args.interval.lower()
    chunk = POWER_INTERVAL_CHUNK.get(interval)
    if chunk is None:
        sys.exit(f"--interval {args.interval} not supported; choose {list(POWER_INTERVAL_CHUNK)}")
    start = args.start.replace(tzinfo=None)
    end = (args.end or datetime.now(timezone.utc)).replace(tzinfo=None)

    api.log(f"Downloading {interval} power for {site_key} {start.date()}..{end.date()} "
            f"in {chunk.days}-day chunks")
    fields = ["production", "consumption", "storage", "grid"]
    written = 0
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp"] + [x + "_kw" for x in fields])
        cursor = start
        while cursor < end:
            c_end = min(cursor + chunk, end)
            for attempt in range(4):
                try:
                    data = api.power(site_key, cursor, c_end, interval)
                    break
                except Exception as e:
                    api.log(f"  {cursor.date()}..{c_end.date()} error: {e} (retry)")
                    time.sleep(2 ** attempt)
            else:
                api.log(f"  giving up on {cursor.date()}..{c_end.date()}")
                cursor = c_end
                continue
            series = (data.get("power") or {}).get("powerDataSeries") or {}
            rows = _parse_power_series(series)
            for ts in sorted(rows):
                r = rows[ts]
                w.writerow([ts] + [r.get(fld, "") for fld in fields])
                written += 1
            api.log(f"  {cursor.date()}..{c_end.date()}: {len(rows)} points  (total {written})")
            cursor = c_end
    print(f"Done. Wrote {written} rows to {os.path.abspath(args.out)}")


def build_parser():
    p = argparse.ArgumentParser(description="Download SunStrong/mySunPower solar history (headless).")
    p.add_argument("--username", help="account email (or set SUNSTRONG_USERNAME)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("probe", help="log in, list sites, dump one raw power sample to calibrate")
    pr.add_argument("--site", help="siteKey (default: first site)")
    pr.set_defaults(func=cmd_probe)

    si = sub.add_parser("sites", help="list sites (JSON per line)")
    si.set_defaults(func=cmd_sites)

    po = sub.add_parser("power", help="download SYSTEM power/production timeseries to CSV "
                                      "(hourly reaches back to ~2019)")
    po.add_argument("--site", help="siteKey (default: first site)")
    po.add_argument("--start", type=parse_date, required=True, help="start date YYYY-MM-DD (site-local)")
    po.add_argument("--end", type=parse_date, help="end date YYYY-MM-DD (default: now)")
    po.add_argument("--interval", default="hour",
                    choices=["five_minute", "quarter_hour", "hour", "day"],
                    help="hour is recommended (5-min is zero-padded on hourly-reporting systems)")
    po.add_argument("--out", default="power_hourly.csv")
    po.set_defaults(func=cmd_power)

    en = sub.add_parser("energy", help="download SYSTEM energy TOTALS per bucket to CSV "
                                       "(back to ~2019)")
    en.add_argument("--site", help="siteKey (default: first site)")
    en.add_argument("--start", type=parse_date, required=True, help="start date YYYY-MM-DD (site-local)")
    en.add_argument("--end", type=parse_date, help="end date YYYY-MM-DD (default: now)")
    en.add_argument("--interval", default="month", choices=["day", "month", "year"])
    en.add_argument("--out", default="energy_totals.csv")
    en.set_defaults(func=cmd_energy)

    pa = sub.add_parser("panels", help="download PANEL-LEVEL data (per microinverter serial, hourly) to CSV")
    pa.add_argument("--site", help="siteKey (default: first site)")
    pa.add_argument("--start", type=parse_date, required=True, help="start date YYYY-MM-DD (UTC)")
    pa.add_argument("--end", type=parse_date, help="end date YYYY-MM-DD (default: now)")
    pa.add_argument("--date-format", default="%Y-%m-%d",
                    help="strftime for the Panels $date variable (use probe to confirm; "
                         "fallback '%%Y-%%m-%%dT00:00:00.000Z')")
    pa.add_argument("--out", default="panels_hourly.csv",
                    help="hourly CSV; a _daily.csv and _layout.csv are written alongside")
    pa.set_defaults(func=cmd_panels)
    return p


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
    except RuntimeError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
