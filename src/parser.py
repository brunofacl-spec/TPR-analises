"""
parser.py - Parse network (.xlsm/.xlsx) and execution (.xls/.xlsx/.csv) files
for the transport network delay analysis application.
"""
from __future__ import annotations

import io
import re
import datetime
import logging
from typing import Optional, Union

import pandas as pd
import openpyxl

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reverse logistics detection
# ---------------------------------------------------------------------------

_REVERSE_KEYWORDS = [
    "vazi",          # covers: vazio, vazia, vazias, contentores vazios, paletes vazias
    "retorno",
    "inversa",       # covers: logistica inversa, logística inversa
    "empty",
    "paletes v",
    "contentores v",
]

_REVERSE_NAME_TOKENS = ["rib", "rb"]   # checked as whole word / suffix in route name


def detect_reverse_logistics(route: dict) -> bool:
    """Return True when the route is a reverse-logistics (empty return) leg.

    Detection criteria (any one sufficient):
    1. Route name (designacao) or observation (obs) contain a reverse keyword.
    2. Route name contains 'RIB' or ends with 'RB' as a token.
    3. Last stop has the same stop-code / name as the first stop (round-trip).
    """
    name = (route.get("designacao") or "").lower()
    obs  = (route.get("obs") or "").lower()

    # Keyword match in name or obs
    if any(k in name or k in obs for k in _REVERSE_KEYWORDS):
        return True

    # Token match for RIB / RB in name
    for token in _REVERSE_NAME_TOKENS:
        # Match as whole word boundaries using simple split check
        if token in name.split() or name.endswith(f"-{token}") or name.endswith(f" {token}"):
            return True

    # Round-trip: last stop == first stop (by paragem name)
    stops = route.get("stops", [])
    if len(stops) >= 2:
        first_name = (stops[0].get("paragem") or "").strip().lower()
        last_name  = (stops[-1].get("paragem") or "").strip().lower()
        if first_name and first_name == last_name:
            return True

    return False


def find_reverse_leg_from(route: dict) -> Optional[int]:
    """Return the index of the stop where the return leg begins, or None.

    For a round-trip route the return point is the stop with the highest km
    (turnaround point) — heuristically the middle stop for simple out-and-back
    routes.  Returns None when the route is not a round-trip.
    """
    stops = route.get("stops", [])
    if len(stops) < 2:
        return None

    first_name = (stops[0].get("paragem") or "").strip().lower()
    last_name  = (stops[-1].get("paragem") or "").strip().lower()
    if not (first_name and first_name == last_name):
        return None

    # Find the stop with maximum accumulated km (turnaround)
    max_km = -1
    max_idx = len(stops) // 2  # fallback: midpoint
    for i, s in enumerate(stops):
        km = s.get("km")
        if km is not None:
            try:
                km_f = float(km)
                if km_f > max_km:
                    max_km = km_f
                    max_idx = i
            except (ValueError, TypeError):
                pass

    return max_idx if max_idx > 0 else None


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _time_to_minutes(t) -> Optional[int]:
    """Convert a time value to minutes since midnight.

    Accepts:
    - datetime.time objects
    - HH:MM strings
    - HH:MM:SS strings
    - None / empty → None
    """
    if t is None:
        return None
    if isinstance(t, datetime.time):
        return t.hour * 60 + t.minute
    if isinstance(t, (int, float)):
        # Might be an Excel serial or already minutes – ignore
        return None
    s = str(t).strip()
    if not s or s in ("00:00", "0:00"):
        return 0
    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def _fix_overnight(stops: list[dict]) -> list[dict]:
    """Adjust times for routes that cross midnight.

    If a departure time at stop N is less than the arrival time at stop N-1
    we assume midnight was crossed and add 1440 (24h in minutes).
    """
    if not stops:
        return stops

    prev_dep = stops[0].get("hpp")
    if prev_dep is None:
        prev_dep = stops[0].get("hpc") or 0

    for stop in stops[1:]:
        for key in ("hpc", "hpp"):
            val = stop.get(key)
            if val is not None and val < prev_dep - 30:
                stop[key] = val + 1440
        dep = stop.get("hpp")
        if dep is not None:
            prev_dep = dep
        elif stop.get("hpc") is not None:
            prev_dep = stop["hpc"]

    return stops


