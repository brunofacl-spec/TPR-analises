"""
app.py — Transport Network Delay Analysis
Streamlit application: 3 tabs
  1. Rede Semanal        – network file upload + summary + Sankey + graph
  2. Análise de Impacto  – execution file upload + delay cascade analysis
  3. Pesquisa de Carreira– search for a route and inspect dependencies
"""
from __future__ import annotations

import io
import logging
import math
from typing import Optional

import networkx as nx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ── project imports ──────────────────────────────────────────────────────────
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from src.parser import parse_network_xlsm, parse_execution_file
from src.network import build_dependency_graph, get_downstream, get_upstream
from src.delays import calculate_cascade, get_initial_delays

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="TPR Análise de Atrasos",
    page_icon="🚌",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── constants ─────────────────────────────────────────────────────────────────
SUL_REGIONS = {"OCS/Sul", "OCS/Évora", "OCS/Palmela", "SEX", "SIB", "OLX",
               "CO ALG", "CO EV", "CO BEN", "CO PAL", "SCR"}

REDE_COLORS = {"R1": "#2196F3", "R2": "#4CAF50", "R3": "#FF9800", "": "#9E9E9E"}


def _hex_to_rgba(hex_color: str, alpha: float = 0.5) -> str:
    """Convert #RRGGBB to rgba(r,g,b,alpha) for Plotly compatibility."""
    h = hex_color.lstrip("#")
    if len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r},{g},{b},{alpha})"
    return hex_color

DELAY_COLOR = {
    "low":    "#FFC107",   # < 30 min  🟡
    "medium": "#FF5722",   # 30–60 min 🟠
    "high":   "#D32F2F",   # > 60 min  🔴
}


def _delay_color(minutes: float) -> str:
    if minutes < 30:
        return DELAY_COLOR["low"]
    if minutes < 60:
        return DELAY_COLOR["medium"]
    return DELAY_COLOR["high"]


def _delay_emoji(minutes: float) -> str:
    if minutes < 30:
        return "🟡"
    if minutes < 60:
        return "🟠"
    return "🔴"


# ── session state helpers ─────────────────────────────────────────────────────

def _get_state(key, default=None):
    return st.session_state.get(key, default)


def _set_state(key, value):
    st.session_state[key] = value


# ── sidebar ───────────────────────────────────────────────────────────────────

def render_sidebar():
    st.sidebar.title("⚙️ Configurações")

    connection_window = st.sidebar.slider(
        "Janela de ligação (min)",
        min_value=30,
        max_value=180,
        value=_get_state("connection_window", 90),
        step=5,
        help="Tempo máximo entre partida de A e partida de B no mesmo ponto para considerar ligação.",
    )

    sul_only = st.sidebar.checkbox(
        "Apenas Sul (OCS/SCR/SEX/…)",
        value=_get_state("sul_only", False),
        help="Filtra para carreiras das regiões Sul: OCS/Sul, OCS/Évora, OCS/Palmela, SEX, SIB, OLX, CO ALG, CO EV, CO BEN, CO PAL, SCR",
    )

    # Rebuild graph if parameters changed
    prev_window = _get_state("connection_window", 90)
    prev_sul = _get_state("sul_only", False)

    _set_state("connection_window", connection_window)
    _set_state("sul_only", sul_only)

    routes = _get_state("routes")
    if routes and (connection_window != prev_window or sul_only != prev_sul):
        _rebuild_graph(routes, connection_window, sul_only)

    include_reverse = st.sidebar.toggle(
        "Incluir logística inversa no impacto",
        value=_get_state("include_reverse_logistics", False),
        help="Quando desactivado (padrão), atrasos em carreiras de logística inversa (retornos vazios) NÃO se propagam para carreiras a jusante.",
    )
    _set_state("include_reverse_logistics", include_reverse)

    st.sidebar.markdown("---")
    st.sidebar.caption("TPR Análise de Transportes v1.0")


def _filter_routes(routes: list[dict], sul_only: bool) -> list[dict]:
    if not sul_only:
        return routes
    return [r for r in routes if any(s in r.get("regiao", "") for s in SUL_REGIONS)]


def _rebuild_graph(routes: list[dict], window: int, sul_only: bool):
    filtered = _filter_routes(routes, sul_only)
    with st.spinner("A construir grafo de dependências…"):
        graph = build_dependency_graph(filtered, window)
    _set_state("graph", graph)
    _set_state("graph_routes", filtered)
    routes_dict = {r["carreira"]: r for r in filtered}
    _set_state("routes_dict", routes_dict)


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Rede Semanal
# ═══════════════════════════════════════════════════════════════════════════════

_NETWORK_FILE_PATH = os.path.join(os.path.dirname(__file__), "data", "rede_atual.xlsm")
_NETWORK_META_PATH = os.path.join(os.path.dirname(__file__), "data", "rede_meta.txt")


def _load_network_from_bytes(raw_bytes: bytes, filename: str):
    """Parse network bytes, update session state, rebuild graph."""
    import importlib, src.parser as _parser_mod
    importlib.reload(_parser_mod)
    from src.parser import parse_network_xlsm as _parse_fresh

    file_obj = io.BytesIO(raw_bytes)
    file_obj.name = filename
    data = _parse_fresh(file_obj)
    routes = data["routes"]
    _set_state("routes", routes)
    _set_state("stops_index", data["stops_index"])
    _set_state("summary", data.get("summary", {}))
    _set_state("coordinates", data.get("coordinates", {}))
    window  = _get_state("connection_window", 90)
    sul_only = _get_state("sul_only", False)
    _rebuild_graph(routes, window, sul_only)
    return routes


def _save_network_file(raw_bytes: bytes, filename: str):
    """Persist network file to data/ folder."""
    os.makedirs(os.path.dirname(_NETWORK_FILE_PATH), exist_ok=True)
    with open(_NETWORK_FILE_PATH, "wb") as f:
        f.write(raw_bytes)
    import datetime as _dt
    with open(_NETWORK_META_PATH, "w") as f:
        f.write(f"{filename}\n{_dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")


def _read_network_meta() -> tuple[str, str]:
    """Return (filename, saved_at) from meta file, or ('', '') if absent."""
    try:
        with open(_NETWORK_META_PATH) as f:
            lines = f.read().splitlines()
        return lines[0] if lines else "", lines[1] if len(lines) > 1 else ""
    except FileNotFoundError:
        return "", ""


def tab_rede_semanal():
    st.header("📋 Rede Semanal")

    # ── Auto-load persisted file on first run ────────────────────────────────
    if not _get_state("routes") and os.path.exists(_NETWORK_FILE_PATH):
        fname, saved_at = _read_network_meta()
        with st.spinner(f"A carregar rede guardada ({fname or 'rede_atual.xlsm'})…"):
            try:
                with open(_NETWORK_FILE_PATH, "rb") as f:
                    raw = f.read()
                routes = _load_network_from_bytes(raw, fname or "rede_atual.xlsm")
                if routes:
                    st.success(
                        f"✅ Rede carregada automaticamente — **{fname}**"
                        + (f" (guardada em {saved_at})" if saved_at else "")
                        + f"  |  {len(routes)} carreiras"
                    )
            except Exception as exc:
                st.warning(f"Não foi possível carregar o ficheiro guardado: {exc}")

    # ── Replace file section ─────────────────────────────────────────────────
    fname_current, saved_at = _read_network_meta()
    has_saved = os.path.exists(_NETWORK_FILE_PATH)

    if has_saved:
        label = (
            f"🔄 Substituir ficheiro de rede"
            + (f" (actual: **{fname_current}**, {saved_at})" if fname_current else "")
        )
    else:
        label = "📂 Carregar ficheiro de rede (.xlsm / .xlsx)"

    with st.expander(label, expanded=not has_saved):
        uploaded = st.file_uploader(
            "Novo ficheiro de rede (.xlsm / .xlsx)",
            type=["xlsm", "xlsx"],
            key="network_uploader",
        )
        if uploaded is not None:
            with st.spinner("A processar e guardar ficheiro de rede…"):
                try:
                    raw_bytes = uploaded.read()
                    routes = _load_network_from_bytes(raw_bytes, uploaded.name)
                    if routes:
                        _save_network_file(raw_bytes, uploaded.name)
                        st.success(
                            f"✅ {len(routes)} carreiras carregadas e ficheiro guardado. "
                            "Será usado automaticamente nas próximas sessões."
                        )
                    else:
                        st.error("⚠️ 0 carreiras encontradas.")
                        with st.expander("Diagnóstico"):
                            import openpyxl as _opx
                            _wb = _opx.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
                            st.write("**Sheets:**", _wb.sheetnames)
                            _wb.close()
                except Exception as exc:
                    st.error(f"Erro ao carregar ficheiro: {exc}")
                    logger.exception("Network file parse error")
                    return

    routes = _get_state("routes")
    if not routes:
        st.info("⬆️ Carregue o ficheiro de rede para começar.")
        return

    graph_routes = _get_state("graph_routes", routes)

    # ── Summary tables ──────────────────────────────────────────────────────
    st.subheader("Resumo da rede")
    df_routes = pd.DataFrame([
        {
            "carreira": r["carreira"],
            "designacao": r["designacao"],
            "rede": r.get("rede", ""),
            "regiao": r.get("regiao", ""),
            "transportador": r.get("transportador", ""),
            "veiculo": r.get("veiculo", ""),
            "origem": r.get("origem", ""),
            "destino": r.get("destino", ""),
            "n_paragens": len(r.get("stops", [])),
        }
        for r in graph_routes
    ])

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("**Por tipo de rede (R1/R2/R3)**")
        rede_counts = df_routes["rede"].value_counts().rename_axis("Rede").reset_index(name="Carreiras")
        st.dataframe(rede_counts, hide_index=True, use_container_width=True)

    with col2:
        st.markdown("**Por região**")
        reg_counts = df_routes["regiao"].value_counts().rename_axis("Região").reset_index(name="Carreiras")
        st.dataframe(reg_counts, hide_index=True, use_container_width=True)

    with col3:
        st.markdown("**Por transportador**")
        transp_counts = df_routes["transportador"].value_counts().rename_axis("Transportador").reset_index(name="Carreiras")
        st.dataframe(transp_counts, hide_index=True, use_container_width=True)

    st.markdown("---")

    # ── Sankey: origin stops → routes → destination stops ───────────────────
    st.subheader("Diagrama Sankey — Origens → Carreiras → Destinos")

    with st.spinner("A gerar Sankey…"):
        fig_sankey = _build_sankey(graph_routes)
    st.plotly_chart(fig_sankey, use_container_width=True)

    st.markdown("---")

    # ── Map graph ────────────────────────────────────────────────────────────
    st.subheader("Mapa da Rede de Transportes")
    st.caption("Paragens georreferenciadas com ligações por tipo de rede (R1=azul, R2=verde, R3=laranja)")

    coordinates = _get_state("coordinates", {})
    graph = _get_state("graph")

    with st.spinner("A gerar mapa…"):
        fig_net = _build_map_graph(graph_routes, coordinates, graph)
    st.plotly_chart(fig_net, use_container_width=True)

    # ── Full route table ─────────────────────────────────────────────────────
    with st.expander("📄 Tabela completa de carreiras"):
        st.dataframe(
            df_routes.rename(columns={
                "carreira": "Carreira", "designacao": "Designação",
                "rede": "Rede", "regiao": "Região",
                "transportador": "Transportador", "veiculo": "Veículo",
                "origem": "Origem", "destino": "Destino",
                "n_paragens": "N.º Paragens",
            }),
            hide_index=True,
            use_container_width=True,
        )


