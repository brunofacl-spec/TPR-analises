"""
parser.py - Parse network (.xlsm/.xlsx) and execution (.xls/.xlsx/.csv) files.

Supports TWO network file formats:
  A) Original weekly .xlsm  → sheets: 'Horários', 'Resumo', 'Rotas'
     (Rede_Transportes_Base_YYYYMMDD.xlsm)
  B) Generated summary .xlsx → sheets named 'R1', 'R1_Exp', 'R2', ...
     (Encadeamento_Rede_Transportes.xlsx)
"""
from __future__ import annotations

import io
import re
import datetime
import logging
from typing import Optional

import pandas as pd
import openpyxl

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reverse logistics detection
# ---------------------------------------------------------------------------

_REVERSE_KEYWORDS = [
    "vazi", "retorno", "inversa", "empty", "paletes v", "contentores v",
]
_REVERSE_NAME_TOKENS = ["rib", "rb"]


def detect_reverse_logistics(route: dict) -> bool:
    name = (route.get("designacao") or "").lower()
    obs  = (route.get("obs") or "").lower()
    if any(k in name or k in obs for k in _REVERSE_KEYWORDS):
        return True
    for token in _REVERSE_NAME_TOKENS:
        if token in name.split() or name.endswith(f"-{token}") or name.endswith(f" {token}"):
            return True
    stops = route.get("stops", [])
    if len(stops) >= 2:
        first = (stops[0].get("paragem") or "").strip().lower()
        last  = (stops[-1].get("paragem") or "").strip().lower()
        if first and first == last:
            return True
    return False


def find_reverse_leg_from(route: dict) -> Optional[int]:
    stops = route.get("stops", [])
    if len(stops) < 2:
        return None
    first = (stops[0].get("paragem") or "").strip().lower()
    last  = (stops[-1].get("paragem") or "").strip().lower()
    if not (first and first == last):
        return None
    max_km, max_idx = -1, len(stops) // 2
    for i, s in enumerate(stops):
        try:
            km_f = float(s.get("km") or 0)
            if km_f > max_km:
                max_km, max_idx = km_f, i
        except (ValueError, TypeError):
            pass
    return max_idx if max_idx > 0 else None


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _time_to_minutes(t) -> Optional[int]:
    if t is None:
        return None
    if isinstance(t, datetime.time):
        return t.hour * 60 + t.minute
    if isinstance(t, datetime.datetime):
        return t.hour * 60 + t.minute
    if isinstance(t, (int, float)):
        return None
    s = str(t).strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def _fix_overnight(stops: list[dict]) -> list[dict]:
    if not stops:
        return stops
    prev = stops[0].get("hpp") or stops[0].get("hpc") or 0
    for stop in stops[1:]:
        for key in ("hpc", "hpp"):
            val = stop.get(key)
            if val is not None and val < prev - 30:
                stop[key] = val + 1440
        dep = stop.get("hpp") or stop.get("hpc")
        if dep is not None:
            prev = dep
    return stops


def _fmt_minutes(minutes: Optional[int]) -> str:
    if minutes is None:
        return ""
    h, m = divmod(int(minutes) % 1440, 60)
    return f"{h:02d}:{m:02d}"


# ---------------------------------------------------------------------------
# FORMAT A — Original weekly .xlsm  (sheet: 'Horários')
# ---------------------------------------------------------------------------