# ---------------------------------------------------------------------------
# Network file parser (Encadeamento_Rede_Transportes.xlsx / Rede_Transportes_Base_YYYYMMDD.xlsm)
# ---------------------------------------------------------------------------

# Sheets that contain route stop data (detail sheets)
_DETAIL_SHEET_PREFIXES = ("R1", "R2", "R3")
_SUMMARY_SHEET = "Resumo Geral"

# Columns expected in detail sheets (positional, 0-indexed)
# Col 0: Carreira, 1: Designação, 2: Transportador, 3: Viatura,
# 4: Periodicidade, 5: N.ºPar, 6: Paragem, 7: H.Chegada, 8: H.Partida,
# 9: T.Paragem(min), 10: T.Trânsito(min), 11: Km Acum., 12: Tipo

_COL_CARREIRA = 0
_COL_DESIG = 1
_COL_TRANSP = 2
_COL_VIATURA = 3
_COL_PERIOD = 4
_COL_NPAR = 5
_COL_PARAGEM = 6
_COL_HCHEG = 7
_COL_HPART = 8
_COL_TPAR = 9
_COL_TTRAN = 10
_COL_KM = 11
_COL_TIPO = 12


def _is_header_row(row) -> bool:
    """Return True if this is the column-header row (not a data row)."""
    return str(row[0]).strip() in ("Carreira", "[")


def _is_route_title_row(row) -> bool:
    """A block title row starts with '[' in col 0 and has None in all others."""
    val = str(row[0]).strip() if row[0] is not None else ""
    return val.startswith("[") and all(row[i] is None for i in range(1, min(len(row), 5)))


def _is_separator_row(row) -> bool:
    return all(v is None for v in row)


def _infer_rede_from_sheet(sheet_name: str) -> str:
    """Infer the 'rede' type (R1/R2/R3) from the sheet name."""
    if sheet_name.startswith("R3"):
        return "R3"
    if sheet_name.startswith("R2"):
        return "R2"
    return "R1"


def _infer_region_from_sheet(sheet_name: str) -> str:
    """Best-effort region from sheet name suffix."""
    mapping = {
        "EIB": "EIB",
        "Exp": "Exp",
        "MAR": "MAR",
        "OIR": "OIR",
        "RB": "RB",
        "RIB": "RIB",
        "Clientes": "Clientes",
    }
    for k, v in mapping.items():
        if k in sheet_name:
            return v
    return "Nacional"


def _parse_detail_sheet(ws, sheet_name: str) -> list[dict]:
    """Parse one detail sheet and return a list of route dicts."""
    rede = _infer_rede_from_sheet(sheet_name)
    region = _infer_region_from_sheet(sheet_name)

    routes: list[dict] = []
    current: Optional[dict] = None

    for row_raw in ws.iter_rows(min_row=1, values_only=True):
        row = list(row_raw)
        # Pad short rows
        while len(row) < 13:
            row.append(None)

        if _is_header_row(row):
            continue

        if _is_route_title_row(row):
            # Save previous route
            if current and current.get("stops"):
                current["stops"] = _fix_overnight(current["stops"])
                routes.append(current)
            current = None
            continue

        if _is_separator_row(row):
            if current and current.get("stops"):
                current["stops"] = _fix_overnight(current["stops"])
                routes.append(current)
            current = None
            continue

        # Data row – col 0 should be numeric carreira code
        carreira_raw = row[_COL_CARREIRA]
        if carreira_raw is None:
            continue

        try:
            carreira = int(carreira_raw)
        except (ValueError, TypeError):
            continue

        # First data row of a new route – initialise the dict
        if current is None or current.get("carreira") != carreira:
            if current and current.get("stops"):
                current["stops"] = _fix_overnight(current["stops"])
                routes.append(current)
            current = {
                "carreira": carreira,
                "designacao": str(row[_COL_DESIG] or "").strip(),
                "transportador": str(row[_COL_TRANSP] or "").strip(),
                "veiculo": str(row[_COL_VIATURA] or "").strip(),
                "periodicidade": str(row[_COL_PERIOD] or "").strip(),
                "rede": rede,
                "regiao": region,
                "stops": [],
            }

        hpc = _time_to_minutes(row[_COL_HCHEG])
        hpp = _time_to_minutes(row[_COL_HPART])

        try:
            npar = int(row[_COL_NPAR]) if row[_COL_NPAR] is not None else None
        except (ValueError, TypeError):
            npar = None

        try:
            tp = float(row[_COL_TPAR]) if row[_COL_TPAR] is not None else None
        except (ValueError, TypeError):
            tp = None

        try:
            tt = float(row[_COL_TTRAN]) if row[_COL_TTRAN] is not None else None
        except (ValueError, TypeError):
            tt = None

        try:
            km = float(row[_COL_KM]) if row[_COL_KM] is not None else None
        except (ValueError, TypeError):
            km = None

        stop = {
            "paragem": str(row[_COL_PARAGEM] or "").strip(),
            "n_par": npar,
            "hpc": hpc,
            "hpp": hpp,
            "tp": tp,
            "tt": tt,
            "km": km,
            "tipo": str(row[_COL_TIPO] or "").strip(),
        }
        current["stops"].append(stop)

    # Flush last route
    if current and current.get("stops"):
        current["stops"] = _fix_overnight(current["stops"])
        routes.append(current)

    return routes