def _build_sankey(routes: list[dict]) -> go.Figure:
    """Build a Sankey diagram: origin stops → route labels → destination stops."""
    # Limit for readability
    sample = routes[:200]

    origins = sorted({r.get("origem", "?") for r in sample if r.get("origem")})
    dests = sorted({r.get("destino", "?") for r in sample if r.get("destino")})
    route_labels = [f"{r['carreira']}" for r in sample]

    all_labels = origins + route_labels + dests

    orig_idx = {o: i for i, o in enumerate(all_labels)}

    sources, targets, values, colors = [], [], [], []

    for r in sample:
        orig = r.get("origem", "")
        dest = r.get("destino", "")
        label = f"{r['carreira']}"
        rede = r.get("rede", "")
        color = REDE_COLORS.get(rede, "#9E9E9E")

        if orig and label in orig_idx and orig in orig_idx:
            sources.append(orig_idx[orig])
            targets.append(orig_idx[label])
            values.append(1)
            colors.append(color)

        if dest and label in orig_idx and dest in orig_idx:
            sources.append(orig_idx[label])
            targets.append(orig_idx[dest])
            values.append(1)
            colors.append(color)

    node_colors = []
    for lbl in all_labels:
        # Check if it's a route label (numeric-ish)
        if lbl.isdigit():
            # Find route rede
            r_list = [r for r in sample if str(r["carreira"]) == lbl]
            if r_list:
                node_colors.append(REDE_COLORS.get(r_list[0].get("rede", ""), "#9E9E9E"))
            else:
                node_colors.append("#9E9E9E")
        else:
            node_colors.append("#78909C")

    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(
            pad=8,
            thickness=12,
            label=all_labels,
            color=node_colors,
        ),
        link=dict(
            source=sources,
            target=targets,
            value=values,
            color=[_hex_to_rgba(c, 0.53) for c in colors],
        ),
    ))
    fig.update_layout(
        title_text="Fluxo Origens → Carreiras → Destinos (primeiras 200 carreiras)",
        font_size=10,
        height=600,
        margin=dict(l=20, r=20, t=40, b=20),
    )
    return fig


def _build_map_graph(
    routes: list[dict],
    coordinates: dict[str, tuple[float, float]],
    graph=None,
) -> go.Figure:
    """Build a Plotly Scattermapbox with stops on the Portugal map.

    Edges = route segments between consecutive stops (coloured by rede).
    Nodes = stops sized by number of connections in the dependency graph.
    """
    from collections import Counter, defaultdict

    # Compute stop degree from dependency graph (how many connections each stop has)
    stop_degree: Counter = Counter()
    if graph is not None:
        for u, v, d in graph.edges(data=True):
            stop_degree[d.get("stop", "")] += 1

    # Collect route segments per rede type (only between stops with coordinates)
    edge_lats: dict[str, list] = defaultdict(list)
    edge_lons: dict[str, list] = defaultdict(list)

    # Collect unique stops
    stop_info: dict[str, dict] = {}

    for r in routes:
        stop_list = r.get("stops", [])
        rede = r.get("rede", "").split()[0] if r.get("rede") else ""  # R1/R2/R3
        rede_key = rede if rede in REDE_COLORS else ""

        for i, s in enumerate(stop_list):
            name = s.get("paragem", "")
            if not name:
                continue
            if name not in stop_info and name in coordinates:
                lat, lon = coordinates[name]
                stop_info[name] = {
                    "lat": lat, "lon": lon,
                    "rede": rede_key,
                    "carreiras": [],
                }
            if name in stop_info:
                stop_info[name]["carreiras"].append(r.get("carreira"))

            # Draw edge from previous stop
            if i > 0:
                prev = stop_list[i - 1].get("paragem", "")
                if prev in coordinates and name in coordinates:
                    plat, plon = coordinates[prev]
                    clat, clon = coordinates[name]
                    edge_lats[rede_key] += [plat, clat, None]
                    edge_lons[rede_key] += [plon, clon, None]

    fig = go.Figure()

    # Edge traces (one per rede type, thin semi-transparent lines)
    rede_labels = {"R1": "R1 (Principal)", "R2": "R2 (Secundária)",
                   "R3": "R3 (Terciária)", "": "Outro"}
    for rede_key, color in REDE_COLORS.items():
        if rede_key not in edge_lats:
            continue
        rgba = _hex_to_rgba(color, 0.25)
        fig.add_trace(go.Scattermapbox(
            lat=edge_lats[rede_key],
            lon=edge_lons[rede_key],
            mode="lines",
            line=dict(width=1, color=rgba),
            name=rede_labels.get(rede_key, rede_key),
            hoverinfo="none",
            showlegend=True,
        ))

    # Node trace — size proportional to dependency degree
    if stop_info:
        lats = [stop_info[n]["lat"] for n in stop_info]
        lons = [stop_info[n]["lon"] for n in stop_info]
        names = list(stop_info.keys())
        colors = [REDE_COLORS.get(stop_info[n]["rede"], "#9E9E9E") for n in names]
        degrees = [stop_degree.get(n, 0) for n in names]
        max_deg = max(degrees) if degrees else 1
        sizes = [6 + 18 * (d / max(max_deg, 1)) for d in degrees]
        n_routes = [len(set(stop_info[n]["carreiras"])) for n in names]
        hover = [
            f"<b>{n}</b><br>Carreiras: {nr}<br>Ligações (dependências): {d}"
            for n, nr, d in zip(names, n_routes, degrees)
        ]

        fig.add_trace(go.Scattermapbox(
            lat=lats,
            lon=lons,
            mode="markers",
            marker=dict(
                size=sizes,
                color=colors,
                opacity=0.85,
                sizemode="diameter",
            ),
            text=names,
            hovertemplate="%{customdata}<extra></extra>",
            customdata=hover,
            name="Paragens",
            showlegend=True,
        ))

    # Península Ibérica centrada em Portugal
    fig.update_layout(
        mapbox=dict(
            style="open-street-map",
            center=dict(lat=39.8, lon=-6.5),
            zoom=5.0,
        ),
        height=720,
        margin=dict(l=0, r=0, t=30, b=0),
        legend=dict(
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="#ccc",
            borderwidth=1,
            x=0.01, y=0.99,
            xanchor="left", yanchor="top",
        ),
        title="Rede de Transportes — Península Ibérica",
    )
    return fig


# ═══════════════════════════════════════════════════════════════════════════════
# Metrics helpers
# ═══════════════════════════════════════════════════════════════════════════════

_SUL_PREFIXES = {"OCS", "SCR", "SEX", "SIB", "OLX", "CO ALG", "CO EV", "CO BEN", "CO PAL"}

def _regiao_label(tp_re: str) -> str:
    prefix = str(tp_re).split("/")[0].strip().upper()
    if prefix in _SUL_PREFIXES:
        return "Sul"
    if prefix.startswith("OCC") or prefix.startswith("CCR") or prefix.startswith("CEX") or prefix in {
        "CO AV", "CO CO", "CO LR", "CO PIN", "CO TN", "CO VS"
    }:
        return "Centro"
    if prefix.startswith("OCN") or prefix.startswith("NCR") or prefix.startswith("NEX") or prefix in {
        "CO BR", "CO RIO"
    }:
        return "Norte"
    if prefix in {"CO MAD", "TPM", "TPA", "CO OVD", "CO SEV"}:
        return "Ibéria/Outro"
    return prefix or "Desconhecido"


_TURNOS = {
    "00h–08h": (0, 8),
    "08h–16h": (8, 16),
    "16h–24h": (16, 24),
}

def _previsto_hour(val) -> Optional[int]:
    """Extract hour from a Previsto value (string 'HH:MM:SS', time, or datetime)."""
    import datetime as _dt
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    if isinstance(val, _dt.time):
        return val.hour
    if isinstance(val, _dt.datetime):
        return val.hour
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s.split(":")[0])
    except (ValueError, IndexError):
        return None


def _turno_label(hour: Optional[int]) -> str:
    if hour is None:
        return "Desconhecido"
    for label, (h0, h1) in _TURNOS.items():
        if h0 <= hour < h1:
            return label
    return "Desconhecido"


