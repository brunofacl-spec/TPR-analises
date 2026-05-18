"""
delays.py - Cascade delay calculation for transport network analysis.

For each initially-delayed route, propagates the delay through the dependency
graph to all downstream routes using a BFS approach.  Each "hop" reduces the
inherited delay by the connection window at that stop; if the inherited delay
falls to zero or below the downstream route is unaffected.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

import networkx as nx
import pandas as pd

logger = logging.getLogger(__name__)


def calculate_cascade(
    delayed_routes: list[dict],
    graph: nx.DiGraph,
    routes_dict: dict[int, dict],
    max_depth: int = 8,
    include_reverse_logistics: bool = False,
) -> pd.DataFrame:
    """Propagate delays from initially-delayed routes through the dependency graph.

    Parameters
    ----------
    delayed_routes : list of dicts
        Each dict must have:
            carreira      (int)   – the originally delayed route
            delay_minutes (float) – original delay in minutes
            stop          (str)   – stop where the delay occurred (optional)
            causa         (str)   – cause description (optional)
    graph : nx.DiGraph
        Built by build_dependency_graph().
    routes_dict : dict {carreira → route_dict}
        Used to look up designacao / rede / regiao for display.
    max_depth : int
        Maximum propagation depth.
    include_reverse_logistics : bool
        When False (default), delays originating from reverse-logistics routes
        (is_reverse_logistics=True in the graph node or routes_dict) are NOT
        propagated to downstream routes.  Individual trips flagged as reverse
        logistics via Obs notes are also excluded when False.

    Returns
    -------
    pd.DataFrame with columns:
        carreira           – affected route code
        designacao         – route name
        rede               – R1/R2/R3
        regiao             – geographic region
        stop_enlace        – shared stop where the connection happens
        atraso_herdado     – inherited delay (minutes)
        origem_atraso      – carreira code of the original delayed route
        profundidade       – hop count from the original delay
        causa_origem       – cause of the original delay
    """
    if not delayed_routes:
        return pd.DataFrame(columns=[
            "carreira", "designacao", "rede", "regiao",
            "stop_enlace", "atraso_herdado", "origem_atraso",
            "profundidade", "causa_origem",
        ])

    # Accumulate impacts per (downstream_carreira, origem) to merge duplicates
    # key: (affected_carreira, origem_carreira)  value: best (max) atraso_herdado record
    impacts: dict[tuple[int, int], dict] = {}

    for src in delayed_routes:
        src_carreira = int(src["carreira"])
        src_delay = float(src.get("delay_minutes", 0))
        src_causa = str(src.get("causa", ""))
        src_stop = str(src.get("stop", ""))

        if src_carreira not in graph:
            continue
        if src_delay <= 0:
            continue

        # Reverse-logistics filter: skip propagation when disabled
        if not include_reverse_logistics:
            # Check graph node attribute first, then routes_dict, then trip-level obs flag
            node_data = graph.nodes.get(src_carreira, {})
            rdict = routes_dict.get(src_carreira, {})
            is_rev = (
                node_data.get("is_reverse_logistics", False)
                or rdict.get("is_reverse_logistics", False)
                or src.get("is_reverse_logistics", False)
            )
            if is_rev:
                continue

        # BFS – queue entries: (node, delay_at_node, depth, stop_enlace)
        queue: list[tuple[int, float, int, str]] = [(src_carreira, src_delay, 0, src_stop)]
        visited_in_this_src: set[int] = {src_carreira}

        while queue:
            node, delay, depth, enlace = queue.pop(0)
            if depth >= max_depth:
                continue

            for successor in graph.successors(node):
                edge = graph[node][successor]
                window = edge.get("window", 90)
                shared_stop = edge.get("stop", "")

                # Inherited delay: how much of the original delay exceeds the
                # connection window at this stop.
                # If delay <= window the downstream route has enough slack — no impact.
                inherited = delay - window

                if inherited <= 0:
                    continue

                key = (successor, src_carreira)
                effective_delay = inherited

                if key not in impacts or impacts[key]["atraso_herdado"] < effective_delay:
                    node_data = graph.nodes.get(successor, {})
                    rdict = routes_dict.get(successor, {})
                    impacts[key] = {
                        "carreira": successor,
                        "designacao": node_data.get("designacao", rdict.get("designacao", "")),
                        "rede": node_data.get("rede", rdict.get("rede", "")),
                        "regiao": node_data.get("regiao", rdict.get("regiao", "")),
                        "stop_enlace": shared_stop,
                        "atraso_herdado": round(effective_delay, 1),
                        "origem_atraso": src_carreira,
                        "profundidade": depth + 1,
                        "causa_origem": src_causa,
                    }

                if successor not in visited_in_this_src and inherited > 0:
                    visited_in_this_src.add(successor)
                    queue.append((successor, inherited, depth + 1, shared_stop))

    if not impacts:
        return pd.DataFrame(columns=[
            "carreira", "designacao", "rede", "regiao",
            "stop_enlace", "atraso_herdado", "origem_atraso",
            "profundidade", "causa_origem",
        ])

    df = pd.DataFrame(list(impacts.values()))
    df = df.sort_values(["atraso_herdado", "profundidade"], ascending=[False, True])
    df = df.reset_index(drop=True)
    return df


def get_initial_delays(exec_df: pd.DataFrame, anomaly_filter: Optional[str] = None) -> list[dict]:
    """Extract initially-delayed routes from the execution DataFrame.

    Parameters
    ----------
    exec_df : normalised execution DataFrame (from parse_execution_file)
    anomaly_filter : if given, only rows where anomalia contains this string;
                     defaults to 'ATRASO PARTIDA'.

    Returns
    -------
    list of dicts suitable for calculate_cascade.
    """
    if exec_df is None or exec_df.empty:
        return []

    filter_str = anomaly_filter or "ATRASO PARTIDA"

    mask = (
        exec_df["anomalia"].astype(str).str.contains(filter_str, case=False, na=False)
        & exec_df["atraso_min"].notna()
        & (exec_df["atraso_min"] > 0)
    )
    delayed_df = exec_df[mask].copy()

    if delayed_df.empty:
        return []

    # Keep worst delay per carreira (multiple stops may be recorded)
    delayed_df["carreira_int"] = pd.to_numeric(delayed_df["carreira"], errors="coerce")
    delayed_df = delayed_df.dropna(subset=["carreira_int"])
    delayed_df["carreira_int"] = delayed_df["carreira_int"].astype(int)

    # Aggregate: for each carreira take max delay
    agg = (
        delayed_df.groupby("carreira_int")
        .agg(
            delay_minutes=("atraso_min", "max"),
            stop=("designacao_paragem", "first"),
            causa=("causa", "first"),
        )
        .reset_index()
        .rename(columns={"carreira_int": "carreira"})
    )

    records = agg.to_dict("records")

    # Flag individual trips as reverse logistics based on Obs column
    # Group obs per carreira to check trip-level notes
    obs_by_carreira: dict[int, list[str]] = {}
    for _, row in delayed_df.iterrows():
        car = int(row["carreira_int"])
        obs_val = str(row.get("obs") or "").lower()
        obs_by_carreira.setdefault(car, []).append(obs_val)

    _rev_keywords = ["vazi", "retorno", "inversa", "empty", "paletes v", "contentores v"]
    for rec in records:
        car = rec["carreira"]
        obs_list = obs_by_carreira.get(car, [])
        is_rev = any(
            any(kw in obs for kw in _rev_keywords)
            for obs in obs_list
        )
        rec["is_reverse_logistics"] = is_rev

    return records
