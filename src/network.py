"""
network.py - Build a directed dependency graph between transport routes.

A route A is said to "feed" route B when:
  - They share a common stop S
  - A departs S at time T_a
  - B departs (or arrives) S at time T_b
  - 0 < T_b - T_a <= connection_window_min
"""
from __future__ import annotations

import logging
from typing import Optional

import networkx as nx

logger = logging.getLogger(__name__)


def _get_arrival_time(stop: dict) -> Optional[int]:
    """Arrival time at a stop — when cargo becomes available for transfer."""
    return stop.get("hpc")


def _get_departure_time(stop: dict) -> Optional[int]:
    """Departure time from a stop — when the vehicle leaves with loaded cargo."""
    hpp = stop.get("hpp")
    if hpp is not None:
        return hpp
    # Origin stop has no arrival; use departure only
    return stop.get("hpc")


def build_stop_index(routes: list[dict]) -> dict[str, list[tuple[int, dict]]]:
    """Build an index: stop_name → list of (carreira, stop_dict)."""
    index: dict[str, list[tuple[int, dict]]] = {}
    for route in routes:
        carreira = route["carreira"]
        for stop in route["stops"]:
            name = stop["paragem"]
            if name:
                index.setdefault(name, []).append((carreira, stop))
    return index


def build_dependency_graph(
    routes: list[dict],
    connection_window_min: int = 90,
) -> nx.DiGraph:
    """Build a directed graph of route dependencies.

    Each edge A → B carries metadata:
        stop        : shared stop name
        time_a      : departure time of A at that stop (minutes since midnight)
        time_b      : departure/arrival time of B at that stop
        window      : T_b - T_a (minutes)

    Parameters
    ----------
    routes : list of route dicts from parse_network_xlsm
    connection_window_min : maximum allowed connection window in minutes

    Returns
    -------
    nx.DiGraph  – nodes are carreira codes (int)
    """
    graph = nx.DiGraph()

    # Add all routes as nodes with their metadata
    routes_by_id: dict[int, dict] = {}
    for route in routes:
        carreira = route["carreira"]
        routes_by_id[carreira] = route
        graph.add_node(
            carreira,
            designacao=route.get("designacao", ""),
            rede=route.get("rede", ""),
            regiao=route.get("regiao", ""),
            transportador=route.get("transportador", ""),
            veiculo=route.get("veiculo", ""),
            origem=route.get("origem", ""),
            destino=route.get("destino", ""),
            is_reverse_logistics=route.get("is_reverse_logistics", False),
            reverse_leg_from=route.get("reverse_leg_from"),
        )

    # Build stop → [(carreira, stop_dict)] index
    stop_index = build_stop_index(routes)

    # For each stop, find all pairs (A, B) where A departs before B
    # within the connection window
    for stop_name, entries in stop_index.items():
        if len(entries) < 2:
            continue

        # Build two sub-indexes per stop:
        #   arrivals : routes that arrive here  (hpc is the cargo-available time)
        #   departures: routes that depart here (hpp is the load-deadline time)
        # A dependency A→B exists when A arrives and B departs within the window:
        #   0 < B.hpp - A.hpc <= connection_window_min
        arrivals: list[tuple[int, dict, int]] = []    # (carreira, stop, hpc)
        departures: list[tuple[int, dict, int]] = []  # (carreira, stop, hpp)

        for carreira, stop in entries:
            arr = _get_arrival_time(stop)
            dep = _get_departure_time(stop)
            if arr is not None:
                arrivals.append((carreira, stop, arr))
            if dep is not None:
                departures.append((carreira, stop, dep))

        if not arrivals or not departures:
            continue

        arrivals.sort(key=lambda x: x[2])
        departures.sort(key=lambda x: x[2])

        for car_a, stop_a, t_arr_a in arrivals:
            route_a = routes_by_id.get(car_a, {})

            # Skip: route A is a reverse-logistics route — no cargo dependency
            if route_a.get("is_reverse_logistics", False):
                continue

            stops_a = route_a.get("stops", [])

            # Skip: this is the FIRST stop of route A (origin, not an intermediate hub)
            # The first stop has no meaningful arrival cargo to transfer.
            if stops_a:
                first_stop_a = (stops_a[0].get("paragem") or "").strip()
                if first_stop_a and first_stop_a == stop_name:
                    continue

            # Skip: partial reverse leg (stop is after the turnaround point)
            rev_from = route_a.get("reverse_leg_from")
            if rev_from is not None:
                stop_idx_in_a = next(
                    (idx for idx, s in enumerate(stops_a)
                     if (s.get("paragem") or "").strip() == stop_name),
                    None,
                )
                if stop_idx_in_a is not None and stop_idx_in_a >= rev_from:
                    continue

            for car_b, stop_b, t_dep_b in departures:
                if car_a == car_b:
                    continue

                # B must depart AFTER A arrives (cargo transfer window)
                delta = t_dep_b - t_arr_a
                if delta <= 0:
                    continue
                if delta > connection_window_min:
                    break  # departures are sorted, no point continuing

                # Skip: B's first stop is not a connection (B is just starting its trip)
                route_b = routes_by_id.get(car_b, {})
                stops_b = route_b.get("stops", [])
                if stops_b:
                    first_stop_b = (stops_b[0].get("paragem") or "").strip()
                    if first_stop_b and first_stop_b == stop_name:
                        # B originates here — no incoming cargo from A to inherit
                        continue

                name_a = (route_a.get("designacao") or "").upper()
                obs_a  = (route_a.get("obs") or "").lower()
                is_potentially_reverse = (
                    "RIB" in name_a or name_a.endswith("RB") or "RB " in name_a
                    or "empty" in obs_a or "vazi" in obs_a
                )

                # Edge A → B: keep the tightest (smallest window) connection
                if graph.has_edge(car_a, car_b):
                    if graph[car_a][car_b]["window"] > delta:
                        graph[car_a][car_b].update(
                            stop=stop_name,
                            time_a=t_arr_a,
                            time_b=t_dep_b,
                            window=delta,
                            is_potentially_reverse=is_potentially_reverse,
                        )
                else:
                    graph.add_edge(
                        car_a, car_b,
                        stop=stop_name,
                        time_a=t_arr_a,
                        time_b=t_dep_b,
                        window=delta,
                        is_potentially_reverse=is_potentially_reverse,
                    )

    logger.info(
        "Dependency graph: %d nodes, %d edges (window=%d min)",
        graph.number_of_nodes(),
        graph.number_of_edges(),
        connection_window_min,
    )
    return graph


