import logging
import hashlib
from datetime import datetime, timezone

from typing import Dict, Optional, List
from config import Config
from utils.cache import cache_enrichment_data, get_cached_enrichment_data
from utils.google_spend import (
    API_DISTANCE_MATRIX,
    API_PLACES_NEARBY,
    billed_get,
)

logger = logging.getLogger(__name__)


class TravelTimeService:
    def __init__(self):
        self.google_maps_key = Config.GOOGLE_MAPS_API_KEY
        self.google_places_key = Config.GOOGLE_PLACES_API_KEY

        # Key destinations: 2 reference cities (configurable via Scoring Criteria -> Reference Cities).
        # Internal keys remain for legacy column compatibility:
        # - travel_time_oviedo = reference city A
        # - travel_time_gijon  = reference city B
        self.destinations = {
            "oviedo": "40.4168,-3.7038",  # Madrid (fallback)
            "gijon": "41.3851,2.1734",  # Barcelona (fallback)
        }
        self.destination_labels = {
            "oviedo": "City A",
            "gijon": "City B",
        }
        try:
            from services.settings_service import SettingsService

            cities = SettingsService.get_reference_cities()
            if cities and len(cities) >= 2:
                self.destinations = {
                    "oviedo": f"{cities[0]['lat']},{cities[0]['lon']}",
                    "gijon": f"{cities[1]['lat']},{cities[1]['lon']}",
                }
                self.destination_labels = {
                    "oviedo": cities[0].get("name") or "City A",
                    "gijon": cities[1].get("name") or "City B",
                }
        except Exception:
            # Safe fallback to defaults
            pass

        # Dynamic "nearest" targets (resolved via Places when available).
        self._nearest_place_defs = {
            "beach": {"type": "tourist_attraction", "keyword": "playa"},
            "airport": {"type": "airport"},
            "train_station": {"type": "train_station"},
            "hospital": {"type": "hospital"},
            "police": {"type": "police"},
        }

    def calculate_travel_times(self, land_id: int) -> bool:
        """Calculate travel times for a land property"""
        try:
            from models import Land
            from app import db

            land = db.session.get(Land, land_id)
            if not land or not land.location_lat or not land.location_lon:
                logger.warning("Land %s has no coordinates", land_id)
                return False

            logger.info(f"Calculating travel times for land {land_id}")

            lat = float(land.location_lat)
            lon = float(land.location_lon)
            origin = f"{lat},{lon}"

            cache_type = self._travel_times_cache_type()
            cached = get_cached_enrichment_data(lat, lon, cache_type)
            if isinstance(cached, dict):
                for key, value in cached.items():
                    if value is None:
                        continue
                    if hasattr(land, key):
                        setattr(land, key, value)

                db.session.commit()
                logger.info("Travel times cache hit for land %s", land_id)
                return True

            # Resolve nearest places (Places API) then compute travel (Distance Matrix or fallback).
            nearest_places: Dict[str, Optional[Dict]] = {}
            for key, spec in self._nearest_place_defs.items():
                nearest_places[key] = self._nearest_place(
                    lat, lon, place_type=spec.get("type"), keyword=spec.get("keyword")
                )

            # If beach not found, try a second strategy.
            if not nearest_places.get("beach"):
                nearest_places["beach"] = self._nearest_place(
                    lat, lon, place_type="natural_feature", keyword="beach"
                )

            # Build destination list for a batch call.
            dest_map: Dict[str, str] = {
                "oviedo": self.destinations["oviedo"],
                "gijon": self.destinations["gijon"],
            }
            for k, place in nearest_places.items():
                if not place:
                    continue
                try:
                    dest_map[k] = f"{float(place['lat'])},{float(place['lon'])}"
                except Exception:
                    continue

            dest_keys = list(dest_map.keys())
            dest_values = [dest_map[k] for k in dest_keys]

            results = (
                self._get_google_travel_times(origin, dest_values)
                if self.google_maps_key
                else [None for _ in dest_values]
            )

            def _res_for(key: str) -> Optional[Dict]:
                """A Distance Matrix answer, or an estimate that says so.

                The haversine fallback used to be written into the very column
                Google fills, so a straight line at an assumed speed was
                indistinguishable from a drive time through Asturian mountains
                (#225). It is still computed -- it is worth showing -- but it
                carries its provenance from here on.
                """
                try:
                    idx = dest_keys.index(key)
                except ValueError:
                    return None
                res = results[idx] if idx < len(results) else None
                if res:
                    return {**res, "source": "google"}
                fallback = self._calculate_fallback_travel_time(
                    origin, dest_map.get(key, "")
                )
                if not fallback:
                    return None
                return {**fallback, "source": "estimate"}

            resolved: Dict[str, Optional[Dict]] = {
                key: _res_for(key) for key in dest_keys
            }

            def _measured(key: str) -> Optional[Dict]:
                """Only a real measurement may reach a travel-time column."""
                res = resolved.get(key)
                return res if res and res.get("source") == "google" else None

            oviedo_time = (_measured("oviedo") or {}).get("time")
            gijon_time = (_measured("gijon") or {}).get("time")

            nearest_beach_data = None
            beach_res = _measured("beach")
            if beach_res and nearest_places.get("beach"):
                nearest_beach_data = {
                    "name": nearest_places["beach"].get("name"),
                    "time": beach_res.get("time"),
                    "distance": beach_res.get("distance"),
                }

            def _facility_data(key: str) -> Optional[Dict]:
                place = nearest_places.get(key)
                res = _measured(key)
                if not place or not res:
                    return None
                return {
                    "name": place.get("name"),
                    "time": res.get("time"),
                    "distance": res.get("distance"),
                }

            airport_data = _facility_data("airport")
            train_station_data = _facility_data("train_station")
            hospital_data = _facility_data("hospital")
            police_data = _facility_data("police")

            # Update land record
            if oviedo_time is not None:
                land.travel_time_oviedo = oviedo_time
            if gijon_time is not None:
                land.travel_time_gijon = gijon_time
            if nearest_beach_data:
                land.travel_time_nearest_beach = nearest_beach_data["time"]
                land.nearest_beach_name = nearest_beach_data["name"]

            # Update priority infrastructure travel times and distances
            if airport_data is not None:
                land.travel_time_airport = airport_data["time"]
                land.distance_airport = airport_data["distance"]
            if train_station_data is not None:
                land.travel_time_train_station = train_station_data["time"]
                land.distance_train_station = train_station_data["distance"]
            if hospital_data is not None:
                land.travel_time_hospital = hospital_data["time"]
                land.distance_hospital = hospital_data["distance"]
            if police_data is not None:
                land.travel_time_police = police_data["time"]
                land.distance_police = police_data["distance"]

            # What this run actually learned, per target, so the page can tell a
            # measurement from an estimate and from nothing at all. The same
            # shape as `Property.travel["api_status"]`, one surface later.
            targets: Dict[str, Dict] = {}
            for key in dest_keys:
                res = resolved.get(key)
                if not res:
                    targets[key] = {"source": "unavailable"}
                    continue
                targets[key] = {
                    "source": res.get("source"),
                    "time_min": res.get("time"),
                    "distance_km": res.get("distance"),
                }
                place = nearest_places.get(key)
                if place and place.get("name"):
                    targets[key]["name"] = place.get("name")

            sources = {entry["source"] for entry in targets.values()}
            if sources == {"google"}:
                api_status = "ok"
            elif "google" in sources:
                api_status = "degraded"
            else:
                api_status = "unavailable"

            # A new dict rather than an in-place mutation: SQLAlchemy does not
            # see a JSON column changed under it.
            land.travel = {
                "api_status": api_status,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "targets": targets,
            }

            measured_anything = "google" in sources

            if not measured_anything:
                # Caching a run that measured nothing would keep the next seven
                # days from ever asking Google again.
                db.session.commit()
                logger.info(
                    "Travel times for land %s: no measurement (api_status=%s)",
                    land_id,
                    api_status,
                )
                return True

            cache_enrichment_data(
                lat,
                lon,
                cache_type,
                {
                    "travel_time_oviedo": land.travel_time_oviedo,
                    "travel_time_gijon": land.travel_time_gijon,
                    "travel_time_nearest_beach": land.travel_time_nearest_beach,
                    "nearest_beach_name": land.nearest_beach_name,
                    "travel_time_airport": land.travel_time_airport,
                    "distance_airport": land.distance_airport,
                    "travel_time_train_station": land.travel_time_train_station,
                    "distance_train_station": land.distance_train_station,
                    "travel_time_hospital": land.travel_time_hospital,
                    "distance_hospital": land.distance_hospital,
                    "travel_time_police": land.travel_time_police,
                    "distance_police": land.distance_police,
                },
                timeout=60 * 60 * 24 * 7,  # 7 days
            )

            db.session.commit()

            logger.info(
                "Travel times updated for land %s: %s=%smin, %s=%smin, Beach=%smin",
                land_id,
                self.destination_labels.get("oviedo", "City A"),
                oviedo_time,
                self.destination_labels.get("gijon", "City B"),
                gijon_time,
                nearest_beach_data["time"] if nearest_beach_data else None,
            )

            return True

        except Exception:
            logger.error(
                "Failed to calculate travel times for land %s", land_id, exc_info=True
            )
            return False

    def _travel_times_cache_type(self) -> str:
        ref_sig = f"{self.destinations.get('oviedo')}|{self.destinations.get('gijon')}"
        ref_hash = hashlib.md5(ref_sig.encode()).hexdigest()[:8]
        return f"travel_times_v2:{ref_hash}"

    def _nearest_place(
        self,
        lat: float,
        lon: float,
        place_type: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> Optional[Dict]:
        """Find nearest place using Google Places Nearby Search (best-effort)."""
        if not self.google_places_key:
            return None

        try:
            params: Dict[str, str] = {
                "key": self.google_places_key,
                "location": f"{lat},{lon}",
                "rankby": "distance",
            }
            if place_type:
                params["type"] = str(place_type)
            if keyword:
                params["keyword"] = str(keyword)

            resp = billed_get(
                API_PLACES_NEARBY,
                params=params,
                units=1,
                subject=f"{lat},{lon}:{place_type or keyword or ''}",
                timeout=15,
                call_logger=logger,
            )
            if resp.status_code != 200:
                return None
            data = resp.json() or {}
            if data.get("status") != "OK":
                return None
            results = data.get("results") or []
            if not results:
                return None

            top = results[0] or {}
            geo = (top.get("geometry") or {}).get("location") or {}
            plat = geo.get("lat")
            plon = geo.get("lng")
            if plat is None or plon is None:
                return None

            return {
                "name": top.get("name"),
                "place_id": top.get("place_id"),
                "lat": plat,
                "lon": plon,
                "types": top.get("types") or [],
            }
        except Exception as e:
            logger.warning(
                "Nearest place lookup failed (type=%s keyword=%s): %s",
                place_type,
                keyword,
                e,
            )
            return None

    def _beach_label(self, beach_full_name: str) -> str:
        return (
            beach_full_name.split(",")[0]
            .replace("Playa de ", "")
            .replace("Playa del ", "")
        )

    def _min_by_time(
        self,
        results: List[Optional[Dict]],
        names: Optional[List[str]] = None,
        name_transform=None,
    ) -> Optional[Dict]:
        best = None
        for idx, r in enumerate(results):
            if not r or r.get("time") is None:
                continue
            if best is None or r["time"] < best["time"]:
                best = dict(r)
                if names and idx < len(names):
                    label = names[idx]
                    best["full_name"] = label
                    best["name"] = (
                        name_transform(label) if callable(name_transform) else label
                    )
        return best

    def _get_travel_time(self, origin: str, destination: str) -> Optional[int]:
        """Get travel time in minutes between origin and destination"""
        result = self._get_travel_time_and_distance(origin, destination)
        return result["time"] if result else None

    def _get_travel_time_and_distance(
        self, origin: str, destination: str
    ) -> Optional[Dict]:
        """Get travel time in minutes and distance in km between origin and destination"""
        # Try Google API first if available
        if self.google_maps_key:
            result = self._get_google_travel_time(origin, destination)
            if result:
                return result

        # Fallback to mathematical estimation
        logger.info("Using fallback travel time calculation")
        return self._calculate_fallback_travel_time(origin, destination)

    def _get_google_travel_time(self, origin: str, destination: str) -> Optional[Dict]:
        """Get travel time using Google Maps API"""
        try:
            response = billed_get(
                API_DISTANCE_MATRIX,
                params={
                    "origins": origin,
                    "destinations": destination,
                    "mode": "driving",
                    "units": "metric",
                    "key": self.google_maps_key,
                },
                units=1,
                subject=origin,
                timeout=15,
                call_logger=logger,
            )
            if response.status_code == 200:
                data = response.json()

                if data.get("status") == "OK" and data.get("rows"):
                    elements = data["rows"][0].get("elements", [])
                    if elements and elements[0].get("status") == "OK":
                        el = elements[0]
                        dur = el.get("duration")
                        dist = el.get("distance")
                        if not dur or not dist:
                            return None

                        return {
                            "time": round(dur["value"] / 60),  # convert to minutes
                            "distance": round(
                                dist["value"] / 1000
                            ),  # convert to kilometers
                        }

            logger.warning(
                f"Google API failed for {origin} to {destination}: {data.get('status') if 'data' in locals() else 'No response'}"
            )
            return None

        except Exception:
            logger.error("Google Maps API error", exc_info=True)
            return None

    def _get_google_travel_times(
        self, origin: str, destinations: List[str]
    ) -> List[Optional[Dict]]:
        """Batch travel times using a single Distance Matrix call (destinations <= 25)."""
        if not self.google_maps_key or not destinations:
            return [None for _ in destinations]

        try:
            response = billed_get(
                API_DISTANCE_MATRIX,
                params={
                    "origins": origin,
                    "destinations": "|".join(destinations),
                    "mode": "driving",
                    "units": "metric",
                    "key": self.google_maps_key,
                },
                units=len(destinations),
                subject=origin,
                timeout=15,
                call_logger=logger,
            )
            if response.status_code != 200:
                return [None for _ in destinations]

            data = response.json()
            if data.get("status") != "OK" or not data.get("rows"):
                return [None for _ in destinations]

            elements = data["rows"][0].get("elements", [])
            out: List[Optional[Dict]] = []
            for el in elements[: len(destinations)]:
                if el.get("status") != "OK":
                    out.append(None)
                    continue
                dur = el.get("duration")
                dist = el.get("distance")
                if not dur or not dist:
                    out.append(None)
                    continue
                out.append(
                    {
                        "time": round(dur["value"] / 60),
                        "distance": round(dist["value"] / 1000),
                    }
                )

            # Pad if API returned fewer elements
            while len(out) < len(destinations):
                out.append(None)

            return out
        except Exception as e:
            logger.error("Distance Matrix batch error: %s", e)
            return [None for _ in destinations]

    def _calculate_fallback_travel_time(
        self, origin: str, destination: str
    ) -> Optional[Dict]:
        """Calculate travel time using mathematical distance estimation"""
        try:
            # Parse origin coordinates
            if "," in origin:
                origin_lat, origin_lon = map(float, origin.split(","))
            else:
                logger.error(f"Invalid origin format: {origin}")
                return None

            # Get destination coordinates
            dest_coords = self._get_destination_coordinates(destination)
            if not dest_coords:
                logger.warning(
                    f"Could not get coordinates for destination: {destination}"
                )
                return None

            dest_lat, dest_lon = dest_coords

            # Calculate straight-line distance using Haversine formula
            distance_km = self._haversine_distance(
                origin_lat, origin_lon, dest_lat, dest_lon
            )

            # Estimate travel time based on distance and road type
            # Use realistic speed estimates:
            # - Short distances (<20km): 45 km/h average (local roads, traffic)
            # - Medium distances (20-50km): 55 km/h average (mixed roads)
            # - Long distances (>50km): 65 km/h average (highways)

            if distance_km < 20:
                avg_speed = 45
            elif distance_km < 50:
                avg_speed = 55
            else:
                avg_speed = 65

            # Add 20% to account for actual road routes vs straight line
            actual_distance = distance_km * 1.2
            travel_time = round(
                (actual_distance / avg_speed) * 60
            )  # Convert to minutes

            return {"time": travel_time, "distance": round(actual_distance)}

        except Exception:
            logger.error("Fallback travel time calculation failed", exc_info=True)
            return None

    def _haversine_distance(
        self, lat1: float, lon1: float, lat2: float, lon2: float
    ) -> float:
        """Calculate the great circle distance between two points on earth (in kilometers)"""
        import math

        # Convert decimal degrees to radians
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

        # Haversine formula
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        c = 2 * math.asin(math.sqrt(a))

        # Radius of earth in kilometers
        r = 6371

        return c * r

    def _get_destination_coordinates(self, destination: str) -> Optional[tuple]:
        """Get coordinates for common destinations"""
        # Destination can be provided as "lat,lon"
        try:
            if isinstance(destination, str) and "," in destination:
                lat_s, lon_s = destination.split(",", 1)
                lat = float(lat_s.strip())
                lon = float(lon_s.strip())
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    return (lat, lon)
        except Exception:
            pass
        return None

    def _find_nearest_beach(self, origin: str) -> Optional[Dict]:
        """Find nearest beach and travel time"""
        try:
            # Calculate times to all beaches using available method
            beach_times = []

            for beach in self.beaches:
                travel_data = self._get_travel_time_and_distance(origin, beach)
                if travel_data:
                    beach_name = (
                        beach.split(",")[0]
                        .replace("Playa de ", "")
                        .replace("Playa del ", "")
                    )
                    beach_times.append(
                        {
                            "name": beach_name,
                            "time": travel_data["time"],
                            "distance": travel_data["distance"],
                            "full_name": beach,
                        }
                    )

            if beach_times:
                # Return nearest beach by time
                nearest = min(beach_times, key=lambda x: x["time"])
                logger.info(
                    f"Nearest beach: {nearest['name']} ({nearest['time']} minutes, {nearest['distance']} km)"
                )
                return nearest

            return None

        except Exception:
            logger.error("Error finding nearest beach", exc_info=True)
            return None

    def _find_nearest_facility(
        self, origin: str, facilities: List[str]
    ) -> Optional[int]:
        """Find travel time to nearest facility from a list (legacy for backward compatibility)"""
        result = self._find_nearest_facility_with_distance(origin, facilities)
        return result["time"] if result else None

    def _find_nearest_facility_with_distance(
        self, origin: str, facilities: List[str]
    ) -> Optional[Dict]:
        """Find travel time and distance to nearest facility from a list"""
        if not facilities:
            return None

        try:
            # Calculate times and distances to all facilities using available method
            facility_data = []

            for facility in facilities:
                result = self._get_travel_time_and_distance(origin, facility)
                if result is not None:
                    facility_data.append(result)

            if facility_data:
                # Return nearest facility data (by time)
                nearest = min(facility_data, key=lambda x: x["time"])
                logger.info(
                    f"Nearest facility: {nearest['time']} min, {nearest['distance']} km"
                )
                return nearest

            return None

        except Exception:
            logger.error("Error finding nearest facility", exc_info=True)
            return None

    def generate_google_maps_route_url(
        self, origin_lat: float, origin_lon: float, destination: str
    ) -> str:
        """Generate Google Maps URL for route"""
        origin = f"{origin_lat},{origin_lon}"

        if destination == "oviedo":
            dest = self.destinations["oviedo"]
        elif destination == "gijon":
            dest = self.destinations["gijon"]
        else:
            dest = destination

        return f"https://www.google.com/maps/dir/{origin}/{dest}"
