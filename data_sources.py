"""
data_sources.py — all external data pulls, kept separate from scoring logic
so each fetcher can be tested/swapped independently.
"""

import json
import re
from datetime import datetime, timezone, timedelta

import requests

from config import ALL_STATIONS, STATION_NETWORK

NBM_TEXT_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod"
METAR_URL = "https://aviationweather.gov/api/data/metar"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
IEM_DAILY_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py"

# NWS text-product API.
NWS_PRODUCTS_URL = "https://api.weather.gov/products"

# NWS CLI products for the stations currently supported by the tracker.
# KMIA is the important one because Polymarket US weather settlement uses
# the NWS Daily Climate Report (CLI).
NWS_CLI_CONFIG = {
    "KMIA": {
        "office": "MFL",
        "pil": "CLIMIA",
        "location": "MIA",
    },
}

NWS_HEADERS = {
    "User-Agent": "weather-signal-bot (personal use)",
    "Accept": "application/geo+json, application/json",
}

HEADER_RE = re.compile(
    r"^\s*(\S+)\s+NBM\s+V[\d.]+\s+NBS\s+GUIDANCE",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# NBM
# ---------------------------------------------------------------------------

def fetch_nbm_raw(cycle: str) -> str:
    now = datetime.now(timezone.utc)
    ymd = now.strftime("%Y%m%d")
    url = (
        f"{NBM_TEXT_BASE}/blend.{ymd}/{cycle}/text/"
        f"blend_nbstx.t{cycle}z"
    )

    resp = requests.get(url, timeout=60)

    # NOAA's NOMADS server returns 403 (not 404) for files that don't
    # exist yet, e.g. requesting a cycle before it's been published.
    # Treat both the same for fallback purposes.
    if resp.status_code in (403, 404):
        ymd_prev = (now - timedelta(days=1)).strftime("%Y%m%d")
        url = (
            f"{NBM_TEXT_BASE}/blend.{ymd_prev}/{cycle}/text/"
            f"blend_nbstx.t{cycle}z"
        )
        resp = requests.get(url, timeout=60)

    print(
        f"Fetched NBM data from: {url} "
        f"(status {resp.status_code})"
    )

    resp.raise_for_status()
    return resp.text


def split_by_station(raw_text: str, stations) -> dict:
    stations_set = set(stations)
    matches = list(HEADER_RE.finditer(raw_text))
    blocks = {}

    for i, m in enumerate(matches):
        ident = m.group(1)

        if ident in stations_set:
            start = m.start()
            end = (
                matches[i + 1].start()
                if i + 1 < len(matches)
                else len(raw_text)
            )
            blocks[ident] = raw_text[start:end]

    return blocks


def extract_row(block_text: str, row_label: str):
    for line in block_text.splitlines():
        if line.strip().startswith(row_label):
            tokens = line.strip().split()[1:]
            values = []

            for t in tokens:
                try:
                    values.append(int(t))
                except ValueError:
                    continue

            return values

    return []


# ---------------------------------------------------------------------------
# Date-aware TXN/XND extraction.
# ---------------------------------------------------------------------------

_ISSUE_TIME_RE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4})\s+"
    r"(\d{2})(\d{2})\s+UTC"
)


def parse_bulletin_issue_time(block_text: str):
    """
    Public wrapper used by main.py to derive target_date from the
    bulletin's own fixed issue timestamp rather than wall-clock
    execution time.
    """
    return _parse_bulletin_issue_time(block_text)


def _parse_bulletin_issue_time(block_text: str):
    m = _ISSUE_TIME_RE.search(block_text)

    if not m:
        return None

    month, day, year, hh, mm = (
        int(g) for g in m.groups()
    )

    return datetime(
        year,
        month,
        day,
        hh,
        mm,
        tzinfo=timezone.utc,
    )


def _parse_fixed_width_row(
    line: str,
    num_columns: int,
    label_width: int = 5,
    col_width: int = 3,
):
    """
    NBM text products use a fixed-width layout: label field followed
    by 3-character columns.

    Unlike extract_row(), this preserves blank columns so TXN/XND
    values stay aligned with their actual FHR/UTC column.
    """
    values = []
    pos = label_width

    for _ in range(num_columns):
        chunk = (
            line[pos:pos + col_width].strip()
            if pos < len(line)
            else ""
        )

        try:
            values.append(int(chunk))
        except ValueError:
            values.append(None)

        pos += col_width

    return values


