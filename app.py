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

def tab_rede_semanal():
    st.header("📋 Rede Semanal")

    uploaded = st.file_uploader(
        "Carregar ficheiro de rede (.xlsm / .xlsx)",
        type=["xlsm", "xlsx"],
        key="network_uploader",
    )

    if uploaded is not None:
        with st.spinner("A processar ficheiro de rede…"):
            try:
                import importlib, src.parser as _parser_mod
                importlib.reload(_parser_mod)
                from src.parser import parse_network_xlsm as _parse_fresh

                # Read into fresh BytesIO to avoid any stream-position issues
                raw_bytes = uploaded.read()
                import io as _io
                file_obj = _io.BytesIO(raw_bytes)
                file_obj.name = getattr(uploaded, "name", "network.xlsm")

                data = _parse_fresh(file_obj)
                routes = data["routes"]
                _set_state("routes", routes)
                _set_state("stops_index", data["stops_index"])
                _set_state("summary", data.get("summary", {}))
                _set_state("coordinates", data.get("coordinates", {}))
                # Build graph with current sidebar settings
                window = _get_state("connection_window", 90)
                sul_only = _get_state("sul_only", False)
                _rebuild_graph(routes, window, sul_only)
                if routes:
                    st.success(f"✅ {len(routes)} carreiras carregadas com sucesso.")
                else:
                    st.error(
                        "⚠️ 0 carreiras encontradas. O ficheiro pode estar num formato "
                        "inesperado. Ver detalhes abaixo."
                    )
                    with st.expander("Diagnóstico"):
                        import openpyxl as _opx
                        _wb = _opx.load_workbook(_io.BytesIO(raw_bytes), read_only=True, data_only=True)
                        st.write("**Sheets encontradas:**", _wb.sheetnames)
                        if "Horários" in _wb.sheetnames:
                            _ws = _wb["Horários"]
                            sample = []
                            for _r in _ws.iter_rows(values_only=True):
                                sample.append(_r[:5])
                                if len(sample) >= 10:
                                    break
                            st.write("**Primeiras 10 linhas da sheet Horários:**")
                            st.write(sample)
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

    # Centre the map on Portugal
    fig.update_layout(
        mapbox=dict(
            style="open-street-map",
            center=dict(lat=39.5, lon=-8.0),
            zoom=5.5,
        ),
        height=680,
        margin=dict(l=0, r=0, t=30, b=0),
        legend=dict(
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="#ccc",
            borderwidth=1,
            x=0.01, y=0.99,
            xanchor="left", yanchor="top",
        ),
        title="Rede de Transportes — Paragens georreferenciadas",
    )
    return fig


# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Análise de Impacto de Atrasos
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
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    render_sidebar()

    tab1, tab2, tab3 = st.tabs([
        "📋 Rede Semanal",
        "⏱️ Análise de Impacto de Atrasos",
        "🔍 Pesquisa de Carreira",
    ])

    with tab1:
        tab_rede_semanal()

    with tab2:
        tab_impacto_atrasos()

    with tab3:
        tab_pesquisa_carreira()


if __name__ == "__main__":
    main()