def _parse_summary_sheet(ws) -> dict[int, dict]:
    """Parse the Resumo Geral sheet → {carreira: summary_dict}."""
    summary: dict[int, dict] = {}
    headers = None
    for row in ws.iter_rows(min_row=1, values_only=True):
        if headers is None:
            headers = [str(c or "").strip() for c in row]
            continue
        if row[0] is None:
            continue
        try:
            carreira = int(row[0])
        except (ValueError, TypeError):
            continue
        record: dict = {}
        for i, h in enumerate(headers):
            if i < len(row):
                record[h] = row[i]
        summary[carreira] = record
    return summary


def parse_network_xlsm(file) -> dict:
    """Parse the network .xlsm/.xlsx file.

    Parameters
    ----------
    file : file-like or path
        The uploaded network workbook.

    Returns
    -------
    dict with keys:
        'routes'      : list of route dicts
        'stops_index' : dict {stop_name -> [carreira, ...]}
        'summary'     : dict {carreira -> summary record}
    """
    try:
        wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
    except Exception as exc:
        logger.error("Failed to open network file: %s", exc)
        raise

    summary: dict[int, dict] = {}
    if _SUMMARY_SHEET in wb.sheetnames:
        try:
            summary = _parse_summary_sheet(wb[_SUMMARY_SHEET])
        except Exception as exc:
            logger.warning("Could not parse summary sheet: %s", exc)

    routes: list[dict] = []
    for sheet_name in wb.sheetnames:
        if not any(sheet_name.startswith(p) for p in _DETAIL_SHEET_PREFIXES):
            continue
        try:
            ws = wb[sheet_name]
            sheet_routes = _parse_detail_sheet(ws, sheet_name)
            routes.extend(sheet_routes)
        except Exception as exc:
            logger.warning("Error parsing sheet %s: %s", sheet_name, exc)

    # Enrich routes from summary
    for route in routes:
        carreira = route["carreira"]
        if carreira in summary:
            rec = summary[carreira]
            if not route.get("regiao") or route["regiao"] == "Nacional":
                route["regiao"] = str(rec.get("Região", route["regiao"]) or route["regiao"])
            if not route.get("designacao"):
                route["designacao"] = str(rec.get("Designação", "") or "")
            route["origem"] = str(rec.get("Origem", "") or "")
            route["destino"] = str(rec.get("Destino", "") or "")
            # Rede from summary overrides sheet inference when available
            rede_sum = str(rec.get("Rede", "") or "").strip()
            if rede_sum in ("R1", "R2", "R3"):
                route["rede"] = rede_sum
        else:
            route.setdefault("origem", route["stops"][0]["paragem"] if route["stops"] else "")
            route.setdefault("destino", route["stops"][-1]["paragem"] if route["stops"] else "")

    # Tag reverse-logistics routes
    for route in routes:
        route["is_reverse_logistics"] = detect_reverse_logistics(route)
        route["reverse_leg_from"] = find_reverse_leg_from(route) if route["is_reverse_logistics"] else None

    # Build stops index: paragem name → list of carreira codes
    stops_index: dict[str, list[int]] = {}
    for route in routes:
        carreira = route["carreira"]
        for stop in route["stops"]:
            name = stop["paragem"]
            if name:
                stops_index.setdefault(name, []).append(carreira)

    return {
        "routes": routes,
        "stops_index": stops_index,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Execution file parser
# ---------------------------------------------------------------------------

# Column name normalization map (Portuguese execution file headers → internal names)
_EXEC_COL_MAP = {
    "Carreira": "carreira",
    "Trajeto": "trajeto",
    "Ponto": "ponto",
    "Ligação": "ligacao",
    "Viatura": "viatura",
    "Ocupação": "ocupacao",
    "Rede": "rede",
    "C/P": "cp",
    "Dia": "dia",
    "Previsto": "previsto",
    "Real": "real",
    "Atraso": "atraso",
    "Anomalia": "anomalia",
    "Causa": "causa",
    "Responsabilidade": "responsabilidade",
    "Designação paragem": "designacao_paragem",
    "TP/RE": "tp_re",
    "Ordem": "ordem",
    "Real APL": "real_apl",
    "Real Telemetria": "real_telemetria",
    "Obs.": "obs",
    "Nº Portal": "n_portal",
    # Alternate spellings / case variations
    "Designação Paragem": "designacao_paragem",
    "Ligacao": "ligacao",
    "Ocupacao": "ocupacao",
}


def _parse_delay_value(val) -> Optional[float]:
    """Parse a delay value to float minutes.

    Handles:
    - numeric (int/float)
    - strings like "30", "30 minutos", "30 min", "+30", "-5"
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    # Remove non-numeric suffixes
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)", s)
    if m:
        return float(m.group(1))
    return None


def parse_execution_file(file) -> pd.DataFrame:
    """Parse an execution file (.xls, .xlsx, or .csv).

    Returns a normalised DataFrame with internal column names.
    """
    # Determine format from filename if possible
    fname = getattr(file, "name", "")
    if isinstance(fname, str):
        fname_lower = fname.lower()
    else:
        fname_lower = ""

    df: Optional[pd.DataFrame] = None
    errors = []

    # Try xls first (old Excel format)
    if fname_lower.endswith(".xls") and not fname_lower.endswith(".xlsx"):
        try:
            df = pd.read_excel(file, engine="xlrd")
        except Exception as exc:
            errors.append(f"xlrd: {exc}")

    if df is None:
        # Try openpyxl engine for xlsx
        try:
            if hasattr(file, "seek"):
                file.seek(0)
            df = pd.read_excel(file, engine="openpyxl")
        except Exception as exc:
            errors.append(f"openpyxl: {exc}")

    if df is None:
        try:
            if hasattr(file, "seek"):
                file.seek(0)
            df = pd.read_csv(file, sep=None, engine="python", encoding="utf-8-sig")
        except Exception as exc:
            errors.append(f"csv: {exc}")

    if df is None:
        raise ValueError(f"Could not parse execution file. Errors: {'; '.join(errors)}")

    # Rename columns to internal names
    rename_map = {}
    for orig_col in df.columns:
        key = str(orig_col).strip()
        if key in _EXEC_COL_MAP:
            rename_map[orig_col] = _EXEC_COL_MAP[key]
    df = df.rename(columns=rename_map)

    # Ensure required columns exist (add NaN columns if missing)
    for internal in _EXEC_COL_MAP.values():
        if internal not in df.columns:
            df[internal] = None

    # Parse numeric delay column
    df["atraso_min"] = df["atraso"].apply(_parse_delay_value)

    # Normalise carreira to string for display
    df["carreira_str"] = df["carreira"].apply(lambda x: str(int(x)) if pd.notna(x) else "")

    return df