def extract_max_for_date(block_text: str, target_date):
    """
    Returns (txn_max, xnd) for the specific calendar date.

    Returns (None, None) if that date's max is not present in the
    bulletin.
    """
    issue_time = _parse_bulletin_issue_time(block_text)

    if issue_time is None:
        return None, None

    fhr_line = None
    txn_line = None
    xnd_line = None

    for line in block_text.splitlines():
        stripped = line.strip()

        if fhr_line is None and stripped.startswith("FHR"):
            fhr_line = line

        elif txn_line is None and stripped.startswith("TXN"):
            txn_line = line

        elif xnd_line is None and stripped.startswith("XND"):
            xnd_line = line

    if fhr_line is None or txn_line is None:
        return None, None

    fhr_values = extract_row(block_text, "FHR")
    num_cols = len(fhr_values)

    if num_cols == 0:
        return None, None

    txn_by_col = _parse_fixed_width_row(
        txn_line,
        num_cols,
    )

    xnd_by_col = (
        _parse_fixed_width_row(
            xnd_line,
            num_cols,
        )
        if xnd_line
        else [None] * num_cols
    )

    for i, fhr in enumerate(fhr_values):
        val = (
            txn_by_col[i]
            if i < len(txn_by_col)
            else None
        )

        if val is None:
            continue

        valid_dt = issue_time + timedelta(hours=fhr)

        if valid_dt.hour == 0:
            # MAX entry -- belongs to the preceding calendar date.
            max_date = (
                valid_dt - timedelta(days=1)
            ).date()

            if max_date == target_date:
                xnd_val = (
                    xnd_by_col[i]
                    if i < len(xnd_by_col)
                    else None
                )

                return val, xnd_val

    return None, None


def fetch_all_nbm(cycle: str) -> dict:
    """
    Returns:
        {
            station: {
                "TXN": [...],
                "XND": [...],
                "block": raw_text
            }
        }

    TXN/XND arrays are kept for backward compatibility, but should not
    be indexed directly. Use extract_max_for_date() instead.
    """
    raw = fetch_nbm_raw(cycle)
    blocks = split_by_station(
        raw,
        ALL_STATIONS,
    )

    result = {}

    for station in ALL_STATIONS:
        block = blocks.get(station)

        if block:
            result[station] = {
                "TXN": extract_row(
                    block,
                    "TXN",
                ),
                "XND": extract_row(
                    block,
                    "XND",
                ),
                "block": block,
            }

        else:
            result[station] = {
                "TXN": [],
                "XND": [],
                "block": None,
            }

    return result


# ---------------------------------------------------------------------------
# METAR
# ---------------------------------------------------------------------------

def fetch_all_metar() -> dict:
    params = {
        "ids": ",".join(ALL_STATIONS),
        "format": "json",
        "taf": "false",
    }

    resp = requests.get(
        METAR_URL,
        params=params,
        timeout=30,
    )

    resp.raise_for_status()

    by_station = {}

    for ob in resp.json():
        icao = (
            ob.get("icaoId")
            or ob.get("station_id")
        )

        temp_c = ob.get("temp")

        if icao and temp_c is not None:
            by_station[icao] = round(
                temp_c * 9 / 5 + 32,
                1,
            )

    return by_station


# ---------------------------------------------------------------------------
# NWS gridpoint forecast
# ---------------------------------------------------------------------------

def fetch_gridpoint_max_temp_f(
    lat: float,
    lon: float,
) -> float | None:
    try:
        points_resp = requests.get(
            f"https://api.weather.gov/points/{lat},{lon}",
            headers=NWS_HEADERS,
            timeout=20,
        )

        points_resp.raise_for_status()

        forecast_url = (
            points_resp.json()
            ["properties"]
            ["forecast"]
        )

        fc_resp = requests.get(
            forecast_url,
            headers=NWS_HEADERS,
            timeout=20,
        )

        fc_resp.raise_for_status()

        periods = (
            fc_resp.json()
            ["properties"]
            ["periods"]
        )

        for p in periods:
            if p.get("isDaytime"):
                return float(
                    p["temperature"]
                )

        return None

    except Exception as e:
        print(
            f"Gridpoint fetch failed for "
            f"({lat},{lon}): {e}"
        )
        return None


# ---------------------------------------------------------------------------
# Actual observed daily high
#
# IMPORTANT:
#
# For KMIA, this now uses the NWS Daily Climate Report (CLI), which is
# the settlement source used by Polymarket US weather contracts.
#
# The previous version used IEM's computed daily-summary max_temp_f.
# That can differ from the NWS CLI and caused the tracker to mark a
# 92-93F prediction as a loss when the NWS/Polymarket result was 92F.
# ---------------------------------------------------------------------------

