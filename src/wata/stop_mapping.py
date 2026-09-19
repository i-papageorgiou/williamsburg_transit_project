"""Map WATA GTFS stop_ids to Transit API global_stop_ids.

The Transit API's nearby_stops endpoint returns Transit-internal IDs
(e.g. "STM:17989"), not WATA's own stop_ids, so a join has to be built.

Calling nearby_stops once per stop would burn 100 of the 100 sampled
stops (or 303 of 303) — 100% of a single snapshot's budget just to build
a one-time mapping. Instead this clusters stop coordinates and calls
nearby_stops at cluster centroids with a wide max_distance, since each
call returns many stops at once; a handful of clustered calls should
resolve the whole sample.

The resulting mapping is meant to be cached to disk (see
`save_mapping`/`load_mapping`) and never rebuilt once resolved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Protocol

import pandas as pd
from shapely.geometry import Point

from wata.gtfs import REPO_ROOT

MAPPING_CACHE_PATH = REPO_ROOT / "data" / "processed" / "stop_id_mapping.json"

# Matches the CRS used in wata.metrics.access for meter-accurate clustering.
PROJECTED_CRS = "EPSG:32618"

# nearby_stops caps max_distance at 1500m (its own documented maximum), so
# clusters must be small enough that a single call at the cluster's
# centroid, with some margin, still covers every member stop.
API_MAX_DISTANCE_M = 1500


def cluster_stops(
    stops: pd.DataFrame, *, max_cluster_radius_m: float = 900
) -> pd.DataFrame:
    """Greedily group stops into clusters no wider than max_cluster_radius_m,
    so each cluster can be covered by one nearby_stops call at its centroid.

    `stops` must have stop_id, stop_lat, stop_lon columns. Returns one row
    per stop with an added `cluster_id`, plus the cluster's centroid lat/lon
    (repeated per member) for convenience.
    """
    import geopandas as gpd

    gdf = gpd.GeoDataFrame(
        stops.copy(),
        geometry=[Point(xy) for xy in zip(stops["stop_lon"], stops["stop_lat"])],
        crs="EPSG:4326",
    ).to_crs(PROJECTED_CRS)

    unclustered = list(gdf.index)
    cluster_of: dict = {}
    cluster_id = 0

    coords = {idx: gdf.geometry[idx] for idx in gdf.index}

    while unclustered:
        seed = unclustered.pop(0)
        seed_point = coords[seed]
        members = [seed]
        remaining = []
        for idx in unclustered:
            if coords[idx].distance(seed_point) <= max_cluster_radius_m:
                members.append(idx)
            else:
                remaining.append(idx)
        unclustered = remaining
        for m in members:
            cluster_of[m] = cluster_id
        cluster_id += 1

    result = stops.copy()
    result["cluster_id"] = result.index.map(cluster_of)

    centroids = (
        gdf.assign(cluster_id=gdf.index.map(cluster_of))
        .dissolve(by="cluster_id")
        .geometry.centroid.to_crs("EPSG:4326")
    )
    centroid_lookup = {cid: (pt.y, pt.x) for cid, pt in centroids.items()}
    result["cluster_centroid_lat"] = result["cluster_id"].map(lambda c: centroid_lookup[c][0])
    result["cluster_centroid_lon"] = result["cluster_id"].map(lambda c: centroid_lookup[c][1])
    return result


class NearbyStopsFetcher(Protocol):
    def __call__(self, lat: float, lon: float, max_distance: int) -> dict: ...


def resolve_cluster_mapping(
    stops: pd.DataFrame,
    fetch_nearby_stops: NearbyStopsFetcher,
    *,
    max_cluster_radius_m: float = 900,
    match_tolerance_m: float = 50,
) -> tuple[dict[str, str], list[str]]:
    """Build {wata_stop_id: global_stop_id} by calling fetch_nearby_stops
    once per cluster centroid and matching returned stops back to WATA
    stops by name + proximity.

    `fetch_nearby_stops` is injected (not a live TransitApiClient call
    directly) so this can be exercised in tests with a fake before any
    API key exists.

    Returns (mapping, unresolved_stop_ids). A stop is unresolved if no
    candidate from its cluster's response matched within
    match_tolerance_m — callers should retry those individually.
    """
    clustered = cluster_stops(stops, max_cluster_radius_m=max_cluster_radius_m)
    mapping: dict[str, str] = {}

    for cluster_id, group in clustered.groupby("cluster_id"):
        lat = group["cluster_centroid_lat"].iloc[0]
        lon = group["cluster_centroid_lon"].iloc[0]
        # Wide enough to cover the cluster's own radius plus margin, capped
        # at the API's own documented maximum of 1500m.
        max_distance = min(int(max_cluster_radius_m * 1.5), API_MAX_DISTANCE_M)
        response = fetch_nearby_stops(lat, lon, max_distance=max_distance)
        candidates = response.get("stops", [])

        for _, wata_stop in group.iterrows():
            best = _closest_match(wata_stop, candidates, match_tolerance_m)
            if best is not None:
                mapping[wata_stop["stop_id"]] = best

    all_ids = set(stops["stop_id"])
    unresolved = sorted(all_ids - set(mapping.keys()))
    return mapping, unresolved


def _closest_match(wata_stop, candidates: list[dict], tolerance_m: float) -> str | None:
    """Match a WATA stop to a Transit API `Stop` object.

    `raw_stop_id` is documented as "the GTFS stop_id of the stop [...] when
    the GTFS stop id is *not* stored in the feed" it falls back to the
    real-time stop id instead — so an exact match is preferred but isn't
    guaranteed, and every candidate falls back to nearest-coordinate
    matching within `tolerance_m`.
    """
    for c in candidates:
        if c.get("raw_stop_id") == wata_stop["stop_id"]:
            return c["global_stop_id"]

    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    best_id = None
    best_dist = float("inf")
    for c in candidates:
        try:
            lat, lon = c["stop_lat"], c["stop_lon"]
            global_id = c["global_stop_id"]
        except KeyError:
            continue
        _, _, dist = geod.inv(wata_stop["stop_lon"], wata_stop["stop_lat"], lon, lat)
        if dist < best_dist:
            best_dist = dist
            best_id = global_id
    if best_dist <= tolerance_m:
        return best_id
    return None


def save_mapping(mapping: dict[str, str], path: Path | None = None) -> None:
    path = path or MAPPING_CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=2, sort_keys=True))


def load_mapping(path: Path | None = None) -> dict[str, str]:
    path = path or MAPPING_CACHE_PATH
    if not path.exists():
        return {}
    return json.loads(path.read_text())