def get_downstream(
    graph: nx.DiGraph,
    carreira_id: int,
    max_depth: int = 5,
) -> list[dict]:
    """Return all routes that directly or indirectly depend on carreira_id.

    Returns a list of dicts:
        carreira, depth, via_stop, window, via_carreira (immediate predecessor)
    """
    if carreira_id not in graph:
        return []

    result: list[dict] = []
    visited: set[int] = {carreira_id}
    queue: list[tuple[int, int, str, int, int]] = []  # (node, depth, stop, window, predecessor)

    for successor in graph.successors(carreira_id):
        edge_data = graph[carreira_id][successor]
        queue.append((successor, 1, edge_data.get("stop", ""), edge_data.get("window", 0), carreira_id))

    while queue:
        node, depth, stop, window, predecessor = queue.pop(0)
        if node in visited or depth > max_depth:
            continue
        visited.add(node)
        node_data = graph.nodes[node]
        result.append(
            {
                "carreira": node,
                "designacao": node_data.get("designacao", ""),
                "rede": node_data.get("rede", ""),
                "regiao": node_data.get("regiao", ""),
                "depth": depth,
                "via_stop": stop,
                "window_min": window,
                "predecessor": predecessor,
            }
        )
        if depth < max_depth:
            for successor in graph.successors(node):
                if successor not in visited:
                    ed = graph[node][successor]
                    queue.append((successor, depth + 1, ed.get("stop", ""), ed.get("window", 0), node))

    return result


def get_upstream(
    graph: nx.DiGraph,
    carreira_id: int,
    max_depth: int = 5,
) -> list[dict]:
    """Return all routes that this carreira_id directly or indirectly depends on.

    Returns the same structure as get_downstream but traversed in reverse.
    """
    if carreira_id not in graph:
        return []

    result: list[dict] = []
    visited: set[int] = {carreira_id}
    queue: list[tuple[int, int, str, int, int]] = []

    for predecessor in graph.predecessors(carreira_id):
        edge_data = graph[predecessor][carreira_id]
        queue.append((predecessor, 1, edge_data.get("stop", ""), edge_data.get("window", 0), carreira_id))

    while queue:
        node, depth, stop, window, successor = queue.pop(0)
        if node in visited or depth > max_depth:
            continue
        visited.add(node)
        node_data = graph.nodes[node]
        result.append(
            {
                "carreira": node,
                "designacao": node_data.get("designacao", ""),
                "rede": node_data.get("rede", ""),
                "regiao": node_data.get("regiao", ""),
                "depth": depth,
                "via_stop": stop,
                "window_min": window,
                "successor": successor,
            }
        )
        if depth < max_depth:
            for pred in graph.predecessors(node):
                if pred not in visited:
                    ed = graph[pred][node]
                    queue.append((pred, depth + 1, ed.get("stop", ""), ed.get("window", 0), node))

    return result
