import pandas as pd

from wata.stop_mapping import cluster_stops, resolve_cluster_mapping


def _sample_stops() -> pd.DataFrame:
    # Two stops close together (same cluster), one far away (own cluster).
    return pd.DataFrame(
        [
            {"stop_id": "A", "stop_name": "Stop A", "stop_lat": 37.2700, "stop_lon": -76.7000},
            {"stop_id": "B", "stop_name": "Stop B", "stop_lat": 37.2705, "stop_lon": -76.7005},
            {"stop_id": "C", "stop_name": "Stop C", "stop_lat": 37.3500, "stop_lon": -76.7500},
        ]
    )


def test_cluster_stops_groups_nearby_and_separates_far_stops():
    clustered = cluster_stops(_sample_stops(), max_cluster_radius_m=900)
    a_cluster = clustered.loc[clustered["stop_id"] == "A", "cluster_id"].iloc[0]
    b_cluster = clustered.loc[clustered["stop_id"] == "B", "cluster_id"].iloc[0]
    c_cluster = clustered.loc[clustered["stop_id"] == "C", "cluster_id"].iloc[0]
    assert a_cluster == b_cluster
    assert c_cluster != a_cluster


def test_cluster_count_is_far_fewer_than_stop_count_when_stops_are_close():
    clustered = cluster_stops(_sample_stops(), max_cluster_radius_m=900)
    assert clustered["cluster_id"].nunique() == 2  # {A,B} and {C}


def test_resolve_cluster_mapping_matches_by_exact_raw_stop_id():
    stops = _sample_stops()

    def fake_fetch(lat, lon, max_distance):
        # Every cluster's fetch returns all three candidates; matching
        # must still assign each WATA stop to its own correct global id.
        return {
            "stops": [
                {"raw_stop_id": "A", "global_stop_id": "WATA:1", "stop_lat": 37.2700, "stop_lon": -76.7000},
                {"raw_stop_id": "B", "global_stop_id": "WATA:2", "stop_lat": 37.2705, "stop_lon": -76.7005},
                {"raw_stop_id": "C", "global_stop_id": "WATA:3", "stop_lat": 37.3500, "stop_lon": -76.7500},
            ]
        }

    mapping, unresolved = resolve_cluster_mapping(stops, fake_fetch, max_cluster_radius_m=900)
    assert mapping == {"A": "WATA:1", "B": "WATA:2", "C": "WATA:3"}
    assert unresolved == []


def test_resolve_cluster_mapping_falls_back_to_proximity_when_no_exact_id():
    stops = _sample_stops()

    def fake_fetch(lat, lon, max_distance):
        # No raw_stop_id match at all — must fall back to nearest coordinate.
        return {
            "stops": [
                {"raw_stop_id": "unrelated", "global_stop_id": "WATA:99", "stop_lat": 37.2700, "stop_lon": -76.7000},
            ]
        }

    mapping, unresolved = resolve_cluster_mapping(stops, fake_fetch, max_cluster_radius_m=900)
    assert mapping.get("A") == "WATA:99"  # within tolerance of the candidate
    assert "C" in unresolved  # far cluster's only candidate is 0m from A/B, not C