_CLI_DATE_RE = re.compile(
    r"THE\s+MIAMI\s+CLIMATE\s+SUMMARY\s+FOR\s+"
    r"([A-Z]+)\s+(\d{1,2})\s+(\d{4})",
    re.IGNORECASE,
)

_CLI_MAX_RE = re.compile(
    r"^\s*MAXIMUM\s+(-?\d+(?:\.\d+)?)",
    re.MULTILINE,
)


def _parse_kmia_cli_max(
    text: str,
    target_date,
):
    """
    Extract the NWS CLI maximum temperature for KMIA.

    The Miami CLI normally contains a section such as:

        ...THE MIAMI CLIMATE SUMMARY FOR SEPTEMBER 16 2026...

        TEMPERATURE (F)

         YESTERDAY
          MAXIMUM         92   12:xx PM ...

    or, in an afternoon report:

         TODAY
          MAXIMUM         92   12:xx PM ...

    The target date in the report heading is used to make sure we
    resolve the correct calendar day.
    """
    if not text:
        return None

    # Verify that the CLI is actually for the requested date.
    date_match = _CLI_DATE_RE.search(text)

    if not date_match:
        return None

    month_name = date_match.group(1).upper()
    day = int(date_match.group(2))
    year = int(date_match.group(3))

    month_lookup = {
        "JANUARY": 1,
        "FEBRUARY": 2,
        "MARCH": 3,
        "APRIL": 4,
        "MAY": 5,
        "JUNE": 6,
        "JULY": 7,
        "AUGUST": 8,
        "SEPTEMBER": 9,
        "OCTOBER": 10,
        "NOVEMBER": 11,
        "DECEMBER": 12,
    }

    month = month_lookup.get(month_name)

    if month is None:
        return None

    try:
        report_date = target_date.__class__(
            year,
            month,
            day,
        )
    except Exception:
        return None

    if report_date != target_date:
        return None

    # Find the TEMPERATURE section first so we don't accidentally
    # match a MAXIMUM from some unrelated part of the product.
    temperature_match = re.search(
        r"TEMPERATURE\s+\(F\)(.*?)(?:PRECIPITATION|DEGREE DAYS|SUNRISE AND SUNSET)",
        text,
        re.IGNORECASE | re.DOTALL,
    )

    if not temperature_match:
        temperature_match = re.search(
            r"TEMPERATURE\s+\(F\)(.*)",
            text,
            re.IGNORECASE | re.DOTALL,
        )

    if not temperature_match:
        return None

    temperature_section = (
        temperature_match.group(1)
    )

    maximum_match = _CLI_MAX_RE.search(
        temperature_section
    )

    if not maximum_match:
        return None

    value = maximum_match.group(1)

    try:
        return float(value)
    except ValueError:
        return None


def _fetch_nws_cli_product_ids(
    station: str,
    target_date,
):
    """
    Find recent NWS CLI products for the station's issuing office.

    We request a small window around the target date and then inspect
    the actual product text to identify the CLI containing the desired
    calendar date.
    """
    config = NWS_CLI_CONFIG.get(station)

    if not config:
        return []

    # Search from the target date through the following day. The
    # finalized overnight CLI is normally issued shortly after
    # midnight/local morning, while an afternoon/evening CLI may also
    # exist for the same day.
    start_dt = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        tzinfo=timezone.utc,
    )

    end_dt = start_dt + timedelta(days=2)

    params = {
        "office": config["office"],
        "type": "CLI",
        "start": start_dt.isoformat().replace(
            "+00:00",
            "Z",
        ),
        "end": end_dt.isoformat().replace(
            "+00:00",
            "Z",
        ),
        "limit": 50,
    }

    try:
        resp = requests.get(
            NWS_PRODUCTS_URL,
            params=params,
            headers=NWS_HEADERS,
            timeout=30,
        )

        resp.raise_for_status()

        data = resp.json()

        return data.get(
            "@graph",
            [],
        )

    except Exception as e:
        print(
            f"NWS CLI product search failed for "
            f"{station} {target_date}: {e}"
        )
        return []