def _parse_horarios_sheet(ws, resumo_dict: dict) -> list[dict]:
    """Parse the 'Horários' sheet from the original weekly .xlsm."""
    routes: list[dict] = []
    current: Optional[dict] = None

    for row_raw in ws.iter_rows(values_only=True):
        row = list(row_raw)
        while len(row) < 13:
            row.append(None)

        v0 = row[0]

        # --- Block header rows ---
        if v0 == "Carreira":
            # Save previous
            if current and current.get("stops"):
                current["stops"] = _fix_overnight(current["stops"])
                routes.append(current)
            # Extract carreira code from col 1
            cod = row[1]
            try:
                cod = int(float(str(cod).strip()))
            except (ValueError, TypeError):
                cod = str(cod).strip() if cod else None
            # Look up metadata from Resumo
            meta = resumo_dict.get(cod, {})
            current = {
                "carreira":      cod,
                "designacao":    None,
                "periodicidade": None,
                "transportador": None,
                "veiculo":       None,
                "rede":          str(meta.get("rede", "") or "").strip(),
                "regiao":        str(meta.get("regiao", "") or "").strip(),
                "origem":        str(meta.get("origem", "") or "").strip(),
                "destino":       str(meta.get("destino", "") or "").strip(),
                "stops":         [],
            }
            continue

        if current is None:
            continue

        if v0 == "Designação":
            current["designacao"] = str(row[1] or "").strip()
            continue
        if v0 == "Periodicidade":
            current["periodicidade"] = str(row[1] or "").strip()
            continue
        if v0 == "Transportador":
            current["transportador"] = str(row[1] or "").strip()
            continue
        if v0 == "Viatura":
            current["veiculo"] = str(row[1] or "").strip()
            continue
        if v0 in ("Código", "Codigo", "Código "):
            continue  # column header row

        # Empty row = block separator
        if all(v is None for v in row):
            if current and current.get("stops"):
                current["stops"] = _fix_overnight(current["stops"])
                routes.append(current)
            current = None
            continue

        # Numeric first col = stop data row
        # Format: [stop_code, stop_name, n_par, hpc, hpp, tp, tt, km, vm, ...]
        try:
            int(float(str(v0)))
        except (ValueError, TypeError):
            continue

        stop_name = str(row[1] or "").strip()
        try:
            n_par = int(row[2]) if row[2] is not None else None
        except (ValueError, TypeError):
            n_par = None

        hpc = _time_to_minutes(row[3])
        hpp = _time_to_minutes(row[4])

        try:
            tp = float(row[5]) if row[5] is not None else None
        except (ValueError, TypeError):
            tp = None
        try:
            tt = float(row[6]) if row[6] is not None else None
        except (ValueError, TypeError):
            tt = None
        try:
            km = float(row[7]) if row[7] is not None else None
        except (ValueError, TypeError):
            km = None

        current["stops"].append({
            "paragem": stop_name,
            "n_par":   n_par,
            "hpc":     hpc,
            "hpp":     hpp,
            "tp":      tp,
            "tt":      tt,
            "km":      km,
            "tipo":    "",
        })

    # Flush last
    if current and current.get("stops"):
        current["stops"] = _fix_overnight(current["stops"])
        routes.append(current)

    return routes


def _parse_resumo_sheet(ws) -> dict:
    """Parse 'Resumo' sheet → {carreira_int: meta_dict}."""
    result = {}
    headers = None
    for row in ws.iter_rows(values_only=True):
        if headers is None:
            headers = [str(c or "").strip() for c in row]
            continue
        if not row[0]:
            continue
        try:
            car = int(float(str(row[0])))
        except (ValueError, TypeError):
            continue
        meta = {}
        for i, h in enumerate(headers):
            if i < len(row):
                meta[h] = row[i]
        # Normalise keys we care about
        result[car] = {
            "rede":    str(meta.get("Rede", "") or "").strip(),
            "regiao":  str(meta.get("Região", "") or "").strip(),
            "veiculo": str(meta.get("Veículo", "") or "").strip(),
            "km":      meta.get("Km"),
            "origem":  str(meta.get("GE Origem", "") or "").strip(),
            "destino": str(meta.get("GE Destino", "") or "").strip(),
        }
    return result


