import math


def haversine(lat1, lng1, lat2, lng2):
    R = 6371
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


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