def _fetch_nws_cli_actual_high(
    station: str,
    target_date,
):
    """
    Fetch the NWS CLI maximum temperature for a station/date.

    Returns None when no matching finalized CLI can be found.
    """
    config = NWS_CLI_CONFIG.get(station)

    if not config:
        return None

    products = _fetch_nws_cli_product_ids(
        station,
        target_date,
    )

    if not products:
        print(
            f"No NWS CLI products found for "
            f"{station} {target_date}."
        )
        return None

    # Newest first so that, if multiple CLI reports exist for the
    # same date, we use the latest available report.
    products = sorted(
        products,
        key=lambda product: (
            product.get("issuanceTime")
            or ""
        ),
        reverse=True,
    )

    for product in products:
        product_id = product.get("id")

        if not product_id:
            continue

        # Make sure this is the expected CLI product.
        product_url = (
            f"https://api.weather.gov/products/"
            f"{product_id}"
        )

        try:
            resp = requests.get(
                product_url,
                headers=NWS_HEADERS,
                timeout=30,
            )

            resp.raise_for_status()

            product_data = resp.json()

            text = (
                product_data.get("productText")
                or ""
            )

            actual_high = _parse_kmia_cli_max(
                text,
                target_date,
            )

            if actual_high is not None:
                print(
                    f"NWS CLI actual high for "
                    f"{station} {target_date}: "
                    f"{actual_high}F "
                    f"(product {product_id})"
                )
                return actual_high

        except Exception as e:
            print(
                f"NWS CLI product fetch failed for "
                f"{product_id}: {e}"
            )

    print(
        f"NWS CLI contained no usable maximum for "
        f"{station} {target_date}."
    )

    return None


def fetch_actual_high(
    station: str,
    target_date,
) -> float | None:
    """
    Return the observed daily high used for prediction tracking.

    KMIA:
        Uses the NWS Miami Daily Climate Report (CLI), matching the
        settlement source used by Polymarket US weather contracts.

    Other stations:
        Retains the existing IEM daily-summary behavior unless an
        NWS CLI configuration is added for that station.
    """
    # KMIA is the station used by the current prediction tracker.
    # Use NWS CLI so the tracker is aligned with the settlement source.
    if station in NWS_CLI_CONFIG:
        actual_high = _fetch_nws_cli_actual_high(
            station,
            target_date,
        )

        if actual_high is not None:
            return actual_high

        # IMPORTANT:
        # Do not silently fall back to the IEM computed daily high for
        # KMIA. Doing so could recreate the exact settlement mismatch
        # this function is intended to prevent.
        return None

    # ------------------------------------------------------------------
    # Legacy IEM fallback for stations that do not yet have an NWS CLI
    # configuration.
    # ------------------------------------------------------------------

    network = STATION_NETWORK.get(station)

    if not network:
        print(
            f"No IEM network mapping for station "
            f"{station}, skipping actual-high fetch."
        )
        return None

    # IEM station tables use the 3-character FAA identifier.
    iem_station = (
        station[1:]
        if len(station) == 4
        and station.startswith("K")
        else station
    )

    date_str = (
        target_date.isoformat()
        if hasattr(target_date, "isoformat")
        else str(target_date)
    )

    params = {
        "stations": iem_station,
        "network": network,
        "sts": date_str,
        "ets": date_str,
        "var": "max_temp_f",
        "format": "csv",
    }

    try:
        resp = requests.get(
            IEM_DAILY_URL,
            params=params,
            timeout=30,
        )

        resp.raise_for_status()

        lines = [
            line
            for line in resp.text.strip().splitlines()
            if line
            and not line.startswith("#")
        ]

        if len(lines) < 2:
            print(
                f"IEM returned no data row for "
                f"{iem_station} ({network}) on {date_str}. "
                f"Raw response: "
                f"{resp.text[:200]!r}"
            )
            return None

        header = lines[0].split(",")
        row = lines[1].split(",")

        idx = header.index("max_temp_f")
        val = row[idx].strip()

        if val in ("", "M", "None"):
            print(
                f"IEM has no max_temp_f value for "
                f"{iem_station} on {date_str} "
                f"(got {val!r})."
            )
            return None

        return float(val)

    except Exception as e:
        print(
            f"Actual-high fetch failed for "
            f"{iem_station} {date_str}: {e}"
        )
        return None


# ---------------------------------------------------------------------------
# Polymarket (Gamma API) — website-resolution market
# ---------------------------------------------------------------------------

def build_event_slug(
    city_slug: str,
    target_date: datetime,
) -> str:
    month = target_date.strftime("%B").lower()
    day = target_date.day
    year = target_date.year

    return (
        f"highest-temperature-in-{city_slug}-on-"
        f"{month}-{day}-{year}"
    )


def fetch_market_by_slug(
    slug: str,
) -> dict | None:
    try:
        resp = requests.get(
            GAMMA_EVENTS_URL,
            params={"slug": slug},
            timeout=20,
        )

        resp.raise_for_status()

        data = resp.json()

        if isinstance(data, list) and data:
            return data[0]

        return None

    except Exception as e:
        print(
            f"Gamma fetch failed for slug={slug}: {e}"
        )
        return None