def _parse_rotas_sheet(ws) -> dict:
    """Parse 'Rotas' sheet → {carreira_int: rota_dict} with origem/destino."""
    result = {}
    headers = None
    for row in ws.iter_rows(values_only=True):
        if headers is None:
            headers = [str(c or "").strip() for c in row]
            continue
        if not row[0]:
            continue
        try:
            car = int(float(str(row[0])))
        except (ValueError, TypeError):
            continue
        meta = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}
        result[car] = {
            "origem":      str(meta.get("Local de Origem", "") or "").strip(),
            "destino":     str(meta.get("Local de Destino", "") or "").strip(),
            "intermedios": str(meta.get("Locais Intermédios", "") or "").strip(),
        }
    return result


def _parse_format_a(wb) -> list[dict]:
    """Parse original .xlsm using the literal sheet name 'Horários'."""
    return _parse_format_a_named(wb, "Horários")


def _parse_format_a_named(wb, horarios_sheet_name: str) -> list[dict]:
    """Parse original .xlsm with an explicit Horários sheet name."""
    # Build resumo lookup
    resumo = {}
    if "Resumo" in wb.sheetnames:
        try:
            resumo = _parse_resumo_sheet(wb["Resumo"])
            logger.info("Resumo: %d entries", len(resumo))
        except Exception as e:
            logger.warning("Resumo sheet error: %s", e)

    rotas = {}
    if "Rotas" in wb.sheetnames:
        try:
            rotas = _parse_rotas_sheet(wb["Rotas"])
            logger.info("Rotas: %d entries", len(rotas))
        except Exception as e:
            logger.warning("Rotas sheet error: %s", e)

    routes = []
    if horarios_sheet_name and horarios_sheet_name in wb.sheetnames:
        try:
            routes = _parse_horarios_sheet(wb[horarios_sheet_name], resumo)
            logger.info("Horários (%r): %d routes parsed", horarios_sheet_name, len(routes))
        except Exception as e:
            logger.error("Horários sheet error: %s", e, exc_info=True)
            raise
    else:
        logger.error("Sheet %r not found in workbook. Available: %s", horarios_sheet_name, wb.sheetnames)

    # Merge rotas origem/destino when not from resumo
    for r in routes:
        car = r["carreira"]
        if car in rotas:
            rt = rotas[car]
            if not r.get("origem"):
                r["origem"] = rt["origem"]
            if not r.get("destino"):
                r["destino"] = rt["destino"]
        # Fallback to first/last stop
        if not r.get("origem") and r["stops"]:
            r["origem"] = r["stops"][0]["paragem"]
        if not r.get("destino") and r["stops"]:
            r["destino"] = r["stops"][-1]["paragem"]

    return routes


# ---------------------------------------------------------------------------
# FORMAT B — Generated summary .xlsx (sheets: R1, R1_Exp, R2, ...)
# ---------------------------------------------------------------------------

_GEN_DETAIL_PREFIXES = ("R1", "R2", "R3")
_GEN_SUMMARY_SHEET   = "Resumo Geral"

# Column positions in generated sheets (0-indexed)
_G = dict(
    carreira=0, desig=1, transp=2, viatura=3, period=4,
    npar=5, paragem=6, hcheg=7, hpart=8, tpar=9, ttran=10, km=11, tipo=12,
)


