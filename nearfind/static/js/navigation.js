(function () {
  const isValidCoord = (lat, lng) => {
    if (lat === null || lat === undefined || lng === null || lng === undefined) return false;
    const nLat = Number(lat);
    const nLng = Number(lng);
    if (!Number.isFinite(nLat) || !Number.isFinite(nLng)) return false;
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return false;
    return true;
  };

  const navigationUrl = (destLat, destLng, originLat, originLng) => {
    const dLat = Number(destLat);
    const dLng = Number(destLng);
    if (!isValidCoord(dLat, dLng)) return "";

    const url = new URL("https://www.google.com/maps/dir/");
    url.searchParams.set("api", "1");
    url.searchParams.set("destination", `${dLat},${dLng}`);

    if (isValidCoord(originLat, originLng)) {
      url.searchParams.set("origin", `${Number(originLat)},${Number(originLng)}`);
    }
    return url.toString();
  };

  const osmNavigationUrl = (destLat, destLng, originLat, originLng) => {
    const dLat = Number(destLat);
    const dLng = Number(destLng);
    if (!isValidCoord(dLat, dLng)) return "";

    if (isValidCoord(originLat, originLng)) {
      const oLat = Number(originLat);
      const oLng = Number(originLng);
      return `https://www.openstreetmap.org/directions?engine=fossgis_osrm_car&route=${encodeURIComponent(`${oLat},${oLng};${dLat},${dLng}`)}`;
    }
    return `https://www.openstreetmap.org/search?query=${encodeURIComponent(`${dLat},${dLng}`)}`;
  };

  const checkEndpoints = (destLat, destLng, originLat, originLng) => {
    const hasDest = isValidCoord(destLat, destLng);
    const hasOrigin = isValidCoord(originLat, originLng);
    return {
      canNavigate: hasDest,
      hasOrigin,
      hasDestination: hasDest,
      message: hasDest
        ? (hasOrigin ? "Ready for road route navigation." : "Using current device location as route origin.")
        : "Provider destination coordinates are unavailable.",
    };
  };

  const phoneUrl = (phone) => {
    const value = String(phone || "").replace(/[^+\d]/g, "");
    return value.length >= 7 ? `tel:${value}` : "";
  };

  window.NearFindNavigationUrl = navigationUrl;
  window.NearFindOsmNavigationUrl = osmNavigationUrl;
  window.NearFindCheckNavigationEndpoints = checkEndpoints;
  window.NearFindPhoneUrl = phoneUrl;
})();
