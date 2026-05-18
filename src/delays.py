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

                # Inherited delay: original delay minus the connection slack at this stop.
                # If the delay eats into the connection window, the downstream route
                # may still be affected (but with reduced inherited delay).
                inherited = max(0.0, delay - window)

                # Even if inherited == 0 we still record 1 min to indicate risk.
                # Only truly skip if the delay is far below the window.
                if delay <= 0:
                    continue

                # Record the impact (keep maximum inherited delay across paths)
                key = (successor, src_carreira)
                effective_delay = inherited if inherited > 0 else min(delay, window / 2.0)

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

    return agg.to_dict("records")