BUCKET_RE = re.compile(
    r"(\d+)\s*-\s*(\d+)"
)


def parse_outcomes(event: dict):
    """
    Polymarket structures a multi-bucket temperature event as one
    event containing multiple markets.
    """
    if not event or not event.get("markets"):
        return []

    parsed = []

    for market in event["markets"]:
        label_source = (
            market.get("groupItemTitle")
            or market.get("question", "")
        )

        m = BUCKET_RE.search(label_source)

        if not m:
            continue

        lo = float(m.group(1))
        hi = float(m.group(2))

        outcomes = market.get("outcomes")
        prices = market.get("outcomePrices")

        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        if isinstance(prices, str):
            prices = json.loads(prices)

        if not outcomes or not prices:
            continue

        yes_price = None

        for o, p in zip(outcomes, prices):
            if str(o).strip().lower() == "yes":
                yes_price = float(p)
                break

        if yes_price is None:
            yes_price = float(prices[0])

        parsed.append(
            (
                label_source,
                lo,
                hi,
                yes_price,
            )
        )

    return parsed


def find_bucket_for_temp(
    outcomes,
    temp_f: float,
):
    """
    Returns the narrowest matching bucket.
    """
    matches = [
        (
            label,
            lo,
            hi,
            price,
        )
        for label, lo, hi, price in outcomes
        if lo <= temp_f <= hi
    ]

    if not matches:
        return None

    return min(
        matches,
        key=lambda m: m[2] - m[1],
    )


# ---------------------------------------------------------------------------
# Polymarket US (the app)
# ---------------------------------------------------------------------------

POLYMARKET_US_GATEWAY = (
    "https://gateway.polymarket.us"
)

# Matches phrasing such as:
# "between 92F and 93F"
_US_RANGE_RE = re.compile(
    r"between (\d+)F and (\d+)F"
)

_US_LTE_RE = re.compile(
    r"less than or equal to (\d+)F"
)

_US_GTE_RE = re.compile(
    r"greater than or equal to (\d+)F"
)


def build_polymarket_us_slug(
    us_station_slug: str,
    target_date,
) -> str:
    date_str = (
        target_date.isoformat()
        if hasattr(target_date, "isoformat")
        else str(target_date)
    )

    return (
        f"temp-{us_station_slug}high-{date_str}"
    )


def fetch_polymarket_us_event(
    slug: str,
) -> dict | None:
    try:
        resp = requests.get(
            f"{POLYMARKET_US_GATEWAY}/v1/events/slug/{slug}",
            timeout=20,
        )

        if resp.status_code == 404:
            return None

        resp.raise_for_status()

        return resp.json().get("event")

    except Exception as e:
        print(
            f"Polymarket US event fetch failed "
            f"for slug={slug}: {e}"
        )
        return None


def parse_polymarket_us_outcomes(
    event: dict,
):
    """
    Same return shape as parse_outcomes():

        (label, low_f, high_f, yes_price)

    Open-ended buckets use wide sentinel bounds internally.
    """
    if not event or not event.get("markets"):
        return []

    parsed = []

    for market in event["markets"]:
        desc = market.get(
            "description",
            "",
        )

        m = _US_RANGE_RE.search(desc)

        if m:
            lo = float(m.group(1))
            hi = float(m.group(2))

        else:
            m = _US_LTE_RE.search(desc)

            if m:
                lo = -200.0
                hi = float(m.group(1))

            else:
                m = _US_GTE_RE.search(desc)

                if m:
                    lo = float(m.group(1))
                    hi = 300.0

                else:
                    continue

        yes_price = None

        for side in market.get(
            "marketSides",
            [],
        ):
            if (
                side.get(
                    "description",
                    "",
                )
                .strip()
                .lower()
                == "yes"
            ):
                try:
                    yes_price = float(
                        side["price"]
                    )
                except (
                    KeyError,
                    ValueError,
                    TypeError,
                ):
                    pass

                break

        if yes_price is None:
            continue

        if lo > -200 and hi < 300:
            label = (
                f"{lo:.0f}-{hi:.0f}°F"
            )

        elif hi < 300:
            label = (
                f"≤{hi:.0f}°F"
            )

        else:
            label = (
                f"≥{lo:.0f}°F"
            )

        parsed.append(
            (
                label,
                lo,
                hi,
                yes_price,
            )
        )

    return parsed