def _parse_format_b_sheet(ws, sheet_name: str) -> list[dict]:
    rede = "R3" if sheet_name.startswith("R3") else ("R2" if sheet_name.startswith("R2") else "R1")
    routes: list[dict] = []
    current: Optional[dict] = None

    for row_raw in ws.iter_rows(min_row=2, values_only=True):
        row = list(row_raw)
        while len(row) < 13:
            row.append(None)

        v0 = row[0]
        # Skip header and separator rows
        if v0 is None or str(v0).strip() in ("Carreira", ""):
            continue
        # Block title row: starts with '['
        if isinstance(v0, str) and v0.strip().startswith("["):
            if current and current.get("stops"):
                current["stops"] = _fix_overnight(current["stops"])
                routes.append(current)
            current = None
            continue
        # All-None = separator
        if all(v is None for v in row):
            if current and current.get("stops"):
                current["stops"] = _fix_overnight(current["stops"])
                routes.append(current)
            current = None
            continue

        try:
            carreira = int(float(str(v0)))
        except (ValueError, TypeError):
            continue

        if current is None or current.get("carreira") != carreira:
            if current and current.get("stops"):
                current["stops"] = _fix_overnight(current["stops"])
                routes.append(current)
            current = {
                "carreira":      carreira,
                "designacao":    str(row[_G["desig"]] or "").strip(),
                "transportador": str(row[_G["transp"]] or "").strip(),
                "veiculo":       str(row[_G["viatura"]] or "").strip(),
                "periodicidade": str(row[_G["period"]] or "").strip(),
                "rede":          rede,
                "regiao":        "",
                "origem":        "",
                "destino":       "",
                "stops":         [],
            }

        try:
            npar = int(row[_G["npar"]]) if row[_G["npar"]] is not None else None
        except (ValueError, TypeError):
            npar = None

        hpc = _time_to_minutes(row[_G["hcheg"]])
        hpp = _time_to_minutes(row[_G["hpart"]])

        def _safe_float(v):
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        stop = {
            "paragem": str(row[_G["paragem"]] or "").strip(),
            "n_par":   npar,
            "hpc":     hpc,
            "hpp":     hpp,
            "tp":      _safe_float(row[_G["tpar"]]),
            "tt":      _safe_float(row[_G["ttran"]]),
            "km":      _safe_float(row[_G["km"]]),
            "tipo":    str(row[_G["tipo"]] or "").strip(),
        }
        current["stops"].append(stop)

    if current and current.get("stops"):
        current["stops"] = _fix_overnight(current["stops"])
        routes.append(current)

    return routes


def _parse_format_b(wb) -> list[dict]:
    routes = []
    for sname in wb.sheetnames:
        if any(sname.startswith(p) for p in _GEN_DETAIL_PREFIXES) and sname != _GEN_SUMMARY_SHEET:
            try:
                routes.extend(_parse_format_b_sheet(wb[sname], sname))
            except Exception as e:
                logger.warning("Sheet %s error: %s", sname, e)
    return routes


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_network_xlsm(file) -> dict:
    """Parse a network file (original .xlsm OR generated .xlsx).

    Auto-detects format from sheet names.

    Returns
    -------
    dict:
        routes      : list of route dicts
        stops_index : {stop_name -> [carreira, ...]}
        summary     : {carreira -> meta dict}
    """
    # Always read to bytes first — avoids stream-position issues with
    # Streamlit UploadedFile and ensures openpyxl gets a seekable buffer.
    try:
        if hasattr(file, "read"):
            raw = file.read()
        else:
            with open(file, "rb") as fh:
                raw = fh.read()
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        logger.error("Failed to open network file: %s", exc)
        raise

    sheetnames = set(wb.sheetnames)
    logger.info("FILE OPENED — sheets: %s", sorted(sheetnames))

    # Detect format — check for 'Horários' with and without accent variants
    horarios_name = next(
        (s for s in wb.sheetnames if s.lower().startswith("hor") and "rio" in s.lower()),
        None,
    )
    is_format_a = horarios_name is not None
    is_format_b = (
        any(s.startswith(p) for s in sheetnames for p in _GEN_DETAIL_PREFIXES)
        and not is_format_a
    )

    logger.info(
        "Format detection: is_format_a=%s (sheet=%r), is_format_b=%s",
        is_format_a, horarios_name, is_format_b,
    )

    if is_format_a:
        # Patch wb so _parse_format_a finds the sheet by its actual name
        _orig_horarios = "Horários"
        if horarios_name != _orig_horarios and horarios_name is not None:
            logger.warning(
                "Sheet name differs from expected: %r vs %r — using %r",
                horarios_name, _orig_horarios, horarios_name,
            )
        routes = _parse_format_a_named(wb, horarios_name)
    elif is_format_b:
        routes = _parse_format_b(wb)
    else:
        raise ValueError(
            f"Formato de ficheiro não reconhecido. "
            f"Sheets encontradas: {sorted(sheetnames)}"
        )

    # Tag reverse logistics
    for route in routes:
        route["is_reverse_logistics"] = detect_reverse_logistics(route)
        route["reverse_leg_from"] = (
            find_reverse_leg_from(route) if route["is_reverse_logistics"] else None
        )

    # Build stops index
    stops_index: dict[str, list[int]] = {}
    for route in routes:
        for stop in route.get("stops", []):
            name = stop.get("paragem", "")
            if name:
                stops_index.setdefault(name, []).append(route["carreira"])

    # Build summary dict
    summary = {r["carreira"]: r for r in routes}

    logger.info(
        "Parsed %d routes, %d stops, %d unique stop names",
        len(routes), sum(len(r["stops"]) for r in routes), len(stops_index),
    )

    return {
        "routes":      routes,
        "stops_index": stops_index,
        "summary":     summary,
    }