def _compute_metrics(exec_df: pd.DataFrame) -> dict:
    """Compute OTD, OTA, OTP and summary stats from the execution DataFrame."""
    df = exec_df.copy()
    df["_anomalia"] = df["anomalia"].fillna("").str.strip().str.upper()
    df["_cp"] = df["cp"].fillna("").str.strip()
    df["_atraso"] = pd.to_numeric(df["atraso_min"], errors="coerce").fillna(0)
    df["_tp_re"] = df["tp_re"].fillna("")
    df["_rede"] = df["rede"].fillna("")
    df["_dia"] = df["dia"].astype(str)
    df["_hora"] = df["previsto"].apply(_previsto_hour)
    df["_turno"] = df["_hora"].apply(_turno_label)
    df["_regiao"] = df["_tp_re"].apply(_regiao_label)

    p = df[df["_cp"] == "P"]
    c = df[df["_cp"] == "C"]

    p_valid = p[p["_anomalia"] != "CANCELADA"]
    c_valid = c[c["_anomalia"] != "CANCELADA"]

    # OTD
    p_delayed = p_valid[p_valid["_anomalia"] == "ATRASO PARTIDA"]
    otd = (len(p_valid) - len(p_delayed)) / len(p_valid) * 100 if len(p_valid) else 0.0

    # OTA
    c_delayed = c_valid[c_valid["_anomalia"] == "ATRASO CHEGADA"]
    ota = (len(c_valid) - len(c_delayed)) / len(c_valid) * 100 if len(c_valid) else 0.0

    # OTP — first departure per carreira (min Ordem among P rows)
    if "ordem" in df.columns:
        p_ord = p_valid.copy()
        p_ord["_ordem"] = pd.to_numeric(p_ord["ordem"], errors="coerce")
        first_dep = p_ord.loc[p_ord.groupby("carreira_str")["_ordem"].idxmin()]
    else:
        first_dep = p_valid.loc[p_valid.groupby("carreira_str")["_atraso"].idxmax()]
    fd_delayed = first_dep[first_dep["_anomalia"] == "ATRASO PARTIDA"]
    otp = (len(first_dep) - len(fd_delayed)) / len(first_dep) * 100 if len(first_dep) else 0.0

    # Cancelled routes
    n_cancelled = df[df["_anomalia"] == "CANCELADA"]["carreira_str"].nunique()
    pct_cancelled = n_cancelled / (df["carreira_str"].nunique()) * 100

    # Delay stats (departures only)
    delay_vals = p_delayed["_atraso"]
    mean_delay  = delay_vals.mean() if len(delay_vals) else 0.0
    median_delay = delay_vals.median() if len(delay_vals) else 0.0
    max_delay   = delay_vals.max() if len(delay_vals) else 0.0
    n_gt30 = int((delay_vals > 30).sum())
    n_gt60 = int((delay_vals > 60).sum())

    # OTD by rede
    otd_rede = {}
    for rede, grp in p_valid.groupby("_rede"):
        d = (grp["_anomalia"] == "ATRASO PARTIDA").sum()
        otd_rede[rede] = round((len(grp) - d) / len(grp) * 100, 1) if len(grp) else 0.0

    # OTD by region
    otd_reg = {}
    for reg, grp in p_valid.groupby("_regiao"):
        d = (grp["_anomalia"] == "ATRASO PARTIDA").sum()
        otd_reg[reg] = {
            "otd": round((len(grp) - d) / len(grp) * 100, 1) if len(grp) else 0.0,
            "n_total": len(grp),
            "n_delayed": int(d),
        }

    # OTD by day
    otd_day = {}
    for day, grp in p_valid.groupby("_dia"):
        d = (grp["_anomalia"] == "ATRASO PARTIDA").sum()
        otd_day[day] = round((len(grp) - d) / len(grp) * 100, 1) if len(grp) else 0.0

    # Top delayed routes
    top_delayed = (
        p_delayed.groupby(["carreira_str", "ligacao"])["_atraso"]
        .max()
        .sort_values(ascending=False)
        .head(15)
        .reset_index()
    )
    top_delayed.columns = ["carreira", "designacao", "atraso_max"]

    # Delay distribution buckets
    bins   = [0, 15, 30, 60, float("inf")]
    labels = ["≤15 min", "16–30 min", "31–60 min", ">60 min"]
    dist = pd.cut(delay_vals, bins=bins, labels=labels, right=True).value_counts().sort_index()

    # Per-shift metrics (OTD / OTA / OTP per turno)
    turno_metrics = {}
    for turno_label in list(_TURNOS.keys()) + ["Desconhecido"]:
        pv = p_valid[p_valid["_turno"] == turno_label]
        cv = c_valid[c_valid["_turno"] == turno_label]
        if len(pv) == 0 and len(cv) == 0:
            continue
        pd_ = (pv["_anomalia"] == "ATRASO PARTIDA").sum()
        cd_ = (cv["_anomalia"] == "ATRASO CHEGADA").sum()
        _otd = round((len(pv) - pd_) / len(pv) * 100, 1) if len(pv) else None
        _ota = round((len(cv) - cd_) / len(cv) * 100, 1) if len(cv) else None
        # OTP: first departure per carreira within this shift
        pv_ord = pv.copy()
        pv_ord["_ordem"] = pd.to_numeric(pv_ord["ordem"], errors="coerce")
        if pv_ord["_ordem"].notna().any():
            fd = pv_ord.loc[pv_ord.groupby("carreira_str")["_ordem"].idxmin()]
        else:
            fd = pv_ord.drop_duplicates("carreira_str")
        fd_d = (fd["_anomalia"] == "ATRASO PARTIDA").sum()
        _otp = round((len(fd) - fd_d) / len(fd) * 100, 1) if len(fd) else None
        turno_metrics[turno_label] = dict(
            otd=_otd, ota=_ota, otp=_otp,
            n_p=len(pv), n_delayed_p=int(pd_),
            n_c=len(cv), n_delayed_c=int(cd_),
            mean_delay=round(pv[pv["_anomalia"]=="ATRASO PARTIDA"]["_atraso"].mean(), 1)
                if pd_ > 0 else 0.0,
        )

    # Cause analysis — departure delays only
    causa_col = "causa" if "causa" in df.columns else None
    resp_col  = "responsabilidade" if "responsabilidade" in df.columns else None
    ligacao_col = "ligacao" if "ligacao" in df.columns else None

    causa_summary = []
    if causa_col:
        for causa, grp in p_delayed.groupby(p_delayed[causa_col].fillna("SEM CAUSA").str.strip().str.upper()):
            top_enlaces = []
            if ligacao_col:
                top_enlaces = (
                    grp[ligacao_col].fillna("").str.strip()
                    .value_counts().head(5)
                    .reset_index()
                    .rename(columns={"index": "ligacao", ligacao_col: "ligacao", "count": "n"})
                    .values.tolist()
                )
            top_resp = []
            if resp_col:
                top_resp = (
                    grp[resp_col].fillna("").str.strip()
                    .value_counts().head(3)
                    .reset_index()
                    .rename(columns={"index": "resp", resp_col: "resp", "count": "n"})
                    .values.tolist()
                )
            causa_summary.append({
                "causa":      causa,
                "n":          len(grp),
                "pct":        round(len(grp) / len(p_delayed) * 100, 1),
                "mean_delay": round(grp["_atraso"].mean(), 1),
                "max_delay":  int(grp["_atraso"].max()),
                "top_enlaces": top_enlaces,
                "top_resp":   top_resp,
            })
        causa_summary.sort(key=lambda x: -x["n"])

    return dict(
        otd=otd, ota=ota, otp=otp,
        n_cancelled=n_cancelled, pct_cancelled=pct_cancelled,
        n_delayed_p=len(p_delayed), n_valid_p=len(p_valid),
        n_delayed_c=len(c_delayed), n_valid_c=len(c_valid),
        mean_delay=mean_delay, median_delay=median_delay,
        max_delay=max_delay, n_gt30=n_gt30, n_gt60=n_gt60,
        otd_rede=otd_rede, otd_reg=otd_reg, otd_day=otd_day,
        top_delayed=top_delayed, delay_dist=dist,
        turno_metrics=turno_metrics,
        causa_summary=causa_summary,
        days=sorted(df["_dia"].unique()),
    )


def _render_metrics(m: dict):
    """Render the OTD/OTA/OTP dashboard."""
    def _color(pct):
        if pct >= 95:  return "🟢"
        if pct >= 90:  return "🟡"
        return "🔴"

    # ── KPI cards ────────────────────────────────────────────────────────────
    st.subheader("📊 KPIs de Pontualidade")
    dias_label = " · ".join(m["days"]) if m["days"] else "—"
    st.caption(f"Período: {dias_label}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        f"{_color(m['otd'])} OTD — On Time Departure",
        f"{m['otd']:.1f}%",
        help="% de partidas sem atraso (excluindo canceladas)",
    )
    c2.metric(
        f"{_color(m['ota'])} OTA — On Time Arrival",
        f"{m['ota']:.1f}%",
        help="% de chegadas sem atraso (excluindo canceladas)",
    )
    c3.metric(
        f"{_color(m['otp'])} OTP — On Time Presentation",
        f"{m['otp']:.1f}%",
        help="% de carreiras com a 1.ª partida pontual (proxy de apresentação)",
    )
    c4.metric(
        "🚫 Canceladas",
        f"{m['n_cancelled']} carreiras",
        f"{m['pct_cancelled']:.1f}% do total",
        delta_color="inverse",
        help="Carreiras com pelo menos um evento CANCELADA",
    )

    st.markdown("---")

    # ── Per-shift breakdown ───────────────────────────────────────────────────
    st.subheader("🕐 Métricas por Turno")
    turno_data = m.get("turno_metrics", {})
    if turno_data:
        cols = st.columns(len(turno_data))
        for col, (turno, tm) in zip(cols, turno_data.items()):
            otd_v = tm["otd"]
            ota_v = tm["ota"]
            otp_v = tm["otp"]
            col.markdown(f"**{turno}**")
            col.metric(
                f"{_color(otd_v)} OTD",
                f"{otd_v:.1f}%" if otd_v is not None else "—",
                f"{tm['n_delayed_p']} atrasos / {tm['n_p']} partidas",
                delta_color="off",
            )
            col.metric(
                f"{_color(ota_v)} OTA",
                f"{ota_v:.1f}%" if ota_v is not None else "—",
                f"{tm['n_delayed_c']} atrasos / {tm['n_c']} chegadas",
                delta_color="off",
            )
            col.metric(
                f"{_color(otp_v)} OTP",
                f"{otp_v:.1f}%" if otp_v is not None else "—",
                delta_color="off",
            )
            if tm["mean_delay"] > 0:
                col.caption(f"Média atraso partida: **{tm['mean_delay']:.1f} min**")

        # Bar chart: OTD per shift side-by-side with OTA and OTP
        turnos_order = [t for t in _TURNOS if t in turno_data]
        fig_t = go.Figure()
        for metric_key, metric_name, color in [
            ("otd", "OTD", "#2196F3"),
            ("ota", "OTA", "#4CAF50"),
            ("otp", "OTP", "#FF9800"),
        ]:
            vals = [turno_data[t].get(metric_key) for t in turnos_order]
            fig_t.add_trace(go.Bar(
                name=metric_name,
                x=turnos_order,
                y=vals,
                marker_color=color,
                text=[f"{v:.1f}%" if v is not None else "—" for v in vals],
                textposition="outside",
            ))
        fig_t.add_hline(y=95, line_dash="dot", line_color="#4CAF50",
                        annotation_text="Meta 95%", annotation_position="right")
        fig_t.add_hline(y=90, line_dash="dot", line_color="#FFC107",
                        annotation_text="90%", annotation_position="right")
        fig_t.update_layout(
            barmode="group",
            yaxis=dict(range=[75, 101], title="%"),
            height=320,
            margin=dict(l=20, r=60, t=20, b=20),
            legend=dict(orientation="h", y=1.1),
        )
        st.plotly_chart(fig_t, use_container_width=True)

    st.markdown("---")

    # ── Delay stats + distribution ────────────────────────────────────────────
    col_a, col_b = st.columns([1, 2])

    with col_a:
        st.subheader("Atrasos de Partida")
        st.markdown(f"""
| Indicador | Valor |
|---|---|
| Nº atrasos | **{m['n_delayed_p']}** de {m['n_valid_p']} partidas |
| Média | **{m['mean_delay']:.1f} min** |
| Mediana | **{m['median_delay']:.1f} min** |
| Máximo | **{m['max_delay']:.0f} min** |
| > 30 min | **{m['n_gt30']}** |
| > 60 min | **{m['n_gt60']}** |
""")

    with col_b:
        dist = m["delay_dist"]
        if not dist.empty:
            fig_dist = go.Figure(go.Bar(
                x=dist.index.astype(str).tolist(),
                y=dist.values.tolist(),
                marker_color=["#4CAF50", "#FFC107", "#FF5722", "#D32F2F"],
                text=dist.values.tolist(),
                textposition="outside",
            ))
            fig_dist.update_layout(
                title="Distribuição de atrasos de partida",
                xaxis_title="Intervalo",
                yaxis_title="Nº ocorrências",
                height=280,
                margin=dict(l=20, r=20, t=40, b=20),
                showlegend=False,
            )
            st.plotly_chart(fig_dist, use_container_width=True)

    st.markdown("---")

    # ── OTD by rede + region ────────────────────────────────────────────────
    col_r, col_g = st.columns(2)

    with col_r:
        st.subheader("OTD por Rede")
        rede_data = [
            {"Rede": k, "OTD (%)": v, "": _color(v)}
            for k, v in sorted(m["otd_rede"].items())
        ]
        st.dataframe(pd.DataFrame(rede_data), hide_index=True, use_container_width=True)

    with col_g:
        st.subheader("OTD por Região")
        reg_data = sorted(
            [
                {"Região": r, "OTD (%)": v["otd"], "Atrasos": v["n_delayed"],
                 "Total": v["n_total"], "": _color(v["otd"])}
                for r, v in m["otd_reg"].items()
            ],
            key=lambda x: x["OTD (%)"],
        )
        st.dataframe(pd.DataFrame(reg_data), hide_index=True, use_container_width=True)

    st.markdown("---")

    # ── Top delayed routes ───────────────────────────────────────────────────
    st.subheader("🔴 Top carreiras com maior atraso de partida")
    top = m["top_delayed"].copy()
    top["⚠️"] = top["atraso_max"].apply(_delay_emoji)
    top = top.rename(columns={
        "carreira": "Carreira", "designacao": "Ligação", "atraso_max": "Atraso máx (min)"
    })
    st.dataframe(top[["⚠️", "Carreira", "Ligação", "Atraso máx (min)"]],
                 hide_index=True, use_container_width=True)

    st.markdown("---")

    # ── Cause summary ────────────────────────────────────────────────────────
    causa_list = m.get("causa_summary", [])
    if causa_list:
        st.subheader("📋 Resumo das Causas de Atraso na Partida")

        # Narrative text
        days_str = " e ".join(m["days"]) if m["days"] else "o período"
        total_d   = m["n_delayed_p"]
        top3      = causa_list[:3]

        lines = [
            f"**Período analisado:** {days_str}  |  "
            f"**Total de atrasos de partida:** {total_d} em {m['n_valid_p']} partidas válidas  |  "
            f"**OTD:** {m['otd']:.1f}%\n",
        ]
        lines.append("**Principais causas:**\n")
        for i, c in enumerate(top3, 1):
            enlaces = ", ".join(
                f"*{e[0]}*" for e in c["top_enlaces"][:3] if e[0]
            ) or "—"
            resp = ", ".join(
                f"{r[0]} ({r[1]})" for r in c["top_resp"][:2] if r[0]
            ) or "—"
            lines.append(
                f"{i}. **{c['causa']}** — {c['n']} ocorrências ({c['pct']:.0f}%)  "
                f"| Média: {c['mean_delay']:.0f} min | Máx: {c['max_delay']} min  \n"
                f"   Principais enlaces: {enlaces}  \n"
                f"   Responsabilidade: {resp}\n"
            )
        if len(causa_list) > 3:
            rest = causa_list[3:]
            rest_str = " · ".join(f"{c['causa']} ({c['n']})" for c in rest)
            lines.append(f"\n**Outras causas:** {rest_str}")

        st.markdown("\n".join(lines))

        st.markdown("---")

        # Expandable detail per cause
        st.markdown("**Detalhe por causa:**")
        for c in causa_list:
            label = f"{_delay_emoji(c['max_delay'])} {c['causa']}  — {c['n']} ocorrências ({c['pct']:.0f}%)  ·  média {c['mean_delay']:.0f} min"
            with st.expander(label):
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown("**Principais enlaces afectados**")
                    if c["top_enlaces"]:
                        rows = [{"Ligação": e[0], "Ocorrências": e[1]} for e in c["top_enlaces"] if e[0]]
                        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
                    else:
                        st.caption("Sem dados de ligação")
                with col2:
                    st.markdown("**Responsabilidade**")
                    if c["top_resp"]:
                        rows = [{"Entidade": r[0], "Ocorrências": r[1]} for r in c["top_resp"] if r[0]]
                        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
                    else:
                        st.caption("Sem dados de responsabilidade")

    st.markdown("---")
