"""Coverage: walksheds around served stops, weighted by service intensity.

This uses only GTFS — no Transit API, no Census key. It answers "how much
of the service area is within walking distance of a stop, and how good is
the service at that stop" without yet joining to population data (that join
is a follow-up once a Census API key is requested; the key is free and
instant but is a separate credential from the Transit/Swiftly keys blocked
in Phase 0, so it's deliberately out of scope here).
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from wata.gtfs import GtfsFeed

# WATA's service area is centered on Williamsburg, VA (UTM zone 18N).
# Projecting to this CRS before buffering keeps the 800m walk radius
# accurate in meters instead of degrees.
PROJECTED_CRS = "EPSG:32618"
WALK_BUFFER_METERS = 800  # ~10 minute walk


def stop_departure_counts(feed: GtfsFeed, service_id: str = "Full-Weekday") -> pd.DataFrame:
    """Departures per stop for a given service_id — the service-intensity weight."""
    trip_ids = feed.trips.loc[feed.trips["service_id"] == service_id, "trip_id"]
    st = feed.stop_times[feed.stop_times["trip_id"].isin(trip_ids)]
    counts = st.groupby("stop_id").size().rename("departures").reset_index()
    return counts


def served_stops_gdf(feed: GtfsFeed, service_id: str = "Full-Weekday") -> gpd.GeoDataFrame:
    """Served stops as points, annotated with weekday departure counts."""
    served_ids = feed.served_stop_ids()
    stops = feed.stops[feed.stops["stop_id"].isin(served_ids)].copy()
    counts = stop_departure_counts(feed, service_id=service_id)
    stops = stops.merge(counts, on="stop_id", how="left")
    stops["departures"] = stops["departures"].fillna(0).astype(int)

    geometry = [Point(xy) for xy in zip(stops["stop_lon"], stops["stop_lat"])]
    return gpd.GeoDataFrame(stops, geometry=geometry, crs="EPSG:4326")


def frequency_tier(departures: int) -> str:
    """Bucket a stop's weekday departure count into a rider-legible tier.

    Thresholds: >=60 departures/day is roughly every-30-min-or-better
    across a ~15hr span; >=20 is a handful of trips per hour at peak;
    below that is a few trips a day.
    """
    if departures >= 60:
        return "frequent"
    if departures >= 20:
        return "moderate"
    if departures > 0:
        return "infrequent"
    return "unserved"


def walksheds(feed: GtfsFeed, service_id: str = "Full-Weekday") -> gpd.GeoDataFrame:
    """800m walk buffer polygons around each served stop, tagged by frequency tier.

    Buffers are per-stop (not merged/dissolved) so a caller can still see
    which stop and tier covers a given area; dissolve by tier for a
    coverage map.
    """
    stops = served_stops_gdf(feed, service_id=service_id)
    projected = stops.to_crs(PROJECTED_CRS)
    projected["geometry"] = projected.geometry.buffer(WALK_BUFFER_METERS)
    projected["frequency_tier"] = projected["departures"].map(frequency_tier)
    return projected.to_crs("EPSG:4326")


def coverage_summary(feed: GtfsFeed, service_id: str = "Full-Weekday") -> dict:
    """Total walkshed area (km^2) by frequency tier, dissolved to avoid
    double-counting overlapping buffers from nearby stops.
    """
    sheds = walksheds(feed, service_id=service_id).to_crs(PROJECTED_CRS)
    dissolved = sheds.dissolve(by="frequency_tier")
    dissolved["area_km2"] = dissolved.geometry.area / 1e6
    return dissolved["area_km2"].round(2).to_dict()
