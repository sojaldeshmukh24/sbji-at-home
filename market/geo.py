"""Location helpers for SBJI@Home.

Customers and vendors share a browser location (latitude / longitude).
This module holds the pure functions used to validate coordinates and to
measure the distance between a customer and a shop.
"""
import math

# Shops closer than this are "nearby". Farther shops are still shown, but are
# flagged so the customer knows delivery may take longer. Change it here only.
DELIVERY_RADIUS_KM = 2

EARTH_RADIUS_KM = 6371.0088


def parse_coordinates(latitude, longitude):
    """Return (lat, lng) as floats, or None if missing or out of range."""
    try:
        lat = float(str(latitude).strip())
        lng = float(str(longitude).strip())
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in kilometres between two points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def distance_to_vendor(location, vendor):
    """Distance in km from a (lat, lng) location to a vendor, or None."""
    if location is None or vendor is None:
        return None
    if vendor.latitude is None or vendor.longitude is None:
        return None
    return haversine_km(
        location[0], location[1], vendor.latitude, vendor.longitude,
    )


def is_far_away(distance_km):
    return distance_km is not None and distance_km > DELIVERY_RADIUS_KM


def format_distance(distance_km):
    """Human friendly distance: '350 m' or '1.4 km'."""
    if distance_km is None:
        return ''
    if distance_km < 1:
        return f'{int(round(distance_km * 1000, -1)) or 10} m'
    return f'{distance_km:.1f} km'