# ═══════════════════════════════════════════════════════════════════════════════

def tab_impacto_atrasos():
    st.header("⏱️ Análise de Impacto de Atrasos")

    routes = _get_state("routes")
    if not routes:
        st.warning("⚠️ Carregue primeiro o ficheiro de rede na tab **Rede Semanal**.")
        return

    graph = _get_state("graph")
    routes_dict = _get_state("routes_dict", {r["carreira"]: r for r in routes})

    uploaded_exec = st.file_uploader(
        "Carregar ficheiro de execução (.xls / .xlsx / .csv)",
        type=["xls", "xlsx", "csv"],
        key="exec_uploader",
    )

    if uploaded_exec is not None:
        with st.spinner("A processar ficheiro de execução…"):
            try:
                exec_df = parse_execution_file(uploaded_exec)
                _set_state("exec_df", exec_df)
                st.success(f"✅ {len(exec_df)} registos carregados.")
            except Exception as exc:
                st.error(f"Erro ao carregar ficheiro de execução: {exc}")
                logger.exception("Execution file parse error")
                return

    exec_df = _get_state("exec_df")
    if exec_df is None:
        st.info("⬆️ Carregue o ficheiro de execução para análise de atrasos.")
        return

    # ── OTD / OTA / OTP metrics ──────────────────────────────────────────────
    with st.spinner("A calcular métricas…"):
        metrics = _compute_metrics(exec_df)
    _render_metrics(metrics)

    # ── Detected delays ──────────────────────────────────────────────────────
    anomaly_options = ["ATRASO PARTIDA", "ATRASO CHEGADA", ""]
    anomaly_filter = st.selectbox(
        "Filtro de anomalia",
        options=anomaly_options,
        index=0,
        format_func=lambda x: x if x else "(todas as anomalias)",
        key="anomaly_filter",
    )

    initial_delays = get_initial_delays(exec_df, anomaly_filter or None)

    st.subheader(f"Atrasos iniciais detectados: {len(initial_delays)}")

    if not initial_delays:
        st.info("Nenhum atraso encontrado com os filtros actuais.")
        return

    df_init = pd.DataFrame(initial_delays)
    df_init_display = df_init.copy()
    df_init_display["Sinal"] = df_init_display["delay_minutes"].apply(_delay_emoji)
    # Merge with route info
    df_init_display["designacao"] = df_init_display["carreira"].map(
        lambda c: routes_dict.get(c, {}).get("designacao", "")
    )
    df_init_display["rede"] = df_init_display["carreira"].map(
        lambda c: routes_dict.get(c, {}).get("rede", "")
    )
    # Reverse-logistics badge (combines route-level and trip-level detection)
    def _rev_badge(row):
        route_rev = routes_dict.get(row["carreira"], {}).get("is_reverse_logistics", False)
        trip_rev  = row.get("is_reverse_logistics", False)
        return "↩️ Logística Inversa" if (route_rev or trip_rev) else ""

    df_init_display["Tipo"] = df_init_display.apply(_rev_badge, axis=1)

    st.dataframe(
        df_init_display.rename(columns={
            "Sinal": "⚠️",
            "carreira": "Carreira",
            "designacao": "Designação",
            "rede": "Rede",
            "delay_minutes": "Atraso (min)",
            "stop": "Paragem",
            "causa": "Causa",
            "Tipo": "Tipo",
        })[[
            "⚠️", "Carreira", "Designação", "Rede", "Atraso (min)", "Paragem", "Causa", "Tipo"
        ]],
        hide_index=True,
        use_container_width=True,
    )

    st.markdown("---")

    # ── Cascade calculation ──────────────────────────────────────────────────
    st.subheader("Cálculo de cascata")

    if graph is None:
        st.warning("Grafo de dependências não disponível.")
        return

    include_reverse = _get_state("include_reverse_logistics", False)
    with st.spinner("A calcular cascata de atrasos…"):
        cascade_df = calculate_cascade(
            initial_delays, graph, routes_dict,
            include_reverse_logistics=include_reverse,
        )

    if cascade_df.empty:
        st.info("Nenhum atraso em cascata detectado.")
        return

    st.markdown(f"**{len(cascade_df)} carreiras afectadas em cascata**")

    # Add visual indicators
    cascade_df["⚠️"] = cascade_df["atraso_herdado"].apply(_delay_emoji)
    orig_design = cascade_df["origem_atraso"].map(
        lambda c: routes_dict.get(c, {}).get("designacao", str(c))
    )
    cascade_df["origem_desig"] = orig_design

    # Reverse-logistics badge for each affected route
    cascade_df["Tipo"] = cascade_df["carreira"].map(
        lambda c: "↩️ Logística Inversa" if routes_dict.get(c, {}).get("is_reverse_logistics", False) else ""
    )

    # Color filter
    severity_filter = st.multiselect(
        "Filtrar por severidade",
        options=["🟡 <30 min", "🟠 30-60 min", "🔴 >60 min"],
        default=["🟡 <30 min", "🟠 30-60 min", "🔴 >60 min"],
        key="severity_filter",
    )

    filtered_cascade = cascade_df.copy()
    if "🟡 <30 min" not in severity_filter:
        filtered_cascade = filtered_cascade[filtered_cascade["atraso_herdado"] >= 30]
    if "🟠 30-60 min" not in severity_filter:
        filtered_cascade = filtered_cascade[
            (filtered_cascade["atraso_herdado"] < 30) | (filtered_cascade["atraso_herdado"] >= 60)
        ]
    if "🔴 >60 min" not in severity_filter:
        filtered_cascade = filtered_cascade[filtered_cascade["atraso_herdado"] < 60]

    st.dataframe(
        filtered_cascade[[
            "⚠️", "carreira", "designacao", "rede", "regiao",
            "stop_enlace", "atraso_herdado", "profundidade",
            "origem_atraso", "origem_desig", "causa_origem", "Tipo",
        ]].rename(columns={
            "⚠️": "⚠️",
            "carreira": "Carreira Afectada",
            "designacao": "Designação",
            "rede": "Rede",
            "regiao": "Região",
            "stop_enlace": "Paragem de Enlace",
            "atraso_herdado": "Atraso Herdado (min)",
            "profundidade": "Profundidade",
            "origem_atraso": "Origem (Carreira)",
            "origem_desig": "Origem (Nome)",
            "causa_origem": "Causa Origem",
            "Tipo": "Tipo",
        }),
        hide_index=True,
        use_container_width=True,
    )

    st.markdown("---")

    # ── Cascade visualisation (Sankey) ───────────────────────────────────────
    st.subheader("Visualização da cascata de atrasos")

    with st.spinner("A gerar Sankey de cascata…"):
        fig_cascade = _build_cascade_sankey(initial_delays, cascade_df, routes_dict)
    st.plotly_chart(fig_cascade, use_container_width=True)