# ---------------------------------------------------------------------------
# Execution file parser
# ---------------------------------------------------------------------------

_EXEC_COL_MAP = {
    "Carreira": "carreira", "Trajeto": "trajeto", "Ponto": "ponto",
    "Ligação": "ligacao", "Viatura": "viatura", "Ocupação": "ocupacao",
    "Rede": "rede", "C/P": "cp", "Dia": "dia", "Previsto": "previsto",
    "Real": "real", "Atraso": "atraso", "Anomalia": "anomalia",
    "Causa": "causa", "Responsabilidade": "responsabilidade",
    "Designação paragem": "designacao_paragem", "TP/RE": "tp_re",
    "Ordem": "ordem", "Real APL": "real_apl",
    "Real Telemetria": "real_telemetria", "Obs.": "obs",
    "Nº Portal": "n_portal",
    # alternate spellings
    "Designação Paragem": "designacao_paragem",
    "Ligacao": "ligacao", "Ocupacao": "ocupacao",
}


def _parse_delay_value(val) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)", s)
    if m:
        return float(m.group(1))
    return None


def parse_execution_file(file) -> pd.DataFrame:
    """Parse execution file (.xls, .xlsx, or .csv) into a normalised DataFrame."""
    fname = getattr(file, "name", "")
    fname_lower = str(fname).lower() if fname else ""

    df: Optional[pd.DataFrame] = None
    errors = []

    if fname_lower.endswith(".xls") and not fname_lower.endswith(".xlsx"):
        try:
            df = pd.read_excel(file, engine="xlrd")
        except Exception as exc:
            errors.append(f"xlrd: {exc}")

    if df is None:
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
        raise ValueError(f"Não foi possível abrir o ficheiro. Erros: {'; '.join(errors)}")

    # Rename columns
    rename_map = {c: _EXEC_COL_MAP[str(c).strip()]
                  for c in df.columns if str(c).strip() in _EXEC_COL_MAP}
    df = df.rename(columns=rename_map)

    # Ensure all internal columns exist
    for internal in set(_EXEC_COL_MAP.values()):
        if internal not in df.columns:
            df[internal] = None

    df["atraso_min"] = df["atraso"].apply(_parse_delay_value)

    def _to_carreira_str(x) -> str:
        if not pd.notna(x) or x == "":
            return ""
        try:
            return str(int(float(str(x).strip())))
        except (ValueError, TypeError):
            return str(x).strip()

    df["carreira_str"] = df["carreira"].apply(_to_carreira_str)

    # Flag individual trips as reverse logistics via Obs. column
    def _trip_is_reverse(obs_val) -> bool:
        if obs_val is None:
            return False
        obs_lower = str(obs_val).lower()
        return any(k in obs_lower for k in _REVERSE_KEYWORDS)

    df["trip_is_reverse"] = df["obs"].apply(_trip_is_reverse)

    return df
