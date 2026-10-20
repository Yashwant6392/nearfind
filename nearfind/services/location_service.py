import math


def valid_coordinates(latitude, longitude):
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def haversine(lat1, lng1, lat2, lng2):
    if not valid_coordinates(lat1, lng1) or not valid_coordinates(lat2, lng2):
        raise ValueError("Coordinates must be finite latitude/longitude pairs within valid ranges")
    lat1, lng1, lat2, lng2 = map(float, (lat1, lng1, lat2, lng2))
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    a = min(1.0, max(0.0, a))
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def format_distance(distance_km):
    if not math.isfinite(distance_km) or distance_km < 0:
        raise ValueError("Distance must be a finite non-negative number")
    if 0 < distance_km < 0.001:
        return "<1 m away"
    if distance_km < 1:
        return f"{round(distance_km * 1000)} m away"
    return f"{distance_km:.2f} km away"


def coerce_float(value, name, min_value=None, max_value=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    if min_value is not None and number < min_value:
        raise ValueError(f"{name} must be at least {min_value}")
    if max_value is not None and number > max_value:
        raise ValueError(f"{name} must be at most {max_value}")
    return number


def public_provider_location(provider, selected=False):
    lat = provider.get("lat")
    lng = provider.get("lng")
    if lat is None or lng is None:
        return None, None
    if provider.get("provider_type") == "individual" and not selected:
        return round(float(lat), 3), round(float(lng), 3)
    return float(lat), float(lng)