def _build_cascade_sankey(
    initial_delays: list[dict],
    cascade_df: pd.DataFrame,
    routes_dict: dict,
) -> go.Figure:
    """Build a Sankey diagram showing delay propagation."""
    nodes: list[str] = []
    node_colors: list[str] = []
    node_idx: dict[str, int] = {}

    def _node(label: str, color: str) -> int:
        if label not in node_idx:
            node_idx[label] = len(nodes)
            nodes.append(label)
            node_colors.append(color)
        return node_idx[label]

    sources, targets, values, link_colors = [], [], [], []

    # Initial delay nodes
    for d in initial_delays:
        c = d["carreira"]
        delay = d["delay_minutes"]
        desig = routes_dict.get(c, {}).get("designacao", "")
        label = f"{c}\n{desig[:20]}" if desig else str(c)
        color = _delay_color(delay)
        _node(label, color)

    # Cascade edges
    for _, row in cascade_df.iterrows():
        orig = row["origem_atraso"]
        orig_desig = routes_dict.get(orig, {}).get("designacao", "")
        orig_label = f"{orig}\n{orig_desig[:20]}" if orig_desig else str(orig)

        aff = row["carreira"]
        aff_desig = row.get("designacao", "")
        aff_label = f"{aff}\n{aff_desig[:20]}" if aff_desig else str(aff)

        delay_h = row["atraso_herdado"]
        color = _delay_color(delay_h)

        src_idx = _node(orig_label, _delay_color(
            next((d["delay_minutes"] for d in initial_delays if d["carreira"] == orig), 60)
        ))
        tgt_idx = _node(aff_label, color)

        sources.append(src_idx)
        targets.append(tgt_idx)
        values.append(max(1, delay_h))
        link_colors.append(_hex_to_rgba(color, 0.67))

    if not sources:
        return go.Figure().update_layout(title="Sem dados de cascata para visualizar")

    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(
            pad=10,
            thickness=15,
            label=nodes,
            color=node_colors,
            hovertemplate="<b>%{label}</b><extra></extra>",
        ),
        link=dict(
            source=sources,
            target=targets,
            value=values,
            color=link_colors,
            hovertemplate="Atraso herdado: %{value:.0f} min<extra></extra>",
        ),
    ))
    fig.update_layout(
        title_text="Propagação de Atrasos em Cascata",
        height=600,
        font_size=9,
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Pesquisa de Carreira
# ═══════════════════════════════════════════════════════════════════════════════

def tab_pesquisa_carreira():
    st.header("🔍 Pesquisa de Carreira")

    routes = _get_state("routes")
    graph = _get_state("graph")

    if not routes:
        st.warning("⚠️ Carregue primeiro o ficheiro de rede na tab **Rede Semanal**.")
        return

    # Build search index
    routes_dict = _get_state("routes_dict", {r["carreira"]: r for r in routes})

    # Search input
    search_query = st.text_input(
        "Pesquisar carreira (número ou nome)",
        placeholder="Ex: 111140000 ou CPLS",
        key="carreira_search",
    )

    if not search_query:
        st.info("Digite um número ou nome de carreira para pesquisar.")
        return

    # Search
    query_lower = search_query.strip().lower()
    matches = []
    for r in routes:
        if (
            query_lower in str(r["carreira"]).lower()
            or query_lower in r.get("designacao", "").lower()
            or query_lower in r.get("origem", "").lower()
            or query_lower in r.get("destino", "").lower()
        ):
            matches.append(r)

    if not matches:
        st.warning(f"Nenhuma carreira encontrada para «{search_query}».")
        return

    st.markdown(f"**{len(matches)} resultado(s) encontrado(s)**")

    # Select from matches
    match_options = [
        f"{m['carreira']} — {m.get('designacao', '')} ({m.get('rede', '')})"
        for m in matches
    ]
    selected_idx = st.selectbox(
        "Seleccionar carreira",
        options=range(len(matches)),
        format_func=lambda i: match_options[i],
        key="carreira_select",
    )

    selected_route = matches[selected_idx]
    carreira_id = selected_route["carreira"]

    # ── Route details ────────────────────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Carreira", carreira_id)
    col2.metric("Rede", selected_route.get("rede", "—"))
    col3.metric("Região", selected_route.get("regiao", "—"))
    col4.metric("Transportador", selected_route.get("transportador", "—"))

    st.markdown(f"**Designação:** {selected_route.get('designacao', '—')}")
    st.markdown(f"**Periodicidade:** {selected_route.get('periodicidade', '—')} | **Veículo:** {selected_route.get('veiculo', '—')}")

    # Route type badge
    if selected_route.get("is_reverse_logistics", False):
        st.markdown("**Tipo:** :orange[↩️ Logística Inversa]")
    else:
        st.markdown("**Tipo:** :green[📦 Carga]")

    # ── Stop chain table ─────────────────────────────────────────────────────
    st.subheader("Cadeia de paragens")

    stops = selected_route.get("stops", [])
    if stops:
        def _fmt_time(mins):
            if mins is None:
                return "—"
            h, m = divmod(int(mins) % 1440, 60)
            return f"{h:02d}:{m:02d}"

        stops_df = pd.DataFrame([
            {
                "N.º": s.get("n_par", i + 1),
                "Paragem": s.get("paragem", ""),
                "Chegada": _fmt_time(s.get("hpc")),
                "Partida": _fmt_time(s.get("hpp")),
                "T.Paragem (min)": s.get("tp", ""),
                "T.Trânsito (min)": s.get("tt", ""),
                "Km Acum.": s.get("km", ""),
                "Tipo": s.get("tipo", ""),
            }
            for i, s in enumerate(stops)
        ])

        st.dataframe(
            stops_df.style.apply(
                lambda row: [
                    "background-color: #E3F2FD" if row["Tipo"] == "ORIGEM"
                    else "background-color: #E8F5E9" if row["Tipo"] == "DESTINO"
                    else ""
                ] * len(row),
                axis=1,
            ),
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("Esta carreira não tem paragens definidas.")

    st.markdown("---")

    # ── Upstream / Downstream dependencies ──────────────────────────────────
    if graph is None:
        st.info("Grafo de dependências não disponível.")
        return

    col_up, col_down = st.columns(2)

    with col_up:
        st.subheader("⬆️ Dependências a montante")
        st.caption("Carreiras que alimentam esta")
        upstream = get_upstream(graph, carreira_id)
        if upstream:
            up_df = pd.DataFrame(upstream)
            st.dataframe(
                up_df[["carreira", "designacao", "rede", "regiao", "depth", "via_stop", "window_min"]].rename(
                    columns={
                        "carreira": "Carreira", "designacao": "Designação",
                        "rede": "Rede", "regiao": "Região",
                        "depth": "Distância", "via_stop": "Paragem Enlace",
                        "window_min": "Janela (min)",
                    }
                ),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info("Sem dependências a montante.")

    with col_down:
        st.subheader("⬇️ Dependências a jusante")
        st.caption("Carreiras que dependem desta")
        downstream = get_downstream(graph, carreira_id)
        if downstream:
            down_df = pd.DataFrame(downstream)
            st.dataframe(
                down_df[["carreira", "designacao", "rede", "regiao", "depth", "via_stop", "window_min"]].rename(
                    columns={
                        "carreira": "Carreira", "designacao": "Designação",
                        "rede": "Rede", "regiao": "Região",
                        "depth": "Distância", "via_stop": "Paragem Enlace",
                        "window_min": "Janela (min)",
                    }
                ),
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.info("Sem dependências a jusante.")

    st.markdown("---")

    # ── Mini network graph centred on selected route ─────────────────────────
    st.subheader("Mini-grafo de dependências")

    depth_limit = st.slider(
        "Profundidade máxima",
        min_value=1, max_value=4, value=2,
        key="mini_graph_depth",
    )

    with st.spinner("A gerar mini-grafo…"):
        fig_mini = _build_mini_graph(
            carreira_id, graph, routes_dict,
            upstream=get_upstream(graph, carreira_id, max_depth=depth_limit),
            downstream=get_downstream(graph, carreira_id, max_depth=depth_limit),
        )
    st.plotly_chart(fig_mini, use_container_width=True)


def _build_mini_graph(
    center_id: int,
    graph: nx.DiGraph,
    routes_dict: dict,
    upstream: list[dict],
    downstream: list[dict],
) -> go.Figure:
    """Build a mini Plotly network graph centred on one route."""
    # Gather nodes
    nodes_to_show = {center_id}
    for u in upstream:
        nodes_to_show.add(u["carreira"])
    for d in downstream:
        nodes_to_show.add(d["carreira"])

    sub = graph.subgraph(nodes_to_show)

    if len(sub.nodes) == 0:
        return go.Figure().update_layout(title="Sem dados para o grafo")

    try:
        pos = nx.spring_layout(sub, seed=42)
    except Exception:
        pos = {n: (i, 0) for i, n in enumerate(sub.nodes)}

    # Edge traces
    edge_x, edge_y = [], []
    for a, b in sub.edges():
        if a in pos and b in pos:
            ax, ay = pos[a]
            bx, by = pos[b]
            edge_x += [ax, bx, None]
            edge_y += [ay, by, None]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=edge_x, y=edge_y,
        mode="lines",
        line=dict(color="#BDBDBD", width=1),
        hoverinfo="none",
        showlegend=False,
    ))

    # Node traces by role
    categories = {
        "central": {"ids": [center_id], "color": "#E91E63", "symbol": "star", "size": 18},
        "upstream": {"ids": [u["carreira"] for u in upstream], "color": "#FF9800", "symbol": "triangle-up", "size": 12},
        "downstream": {"ids": [d["carreira"] for d in downstream], "color": "#2196F3", "symbol": "circle", "size": 10},
    }

    for cat, cfg in categories.items():
        cat_nodes = [n for n in cfg["ids"] if n in pos]
        if not cat_nodes:
            continue
        nx_list = [pos[n][0] for n in cat_nodes]
        ny_list = [pos[n][1] for n in cat_nodes]
        labels = [
            f"{n} — {routes_dict.get(n, {}).get('designacao', '')[:30]}"
            for n in cat_nodes
        ]
        fig.add_trace(go.Scatter(
            x=nx_list, y=ny_list,
            mode="markers+text",
            marker=dict(
                symbol=cfg["symbol"],
                size=cfg["size"],
                color=cfg["color"],
                line=dict(width=1, color="#fff"),
            ),
            text=[str(n) for n in cat_nodes],
            textposition="top center",
            textfont=dict(size=8),
            name=cat.capitalize(),
            customdata=labels,
            hovertemplate="%{customdata}<extra></extra>",
        ))

    fig.update_layout(
        showlegend=True,
        hovermode="closest",
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        height=450,
        margin=dict(l=10, r=10, t=40, b=10),
        title=f"Dependências da carreira {center_id}",
        paper_bgcolor="#FAFAFA",
    )
    return fig


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4 — Sugestões de Encadeamento
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_min(minutes: Optional[int]) -> str:
    if minutes is None:
        return "—"
    h, m = divmod(int(minutes) % 1440, 60)
    return f"{h:02d}:{m:02d}"


# Weekday numbers: 0=Mon … 4=Fri, 5=Sat, 6=Sun
_DAY_ABBR: dict[str, int] = {
    "2a": 0, "2ª": 0, "seg": 0,
    "3a": 1, "3ª": 1, "ter": 1,
    "4a": 2, "4ª": 2, "qua": 2,
    "5a": 3, "5ª": 3, "qui": 3,
    "6a": 4, "6ª": 4, "sex": 4,
    "sab": 5, "sáb": 5, "sab.": 5,
    "dom": 6, "dom.": 6,
}

_DAY_NAMES = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


def _parse_periodicidade(s: str) -> frozenset[int]:
    """Convert a Portuguese periodicidade string to a frozenset of weekday numbers.

    Unknown / empty strings return all 7 days (conservative: never filter out).
    """
    if not s or s.strip() == "":
        return frozenset(range(7))

    raw = s.strip()
    # Normalise ordinal indicators → "a", remove "feira/Feira", collapse spaces
    norm = (
        raw.lower()
        .replace("ª", "a")   # feminine ordinal U+00AA
        .replace("°", "a")   # degree sign U+00B0
        .replace("º", "a")   # masculine ordinal U+00BA  (e.g. "6º")
        .replace("feira", "")
        .replace("  ", " ")
        .strip()
    )

    def _tok_to_day(tok: str) -> Optional[int]:
        t = tok.strip().rstrip(".")
        return _DAY_ABBR.get(t)

    # Range: "X a Y"
    if " a " in norm:
        left, right = norm.split(" a ", 1)
        start = _tok_to_day(left.strip().split()[-1])
        end   = _tok_to_day(right.strip().split()[0])
        if start is not None and end is not None:
            if end >= start:
                return frozenset(range(start, end + 1))
            # Wrap-around (e.g. "Dom a 5ª" = 6,0,1,2,3)
            return frozenset(range(start, 7)) | frozenset(range(0, end + 1))

    # Enumeration: "X e Y" or "X, Y e Z"
    if " e " in norm or "," in norm:
        tokens = norm.replace(",", " ").replace(" e ", " ").split()
        days = {_tok_to_day(t) for t in tokens}
        days.discard(None)
        if days:
            return frozenset(days)

    # Single token (e.g. "Sáb", "Dom", "2ª Feira")
    tokens = norm.split()
    for tok in tokens:
        d = _tok_to_day(tok)
        if d is not None:
            return frozenset({d})

    return frozenset(range(7))  # fallback — don't filter


def _dias_label(days: frozenset[int]) -> str:
    """Human-readable day set, e.g. 'Seg–Sex' or 'Sáb · Dom'."""
    if not days:
        return "—"
    sorted_days = sorted(days)
    if sorted_days == list(range(7)):
        return "Todos"
    if sorted_days == list(range(5)):
        return "Seg–Sex"
    if sorted_days == list(range(6)):
        return "Seg–Sáb"
    # Compact consecutive ranges
    parts = []
    start = sorted_days[0]
    prev  = sorted_days[0]
    for d in sorted_days[1:]:
        if d == prev + 1:
            prev = d
        else:
            parts.append(_DAY_NAMES[start] if start == prev else f"{_DAY_NAMES[start]}–{_DAY_NAMES[prev]}")
            start = prev = d
    parts.append(_DAY_NAMES[start] if start == prev else f"{_DAY_NAMES[start]}–{_DAY_NAMES[prev]}")
    return " · ".join(parts)


def _build_chaining_suggestions(
    routes: list[dict],
    window_min: int = 90,
    exclude_same_route: bool = True,
) -> pd.DataFrame:
    """Find pairs (A, B) where A ends at hub X and B departs from hub X within
    window_min, on at least one common operating day.

    Only pairs that share ≥1 weekday are returned (prevents Sat routes being
    chained with Mon routes, etc.).
    """
    from collections import defaultdict

    endings: dict[str, list] = defaultdict(list)
    starts:  dict[str, list] = defaultdict(list)

    for r in routes:
        stops = r.get("stops", [])
        if not stops:
            continue

        last  = stops[-1]
        first = stops[0]

        hub_end   = (last.get("paragem") or "").strip()
        hub_start = (first.get("paragem") or "").strip()
        arr_end   = last.get("hpc")
        dep_start = first.get("hpp")

        days = _parse_periodicidade(r.get("periodicidade", "") or "")

        if hub_end and arr_end is not None:
            endings[hub_end].append((r, int(arr_end), days))
        if hub_start and dep_start is not None:
            starts[hub_start].append((r, int(dep_start), days))

    rows = []
    hubs = set(endings) & set(starts)

    for hub in sorted(hubs):
        for route_a, t_arr, days_a in sorted(endings[hub], key=lambda x: x[1]):
            for route_b, t_dep, days_b in sorted(starts[hub], key=lambda x: x[1]):
                if exclude_same_route and route_a["carreira"] == route_b["carreira"]:
                    continue

                # Only pair routes that share at least one operating day
                common_days = days_a & days_b
                if not common_days:
                    continue

                delta = t_dep - t_arr
                if delta < 0:
                    delta += 1440
                if delta <= 0 or delta > window_min:
                    continue

                rows.append({
                    "hub":             hub,
                    "carreira_a":      route_a["carreira"],
                    "designacao_a":    route_a.get("designacao", ""),
                    "rede_a":          route_a.get("rede", ""),
                    "regiao_a":        route_a.get("regiao", ""),
                    "transportador_a": route_a.get("transportador", ""),
                    "periodo_a":       route_a.get("periodicidade", ""),
                    "chegada_a":       _fmt_min(t_arr),
                    "carreira_b":      route_b["carreira"],
                    "designacao_b":    route_b.get("designacao", ""),
                    "rede_b":          route_b.get("rede", ""),
                    "regiao_b":        route_b.get("regiao", ""),
                    "transportador_b": route_b.get("transportador", ""),
                    "periodo_b":       route_b.get("periodicidade", ""),
                    "partida_b":       _fmt_min(t_dep % 1440),
                    "janela_min":      delta,
                    "dias_comuns":     _dias_label(common_days),
                    "inter_rede":      route_a.get("rede", "") != route_b.get("rede", ""),
                    "inter_transp":    route_a.get("transportador", "") != route_b.get("transportador", ""),
                })

    return pd.DataFrame(rows)


def tab_sugestoes_encadeamento():
    st.header("💡 Sugestões de Encadeamento")
    st.caption(
        "Pares de ligações onde **A termina** e **B começa** no mesmo hub dentro de uma "
        "janela de tempo — o mesmo veículo/condutor poderia realizar ambas em sequência."
    )

    routes = _get_state("routes")
    if not routes:
        st.warning("⚠️ Carregue primeiro o ficheiro de rede na tab **Rede Semanal**.")
        return

    # ── Controls row 1 ───────────────────────────────────────────────────────
    col_w, col_h, col_r, col_t = st.columns(4)
    window = col_w.slider(
        "Janela de encadeamento (min)", 10, 180, 90, 5,
        help="Tempo máximo entre chegada de A e partida de B no mesmo hub",
        key="chain_window",
    )
    hub_filter = col_h.text_input("Filtrar por hub", "", placeholder="ex: CO PAL",
                                   key="chain_hub")
    rede_options = ["Todas"] + sorted({r.get("rede", "").split()[0]
                                        for r in routes if r.get("rede")})
    rede_filter = col_r.selectbox("Rede de B", rede_options, key="chain_rede_b")
    inter_only  = col_t.checkbox("Apenas inter-redes (R1↔R2↔R3)", value=False,
                                  key="chain_inter")

    # ── Controls row 2 — transportador & day filters ─────────────────────────
    col_ta, col_tb, col_day, col_same_t = st.columns(4)
    all_transportadores = sorted({r.get("transportador", "") for r in routes if r.get("transportador")})
    transp_options = ["Todos"] + all_transportadores
    transp_a_filter = col_ta.selectbox("Transportador de A", transp_options, key="chain_transp_a")
    transp_b_filter = col_tb.selectbox("Transportador de B", transp_options, key="chain_transp_b")

    # Day-of-week filter
    day_options = ["Todos"] + _DAY_NAMES
    day_filter = col_day.selectbox(
        "Dia da semana", day_options, key="chain_day",
        help="Mostrar apenas encadeamentos que ocorrem neste dia",
    )
    same_transp_only = col_same_t.checkbox(
        "Mesmo transportador (A = B)", value=False, key="chain_same_transp",
        help="Útil para optimizar frota do mesmo operador",
    )

    with st.spinner("A calcular encadeamentos…"):
        df = _build_chaining_suggestions(routes, window_min=window)

    if df.empty:
        st.info("Sem sugestões para os parâmetros actuais.")
        return

    # ── Apply filters ─────────────────────────────────────────────────────────
    if hub_filter:
        df = df[df["hub"].str.contains(hub_filter, case=False, na=False)]
    if rede_filter != "Todas":
        df = df[df["rede_b"].str.startswith(rede_filter)]
    if inter_only:
        df = df[df["inter_rede"]]
    if transp_a_filter != "Todos":
        df = df[df["transportador_a"] == transp_a_filter]
    if transp_b_filter != "Todos":
        df = df[df["transportador_b"] == transp_b_filter]
    if same_transp_only:
        df = df[df["transportador_a"] == df["transportador_b"]]
    if day_filter != "Todos":
        day_num = _DAY_NAMES.index(day_filter)
        df = df[df["dias_comuns"].apply(
            lambda label: day_filter in label or "Todos" in label or
            # Check by re-parsing the dias_comuns label isn't ideal;
            # filter on the raw periodicidade strings instead
            False
        )]
        # Re-filter properly: check if the day number appears in both routes' periodicidades
        # We stored periodo_a / periodo_b, so parse them again
        df = df[
            df["periodo_a"].apply(lambda p: day_num in _parse_periodicidade(p)) &
            df["periodo_b"].apply(lambda p: day_num in _parse_periodicidade(p))
        ]

    if df.empty:
        st.info("Sem sugestões para os filtros seleccionados.")
        return

    st.markdown(f"**{len(df)} encadeamentos possíveis** em **{df['hub'].nunique()}** hubs")

    # ── Summary by hub ───────────────────────────────────────────────────────
    st.subheader("Hubs com mais oportunidades")
    hub_summary = (
        df.groupby("hub")
        .agg(
            encadeamentos=("janela_min", "count"),
            janela_media=("janela_min", "mean"),
            janela_min_val=("janela_min", "min"),
        )
        .round(1)
        .sort_values("encadeamentos", ascending=False)
        .reset_index()
        .rename(columns={
            "hub": "Hub", "encadeamentos": "Sugestões",
            "janela_media": "Janela média (min)", "janela_min_val": "Janela mín (min)",
        })
    )
    st.dataframe(hub_summary, hide_index=True, use_container_width=True)

    st.markdown("---")

    # ── Detail by hub ─────────────────────────────────────────────────────────
    st.subheader("Detalhe por hub")

    top_hubs = hub_summary["Hub"].tolist()
    selected_hub = st.selectbox("Seleccionar hub", top_hubs, key="chain_hub_sel")

    hub_df = df[df["hub"] == selected_hub].sort_values("janela_min")

    display_rows = []
    for _, row in hub_df.iterrows():
        display_rows.append({
            "Dias": row["dias_comuns"],
            "Chegada": row["chegada_a"],
            "Ligação A": row["designacao_a"] or str(row["carreira_a"]),
            "Rede A": row["rede_a"],
            "Transp. A": row["transportador_a"],
            "Período A": row["periodo_a"],
            "⏱ Janela": f"{row['janela_min']} min",
            "Partida": row["partida_b"],
            "Ligação B": row["designacao_b"] or str(row["carreira_b"]),
            "Rede B": row["rede_b"],
            "Transp. B": row["transportador_b"],
            "Período B": row["periodo_b"],
        })

    st.dataframe(
        pd.DataFrame(display_rows),
        hide_index=True,
        use_container_width=True,
        column_config={
            "⏱ Janela": st.column_config.TextColumn(width="small"),
            "Chegada":  st.column_config.TextColumn(width="small"),
            "Partida":  st.column_config.TextColumn(width="small"),
            "Dias":     st.column_config.TextColumn(width="medium"),
        },
    )

    # ── Visual timeline per hub ───────────────────────────────────────────────
    st.markdown("---")
    st.subheader(f"Diagrama de encadeamentos — {selected_hub}")

    fig = go.Figure()
    y_pos = 0
    seen_routes = {}
    palette = ["#2196F3", "#4CAF50", "#FF9800", "#9C27B0", "#F44336",
               "#00BCD4", "#8BC34A", "#FF5722", "#607D8B", "#E91E63"]

    for _, row in hub_df.iterrows():
        # Route A bar (ending at hub)
        arr_m = int(row["chegada_a"].replace(":", "")) // 100 * 60 + \
                int(row["chegada_a"].replace(":", "")) % 100
        dep_m = int(row["partida_b"].replace(":", "")) // 100 * 60 + \
                int(row["partida_b"].replace(":", "")) % 100
        if dep_m < arr_m:
            dep_m += 1440

        ca = row["carreira_a"]
        cb = row["carreira_b"]
        col_a = palette[hash(str(ca)) % len(palette)]
        col_b = palette[hash(str(cb)) % len(palette)]

        # Arrival marker
        fig.add_trace(go.Scatter(
            x=[arr_m], y=[y_pos],
            mode="markers+text",
            marker=dict(symbol="triangle-right", size=12, color=col_a),
            text=[f"↘ {row['designacao_a'][:25]}"],
            textposition="middle right",
            textfont=dict(size=9),
            hovertemplate=f"<b>CHEGADA</b> {row['chegada_a']}<br>{row['designacao_a']}<br>Rede: {row['rede_a']}<extra></extra>",
            showlegend=False,
        ))
        # Departure marker
        fig.add_trace(go.Scatter(
            x=[dep_m], y=[y_pos],
            mode="markers+text",
            marker=dict(symbol="triangle-right", size=12, color=col_b),
            text=[f"↗ {row['designacao_b'][:25]}"],
            textposition="middle right",
            textfont=dict(size=9),
            hovertemplate=f"<b>PARTIDA</b> {row['partida_b']}<br>{row['designacao_b']}<br>Rede: {row['rede_b']}<extra></extra>",
            showlegend=False,
        ))
        # Connection window bar
        fig.add_shape(
            type="rect",
            x0=arr_m, x1=dep_m, y0=y_pos - 0.3, y1=y_pos + 0.3,
            fillcolor=_hex_to_rgba("#FFC107", 0.35),
            line=dict(color="#FFC107", width=1),
        )
        y_pos += 1

    # X axis: convert minutes to HH:MM ticks
    tick_vals = list(range(0, 1441, 60))
    tick_text = [f"{h:02d}:00" for h in range(25)]
    fig.update_layout(
        xaxis=dict(
            tickvals=tick_vals, ticktext=tick_text,
            title="Hora", range=[0, 1440],
        ),
        yaxis=dict(showticklabels=False, title=""),
        height=max(300, 40 + len(hub_df) * 50),
        margin=dict(l=20, r=200, t=30, b=40),
        showlegend=False,
        title=f"Janelas de encadeamento em {selected_hub}",
    )
    st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")
    # ── Export ───────────────────────────────────────────────────────────────
    csv = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ Exportar todas as sugestões (CSV)",
        data=csv,
        file_name="sugestoes_encadeamento.csv",
        mime="text/csv",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 5 — Ocupação & Grupagem
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_occupancy(exec_df: pd.DataFrame) -> pd.DataFrame:
    """Extract occupancy stats per route from execution DataFrame (P-rows only)."""
    df = exec_df.copy()
    p = df[df["cp"].fillna("").str.strip() == "P"].copy()

    if "ocupacao" not in df.columns or p.empty:
        return pd.DataFrame()

    p["_occ"] = pd.to_numeric(p["ocupacao"], errors="coerce").fillna(0)
    p["_occ_atr"] = (
        pd.to_numeric(p["ocupacao_atrelado"], errors="coerce").fillna(0)
        if "ocupacao_atrelado" in p.columns else 0.0
    )

    agg = (
        p.groupby("carreira_str")
        .agg(
            occ_max=("_occ", "max"),
            occ_mean=("_occ", "mean"),
            occ_atr_max=("_occ_atr", "max"),
            n_medicoes=("_occ", "count"),
            rede=("rede", "first"),
            tp_re=("tp_re", "first"),
            ligacao=("ligacao", "first"),
        )
        .reset_index()
    )
    agg["occ_max"]  = agg["occ_max"].round(1)
    agg["occ_mean"] = agg["occ_mean"].round(1)
    agg["regiao"]   = agg["tp_re"].apply(_regiao_label)
    return agg


def _find_grouping_suggestions(
    occ_df: pd.DataFrame,
    routes: list[dict],
    graph,
    max_combined: float = 100.0,
) -> pd.DataFrame:
    """Find pairs of same-O/D routes whose combined max-occupancy fits in one vehicle.

    Returns a DataFrame of candidate consolidations sorted by dependency risk
    (safe first) then combined occupancy ascending.
    """
    # Build carreira_str → route dict lookup
    routes_by_str: dict[str, dict] = {str(r["carreira"]): r for r in routes}

    df = occ_df.copy()
    df["origem"]     = df["carreira_str"].map(lambda c: routes_by_str.get(c, {}).get("origem", ""))
    df["destino"]    = df["carreira_str"].map(lambda c: routes_by_str.get(c, {}).get("destino", ""))
    df["veiculo"]    = df["carreira_str"].map(lambda c: routes_by_str.get(c, {}).get("veiculo", ""))
    df["designacao"] = df["carreira_str"].map(
        lambda c: routes_by_str.get(c, {}).get("designacao", "") or df.loc[df["carreira_str"] == c, "ligacao"].iat[0]
        if c in routes_by_str else ""
    )
    df["is_reverse"] = df["carreira_str"].map(
        lambda c: routes_by_str.get(c, {}).get("is_reverse_logistics", False)
    )

    # Keep only cargo routes with valid O-D and measured occupancy
    valid = df[
        (~df["is_reverse"]) &
        (df["occ_max"] > 0) &
        df["origem"].notna() & (df["origem"] != "") &
        df["destino"].notna() & (df["destino"] != "")
    ].copy()

    # Dependency presence flags (any node with edges has connection risk)
    has_deps: set[str] = set()
    if graph is not None:
        for n in graph.nodes:
            if graph.degree(n) > 0:
                has_deps.add(str(n))

    rows = []
    for (orig, dest), grp in valid.groupby(["origem", "destino"]):
        if not orig or not dest:
            continue
        members = grp.to_dict("records")
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                combined = a["occ_max"] + b["occ_max"]
                if combined > max_combined:
                    continue

                dep_a = a["carreira_str"] in has_deps
                dep_b = b["carreira_str"] in has_deps

                rows.append({
                    "origem":        orig,
                    "destino":       dest,
                    "carreira_a":    a["carreira_str"],
                    "designacao_a":  a.get("designacao") or a.get("ligacao", ""),
                    "rede_a":        a["rede"],
                    "occ_max_a":     a["occ_max"],
                    "carreira_b":    b["carreira_str"],
                    "designacao_b":  b.get("designacao") or b.get("ligacao", ""),
                    "rede_b":        b["rede"],
                    "occ_max_b":     b["occ_max"],
                    "occ_combinada": round(combined, 1),
                    "capacidade_livre": round(100.0 - combined, 1),
                    "dep_risk":      dep_a or dep_b,
                    "dep_a":         dep_a,
                    "dep_b":         dep_b,
                })

    if not rows:
        return pd.DataFrame()

    return (
        pd.DataFrame(rows)
        .sort_values(["dep_risk", "occ_combinada"], ascending=[True, True])
        .reset_index(drop=True)
    )


def tab_ocupacao_grupagem():
    st.header("📦 Ocupação & Grupagem")
    st.caption(
        "Análise de ocupação das viaturas por carreira e sugestões de consolidação "
        "de ligações com o mesmo O/D, sem prejuízo dos enlaces da rede."
    )

    routes = _get_state("routes")
    graph  = _get_state("graph")
    exec_df = _get_state("exec_df")

    # ── Guard: need both files ────────────────────────────────────────────────
    missing = []
    if not routes:
        missing.append("ficheiro de rede (tab **Rede Semanal**)")
    if exec_df is None:
        missing.append("ficheiro de execução (tab **Análise de Impacto de Atrasos**)")
    if missing:
        st.warning("⚠️ Carregue primeiro o " + " e o ".join(missing) + ".")
        return

    # ── Compute occupancy per route ───────────────────────────────────────────
    with st.spinner("A calcular ocupações…"):
        occ_df = _compute_occupancy(exec_df)

    if occ_df.empty:
        st.error("Coluna de ocupação não encontrada no ficheiro de execução.")
        return

    n_routes   = len(occ_df)
    n_with_occ = int((occ_df["occ_max"] > 0).sum())
    mean_occ   = occ_df[occ_df["occ_max"] > 0]["occ_max"].mean()
    n_low      = int((occ_df["occ_max"].between(0.01, 50)).sum())

    # ── KPI cards ─────────────────────────────────────────────────────────────
    st.subheader("📊 Visão geral da ocupação")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Carreiras analisadas", n_routes)
    k2.metric("Com ocupação registada", n_with_occ)
    k3.metric("Ocupação média (pico)", f"{mean_occ:.1f}%")
    k4.metric("Carreiras < 50 % (pico)", n_low,
              help="Candidatas a grupagem ou redimensionamento")

    st.markdown("---")

    # ── Distribution histogram ────────────────────────────────────────────────
    col_hist, col_rede = st.columns([2, 1])

    with col_hist:
        st.subheader("Distribuição de ocupação máxima")
        bins   = [0, 25, 50, 75, 90, 100]
        labels = ["0–25 %", "26–50 %", "51–75 %", "76–90 %", "91–100 %"]
        occ_pos = occ_df[occ_df["occ_max"] > 0]["occ_max"]
        dist = pd.cut(occ_pos, bins=bins, labels=labels, right=True).value_counts().sort_index()
        fig_hist = go.Figure(go.Bar(
            x=dist.index.astype(str).tolist(),
            y=dist.values.tolist(),
            marker_color=["#D32F2F", "#FF9800", "#FFC107", "#4CAF50", "#1565C0"],
            text=dist.values.tolist(),
            textposition="outside",
        ))
        fig_hist.update_layout(
            xaxis_title="Intervalo de ocupação",
            yaxis_title="N.º carreiras",
            height=300,
            margin=dict(l=20, r=20, t=20, b=20),
            showlegend=False,
        )
        st.plotly_chart(fig_hist, use_container_width=True)

    with col_rede:
        st.subheader("Por tipo de rede")
        rede_agg = (
            occ_df[occ_df["occ_max"] > 0]
            .groupby("rede")
            .agg(media=("occ_max", "mean"), n=("occ_max", "count"),
                 abaixo50=("occ_max", lambda x: (x <= 50).sum()))
            .round(1)
            .reset_index()
            .rename(columns={"rede": "Rede", "media": "Média (%)",
                             "n": "Carreiras", "abaixo50": "< 50 %"})
        )
        st.dataframe(rede_agg, hide_index=True, use_container_width=True)

        st.subheader("Por região")
        reg_agg = (
            occ_df[occ_df["occ_max"] > 0]
            .groupby("regiao")
            .agg(media=("occ_max", "mean"), n=("occ_max", "count"),
                 abaixo50=("occ_max", lambda x: (x <= 50).sum()))
            .round(1)
            .reset_index()
            .rename(columns={"regiao": "Região", "media": "Média (%)",
                             "n": "Carreiras", "abaixo50": "< 50 %"})
            .sort_values("Média (%)")
        )
        st.dataframe(reg_agg, hide_index=True, use_container_width=True)

    st.markdown("---")

    # ── Route occupancy table with filters ────────────────────────────────────
    st.subheader("Detalhe por carreira")

    col_f1, col_f2, col_f3 = st.columns(3)
    rede_opts = ["Todas"] + sorted(occ_df["rede"].dropna().unique().tolist())
    rede_sel  = col_f1.selectbox("Rede", rede_opts, key="occ_rede_filter")
    reg_opts  = ["Todas"] + sorted(occ_df["regiao"].dropna().unique().tolist())
    reg_sel   = col_f2.selectbox("Região", reg_opts, key="occ_reg_filter")
    max_occ_filter = col_f3.slider(
        "Ocupação máxima até…", 1, 100, 100, 5, key="occ_max_filter",
        help="Mostrar apenas carreiras com occ_max ≤ este valor"
    )

    disp_df = occ_df.copy()
    if rede_sel != "Todas":
        disp_df = disp_df[disp_df["rede"] == rede_sel]
    if reg_sel != "Todas":
        disp_df = disp_df[disp_df["regiao"] == reg_sel]
    disp_df = disp_df[disp_df["occ_max"] <= max_occ_filter]
    disp_df = disp_df.sort_values("occ_max")

    def _occ_emoji(v):
        if v <= 25:   return "🔴"
        if v <= 50:   return "🟠"
        if v <= 75:   return "🟡"
        if v <= 90:   return "🟢"
        return "🔵"

    disp_df[""] = disp_df["occ_max"].apply(_occ_emoji)
    disp_df["Atrelado"] = disp_df["occ_atr_max"].apply(
        lambda v: f"{v:.0f}%" if v > 0 else "—"
    )

    st.dataframe(
        disp_df[[
            "", "carreira_str", "ligacao", "rede", "regiao",
            "occ_max", "occ_mean", "Atrelado", "n_medicoes",
        ]].rename(columns={
            "carreira_str": "Carreira", "ligacao": "Ligação",
            "rede": "Rede", "regiao": "Região",
            "occ_max": "Occ. Máx (%)", "occ_mean": "Occ. Média (%)",
            "n_medicoes": "N.º Medições",
        }),
        hide_index=True,
        use_container_width=True,
    )

    # CSV export
    csv_occ = disp_df.drop(columns=[""]).to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ Exportar ocupações (CSV)",
        data=csv_occ,
        file_name="ocupacoes.csv",
        mime="text/csv",
        key="dl_occ",
    )

    st.markdown("---")

    # ═══════════════════════════════════════════════════════════════════════════
    # Grouping suggestions
    # ═══════════════════════════════════════════════════════════════════════════
    st.subheader("🔀 Sugestões de Grupagem")
    st.caption(
        "Pares de carreiras com o **mesmo O/D** cuja ocupação combinada cabe numa só viatura "
        "(≤ limiar configurável). Carreiras com dependências no grafo estão assinaladas — "
        "verifique o impacto antes de consolidar."
    )

    col_g1, col_g2 = st.columns(2)
    max_comb = col_g1.slider(
        "Ocupação combinada máxima (%)", 50, 100, 100, 5,
        key="group_max_occ",
        help="Só sugere pares cuja soma das ocupações máximas não exceda este limiar",
    )
    safe_only = col_g2.checkbox(
        "Mostrar apenas sem risco de dependências",
        value=False,
        key="group_safe_only",
    )

    with st.spinner("A calcular sugestões de grupagem…"):
        grp_df = _find_grouping_suggestions(occ_df, routes, graph, max_combined=max_comb)

    if grp_df.empty:
        st.info(
            "Sem sugestões de grupagem para os parâmetros actuais. "
            "Experimente aumentar o limiar de ocupação combinada."
        )
        return

    if safe_only:
        grp_df = grp_df[~grp_df["dep_risk"]]

    if grp_df.empty:
        st.info("Sem sugestões sem risco de dependências.")
        return

    st.markdown(
        f"**{len(grp_df)} sugestões** em "
        f"**{grp_df[['origem', 'destino']].drop_duplicates().shape[0]}** pares O/D  ·  "
        f"Sem risco: **{(~grp_df['dep_risk']).sum()}**  ·  "
        f"Com dependências: **{grp_df['dep_risk'].sum()}**"
    )

    # ── Summary by O-D pair ───────────────────────────────────────────────────
    st.subheader("Resumo por par O/D")
    od_summary = (
        grp_df.groupby(["origem", "destino"])
        .agg(
            sugestoes=("occ_combinada", "count"),
            occ_min=("occ_combinada", "min"),
            occ_max_comb=("occ_combinada", "max"),
            sem_risco=("dep_risk", lambda x: (~x).sum()),
        )
        .reset_index()
        .sort_values("sugestoes", ascending=False)
        .rename(columns={
            "origem": "Origem", "destino": "Destino",
            "sugestoes": "Sugestões", "occ_min": "Occ. comb. mín (%)",
            "occ_max_comb": "Occ. comb. máx (%)", "sem_risco": "Sem risco",
        })
    )
    st.dataframe(od_summary, hide_index=True, use_container_width=True)

    st.markdown("---")

    # ── Detail table ─────────────────────────────────────────────────────────
    st.subheader("Detalhe das sugestões")

    od_pairs = (
        grp_df[["origem", "destino"]]
        .drop_duplicates()
        .apply(lambda r: f"{r['origem']} → {r['destino']}", axis=1)
        .tolist()
    )
    selected_od = st.selectbox("Par O/D", ["(todos)"] + od_pairs, key="grp_od_sel")

    if selected_od != "(todos)":
        orig_sel, dest_sel = selected_od.split(" → ", 1)
        view = grp_df[(grp_df["origem"] == orig_sel) & (grp_df["destino"] == dest_sel)]
    else:
        view = grp_df

    def _dep_badge(row):
        parts = []
        if row["dep_a"]:
            parts.append(f"⚠️ {row['carreira_a']}")
        if row["dep_b"]:
            parts.append(f"⚠️ {row['carreira_b']}")
        return " · ".join(parts) if parts else "✅ Seguro"

    view = view.copy()
    view["Risco"] = view.apply(_dep_badge, axis=1)

    st.dataframe(
        view[[
            "Risco", "origem", "destino",
            "carreira_a", "designacao_a", "rede_a", "occ_max_a",
            "carreira_b", "designacao_b", "rede_b", "occ_max_b",
            "occ_combinada", "capacidade_livre",
        ]].rename(columns={
            "origem": "Origem", "destino": "Destino",
            "carreira_a": "Carreira A", "designacao_a": "Ligação A",
            "rede_a": "Rede A", "occ_max_a": "Occ. A (%)",
            "carreira_b": "Carreira B", "designacao_b": "Ligação B",
            "rede_b": "Rede B", "occ_max_b": "Occ. B (%)",
            "occ_combinada": "Occ. Combinada (%)", "capacidade_livre": "Cap. Livre (%)",
        }),
        hide_index=True,
        use_container_width=True,
        column_config={
            "Occ. A (%)":         st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
            "Occ. B (%)":         st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
            "Occ. Combinada (%)": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
        },
    )

    # ── Bubble chart: A vs B occupancy, coloured by risk ─────────────────────
    st.markdown("---")
    st.subheader("Mapa de grupagem — Occ. A vs Occ. B")

    fig_bubble = go.Figure()
    for dep_risk, grp_view in view.groupby("dep_risk"):
        color  = "#D32F2F" if dep_risk else "#4CAF50"
        label  = "⚠️ Com dependências" if dep_risk else "✅ Seguro"
        symbol = "x" if dep_risk else "circle"
        hover  = [
            f"<b>{r['Ligação A']}</b> + <b>{r['Ligação B']}</b><br>"
            f"O/D: {r['Origem']} → {r['Destino']}<br>"
            f"Occ. combinada: {r['Occ. Combinada (%)']:.0f}%<br>{r['Risco']}"
            for _, r in grp_view.rename(columns={
                "carreira_a": "_", "designacao_a": "Ligação A",
                "designacao_b": "Ligação B", "origem": "Origem", "destino": "Destino",
                "occ_combinada": "Occ. Combinada (%)",
            }).iterrows()
        ]
        fig_bubble.add_trace(go.Scatter(
            x=grp_view["occ_max_a"],
            y=grp_view["occ_max_b"],
            mode="markers",
            marker=dict(
                size=10, color=color, symbol=symbol,
                line=dict(width=1, color="#fff"),
            ),
            name=label,
            text=grp_view["designacao_a"] + " + " + grp_view["designacao_b"],
            customdata=grp_view["occ_combinada"],
            hovertemplate="<b>%{text}</b><br>Occ. A: %{x:.0f}%  Occ. B: %{y:.0f}%<br>Combinada: %{customdata:.0f}%<extra></extra>",
        ))

    # Diagonal lines: combined = 100% and combined = max_comb%
    diag_x = list(range(0, 101, 5))
    fig_bubble.add_trace(go.Scatter(
        x=diag_x, y=[100 - x for x in diag_x],
        mode="lines", line=dict(color="#9E9E9E", dash="dot", width=1),
        name="Occ. comb. = 100%", showlegend=True,
    ))
    if max_comb < 100:
        fig_bubble.add_trace(go.Scatter(
            x=diag_x, y=[max_comb - x for x in diag_x],
            mode="lines", line=dict(color="#FFC107", dash="dot", width=1),
            name=f"Limite = {max_comb}%", showlegend=True,
        ))

    fig_bubble.update_layout(
        xaxis=dict(title="Ocupação máxima A (%)", range=[0, 105]),
        yaxis=dict(title="Ocupação máxima B (%)", range=[0, 105]),
        height=450,
        margin=dict(l=20, r=20, t=20, b=40),
        legend=dict(orientation="h", y=1.05),
    )
    st.plotly_chart(fig_bubble, use_container_width=True)

    # ── Export ───────────────────────────────────────────────────────────────
    csv_grp = view.drop(columns=["Risco"], errors="ignore").to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "⬇️ Exportar sugestões de grupagem (CSV)",
        data=csv_grp,
        file_name="sugestoes_grupagem.csv",
        mime="text/csv",
        key="dl_grp",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    render_sidebar()

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📋 Rede Semanal",
        "⏱️ Análise de Impacto de Atrasos",
        "🔍 Pesquisa de Carreira",
        "💡 Sugestões de Encadeamento",
        "📦 Ocupação & Grupagem",
    ])

    with tab1:
        tab_rede_semanal()

    with tab2:
        tab_impacto_atrasos()

    with tab3:
        tab_pesquisa_carreira()

    with tab4:
        tab_sugestoes_encadeamento()

    with tab5:
        tab_ocupacao_grupagem()


if __name__ == "__main__":
    main()
