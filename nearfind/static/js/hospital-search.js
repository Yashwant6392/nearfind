(function () {
  const mapEl = document.getElementById("hospitalMap");
  const resultsEl = document.getElementById("hospitalResults");
  const statusEl = document.getElementById("hospitalSearchStatus");
  const countEl = document.getElementById("hospitalResultCount");
  const locationStatus = document.getElementById("emergencyLocationStatus");
  const locationButton = document.getElementById("useEmergencyLocation");
  const searchButton = document.getElementById("findHospitals");
  const refreshButton = document.getElementById("refreshHospitals");
  const radiusSelect = document.getElementById("hospitalRadius");
  const detailsDialog = document.getElementById("hospitalDetails");
  const detailTitle = document.getElementById("hospitalDetailsTitle");
  const detailMeta = document.getElementById("hospitalDetailsMeta");
  const detailAvailability = document.getElementById("hospitalDetailsAvailability");
  const detailFreshness = document.getElementById("hospitalDetailsFreshness");
  const detailActions = document.getElementById("hospitalDetailsActions");
  const closeDetails = document.getElementById("closeHospitalDetails");
  if (!mapEl || !resultsEl || !statusEl) return;

  const bedLabels = { general: "General", icu: "ICU", oxygen: "Oxygen-supported", emergency: "Emergency", isolation: "Isolation" };
  let coordinates;
  let map;
  const markers = new Map();
  let resultsSnapshot = [];
  let lastFocusedElement;
  let searching = false;

  if (window.L) {
    map = L.map(mapEl).setView([20, 0], 2);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors",
    }).addTo(map);
  }

  const selectedBedType = () => document.querySelector("input[name='bed_type']:checked")?.value || "general";
  const formatUpdated = (result) => {
    if (result.updated_minutes_ago === null || result.updated_minutes_ago === undefined) return "Update time unavailable";
    if (result.updated_minutes_ago < 1) return "Updated just now";
    if (result.updated_minutes_ago < 60) return `Updated ${result.updated_minutes_ago} minute${result.updated_minutes_ago === 1 ? "" : "s"} ago`;
    const hours = Math.floor(result.updated_minutes_ago / 60);
    return `Updated ${hours} hour${hours === 1 ? "" : "s"} ago`;
  };
  const clearMap = () => markers.forEach((marker, id) => {
    marker.remove();
    markers.delete(id);
  });
  const showStatus = (message, kind = "empty") => {
    statusEl.textContent = message;
    statusEl.className = kind;
    statusEl.hidden = false;
  };

  function renderDetails(result) {
    lastFocusedElement = document.activeElement;
    detailTitle.textContent = result.name || "Hospital";
    detailMeta.textContent = `${result.distance_km} km away${result.address ? ` · ${result.address}` : ""}${result.phone ? ` · ${result.phone}` : ""}`;
    detailAvailability.replaceChildren();
    Object.entries(result.availability || {}).forEach(([type, count]) => {
      const term = document.createElement("dt");
      term.textContent = bedLabels[type] || type;
      const value = document.createElement("dd");
      value.textContent = `${count} available`;
      detailAvailability.append(term, value);
    });
    detailFreshness.textContent = `${formatUpdated(result)} · ${result.freshness_label}`;
    detailActions.replaceChildren();
    if (result.requested_count > 0) {
      const holdBtn = document.createElement("button");
      holdBtn.className = "btn primary small";
      holdBtn.type = "button";
      holdBtn.textContent = `Hold 1 ${bedLabels[selectedBedType()] || "Bed"}`;
      holdBtn.addEventListener("click", () => requestBedHold(result, holdBtn));
      detailActions.appendChild(holdBtn);
    }
    const navigationUrl = navigationUrlFor(result);
    if (navigationUrl) detailActions.appendChild(actionLink("Start navigation", navigationUrl, true));
    if (result.phone) detailActions.appendChild(actionLink("Call hospital", `tel:${phoneValue(result.phone)}`));
    if (typeof detailsDialog.showModal === "function") detailsDialog.showModal();
    closeDetails.focus();
  }

  async function requestBedHold(result, buttonEl) {
    const bedName = bedLabels[selectedBedType()] || selectedBedType();
    if (!confirm(`Request a temporary 2-hour emergency hold for 1 ${bedName} bed at ${result.name || "this hospital"}?`)) {
      return;
    }
    const originalText = buttonEl.textContent;
    buttonEl.disabled = true;
    buttonEl.textContent = "Reserving hold...";
    try {
      const { payload } = await window.NearFindFetch(`/hospitals/${result.hospital_id}/hold-bed`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": window.NearFindCSRFToken,
        },
        body: JSON.stringify({ bed_type: selectedBedType() }),
      });
      const message = payload?.data?.message || "Emergency bed hold confirmed!";
      if (typeof window.NearFindToast === "function") {
        window.NearFindToast(message, "success");
      } else {
        alert(message);
      }
      if (typeof detailsDialog?.close === "function" && detailsDialog.open) {
        detailsDialog.close();
      }
      search();
    } catch (err) {
      const errorMsg = err?.payload?.error?.message || err.message || "Failed to reserve bed hold.";
      if (typeof window.NearFindToast === "function") {
        window.NearFindToast(errorMsg, "error");
      } else {
        alert(errorMsg);
      }
      buttonEl.disabled = false;
      buttonEl.textContent = originalText;
    }
  }

  const phoneValue = (phone) => String(phone).replace(/[^+\d]/g, "");
  const navigationUrlFor = (result) => {
    const latitude = Number(result.lat);
    const longitude = Number(result.lng);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return "";
    const destination = encodeURIComponent(`${latitude},${longitude}`);
    return `https://www.google.com/maps/dir/?api=1&destination=${destination}`;
  };
  const actionLink = (label, href, external = false) => {
    const link = document.createElement("a");
    link.className = "btn small";
    link.textContent = label;
    link.href = href;
    if (external) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    return link;
  };

  function setSelected(resultId) {
    document.querySelectorAll(".hospital-card").forEach((card) => card.classList.toggle("selected", card.dataset.hospitalId === resultId));
    markers.forEach((marker, id) => {
      marker.setZIndexOffset(id === resultId ? 1000 : 0);
      marker.getElement()?.classList.toggle("selected", id === resultId);
    });
  }

  function markerFor(result) {
    let marker = markers.get(result.hospital_id);
    if (!marker) {
      marker = L.marker([result.lat, result.lng], { icon: L.divIcon({ className: "hospital-marker", iconSize: [24, 24], iconAnchor: [12, 12] }) });
      marker.addTo(map);
      marker.on("click", () => {
        setSelected(result.hospital_id);
        renderDetails(result);
      });
      markers.set(result.hospital_id, marker);
    } else {
      marker.setLatLng([result.lat, result.lng]);
    }
    marker.bindPopup(`${result.name || "Hospital"}<br>${result.bed_label} · ${result.requested_count} bed${result.requested_count === 1 ? "" : "s"}<br>${result.distance_km} km away`);
    return marker;
  }

  function renderResults(results) {
    resultsEl.replaceChildren();
    resultsSnapshot = results;
    const resultLabel = `${results.length} hospital${results.length === 1 ? "" : "s"} found nearby`;
    countEl.textContent = results.length ? resultLabel : "";
    if (!results.length) {
      showStatus("No hospitals with recently reported availability were found nearby.");
      clearMap();
      return;
    }
    statusEl.hidden = true;
    const bounds = [];
    results.forEach((result) => {
      const card = document.createElement("article");
      card.className = "hospital-card";
      card.dataset.hospitalId = result.hospital_id;
      const heading = document.createElement("div");
      heading.className = "hospital-card-heading";
      const title = document.createElement("h3");
      title.textContent = result.name || "Hospital";
      const status = document.createElement("span");
      const stale = result.freshness_state === "stale";
      status.className = `status-chip ${stale ? "stale" : result.availability_status === "available" ? "available" : "expired"}`;
      status.textContent = stale ? `${result.bed_label} reported` : result.availability_status === "available" ? `${result.bed_label} available` : `${result.bed_label} currently full`;
      heading.append(title, status);
      const count = document.createElement("p");
      count.className = "hospital-bed-count";
      count.textContent = result.freshness_state === "stale"
        ? `${result.requested_count} ${result.bed_label} bed${result.requested_count === 1 ? "" : "s"} reported`
        : result.requested_count > 0
          ? `${result.requested_count} ${result.bed_label} bed${result.requested_count === 1 ? "" : "s"} available`
          : `No ${result.bed_label} beds currently reported`;
      const meta = document.createElement("p");
      meta.className = "muted";
      meta.textContent = `${result.distance_km} km away · ${formatUpdated(result)}`;
      const freshness = document.createElement("p");
      freshness.className = `hospital-freshness ${result.freshness_state}`;
      freshness.textContent = result.freshness_state === "stale" ? "Availability may be outdated." : result.freshness_label;
      const details = document.createElement("button");
      details.className = "btn small";
      details.type = "button";
      details.textContent = "View details";
      details.addEventListener("click", () => {
        setSelected(result.hospital_id);
        renderDetails(result);
      });
      card.append(heading, count, meta, freshness, details);
      if (result.requested_count > 0) {
        const holdCardBtn = document.createElement("button");
        holdCardBtn.className = "btn primary small";
        holdCardBtn.type = "button";
        holdCardBtn.textContent = "Hold bed";
        holdCardBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          requestBedHold(result, holdCardBtn);
        });
        card.appendChild(holdCardBtn);
      }
      const navigationUrl = navigationUrlFor(result);
      if (navigationUrl) card.appendChild(actionLink("Navigate", navigationUrl, true));
      if (result.phone) card.appendChild(actionLink("Call hospital", `tel:${phoneValue(result.phone)}`));
      card.addEventListener("click", (event) => {
        if (event.target.closest("a,button")) return;
        setSelected(result.hospital_id);
        markers.get(result.hospital_id)?.openPopup();
      });
      resultsEl.appendChild(card);
      if (map) {
        markerFor(result);
        bounds.push([result.lat, result.lng]);
      }
    });
    const resultIds = new Set(results.map((result) => result.hospital_id));
    markers.forEach((marker, id) => {
      if (!resultIds.has(id)) {
        marker.remove();
        markers.delete(id);
      }
    });
    if (map && bounds.length) map.fitBounds(bounds, { padding: [24, 24], maxZoom: 14 });
  }

  const manualToggle = document.getElementById("toggleEmergencyManualCoords");
  const manualBox = document.getElementById("emergencyManualLocation");
  const manualLat = document.getElementById("manualEmergencyLat");
  const manualLng = document.getElementById("manualEmergencyLng");
  const manualApply = document.getElementById("applyEmergencyManualCoords");

  function showManualLocationBox() {
    if (manualBox) manualBox.style.display = "block";
  }

  manualToggle?.addEventListener("click", () => {
    if (!manualBox) return;
    manualBox.style.display = manualBox.style.display === "none" ? "block" : "none";
  });

  manualApply?.addEventListener("click", () => {
    if (!manualLat || !manualLng) return;
    const lat = parseFloat(manualLat.value);
    const lng = parseFloat(manualLng.value);
    if (!Number.isFinite(lat) || lat < -90 || lat > 90) {
      window.NearFindToast?.("Latitude must be a valid number between -90 and 90.", "error");
      manualLat.focus();
      return;
    }
    if (!Number.isFinite(lng) || lng < -180 || lng > 180) {
      window.NearFindToast?.("Longitude must be a valid number between -180 and 180.", "error");
      manualLng.focus();
      return;
    }
    coordinates = { latitude: lat, longitude: lng };
    locationStatus.textContent = `Coordinates set: ${lat.toFixed(4)}, ${lng.toFixed(4)} ✓`;
    locationStatus.className = "notice location-ready";
    locationButton.textContent = "Refresh My Location";
    if (map) map.setView([lat, lng], 13);
    search();
  });

  function initSavedLocation() {
    const lat = parseFloat(mapEl.dataset.lat);
    const lng = parseFloat(mapEl.dataset.lng);
    if (Number.isFinite(lat) && Number.isFinite(lng) && lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180) {
      coordinates = { latitude: lat, longitude: lng };
      locationStatus.textContent = `Using your saved location (${lat.toFixed(4)}, ${lng.toFixed(4)}).`;
      locationStatus.className = "notice location-ready";
      locationButton.textContent = "Refresh My Location";
      if (manualLat) manualLat.value = lat;
      if (manualLng) manualLng.value = lng;
      if (map) map.setView([lat, lng], 12);
    }
  }

  function getLocation() {
    if (!navigator.geolocation) {
      locationStatus.textContent = "Geolocation is not supported by your browser. Please enter coordinates manually.";
      locationStatus.className = "notice location-error";
      showManualLocationBox();
      return;
    }
    locationButton.disabled = true;
    locationButton.textContent = "Finding your location...";
    locationStatus.textContent = "Waiting for your location permission...";
    locationStatus.className = "notice";
    navigator.geolocation.getCurrentPosition(
      (position) => {
        coordinates = { latitude: position.coords.latitude, longitude: position.coords.longitude };
        locationStatus.textContent = `Location detected (${coordinates.latitude.toFixed(4)}, ${coordinates.longitude.toFixed(4)}) ✓`;
        locationStatus.className = "notice location-ready";
        locationButton.disabled = false;
        locationButton.textContent = "Location detected ✓";
        if (manualLat) manualLat.value = coordinates.latitude.toFixed(6);
        if (manualLng) manualLng.value = coordinates.longitude.toFixed(6);
        if (map) map.setView([coordinates.latitude, coordinates.longitude], 13);
        search();
      },
      (error) => {
        locationButton.disabled = false;
        locationButton.textContent = "Use my current location";
        showManualLocationBox();
        if (error.code === 1) {
          locationStatus.textContent = "Location permission denied. Enter coordinates manually below or enable location in browser settings.";
        } else if (error.code === 3) {
          locationStatus.textContent = "Location request timed out. Please retry or enter coordinates manually below.";
        } else {
          locationStatus.textContent = "Location unavailable. Please enter coordinates manually below.";
        }
        locationStatus.className = "notice location-error";
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
    );
  }

  async function search() {
    if (searching) return;
    if (!coordinates) {
      showStatus("Please detect or enter your location to find nearby hospitals.", "error");
      showManualLocationBox();
      return;
    }
    searching = true;
    searchButton.disabled = true;
    if (refreshButton) refreshButton.hidden = false;
    searchButton.textContent = "Searching...";
    showStatus("Finding nearby hospitals...", "loading");
    try {
      const params = new URLSearchParams({ ...coordinates, radius: radiusSelect.value, bed_type: selectedBedType() });
      const { payload } = await window.NearFindFetch(`/hospitals/search?${params.toString()}`, { cache: "no-store" });
      renderResults(payload.data.hospitals || []);
    } catch (error) {
      showStatus(error.message || "Unable to load hospital availability. Please try again.", "error");
      resultsEl.replaceChildren();
      countEl.textContent = "";
      clearMap();
    } finally {
      searching = false;
      searchButton.disabled = false;
      searchButton.textContent = "Find available hospitals";
    }
  }

  locationButton.addEventListener("click", getLocation);
  searchButton.addEventListener("click", search);
  refreshButton?.addEventListener("click", search);
  document.querySelectorAll("input[name='bed_type']").forEach((input) => input.addEventListener("change", () => {
    if (coordinates && resultsSnapshot.length) search();
  }));
  closeDetails.addEventListener("click", () => detailsDialog.close());
  detailsDialog.addEventListener("close", () => lastFocusedElement?.focus());
  detailsDialog.addEventListener("click", (event) => {
    if (event.target === detailsDialog) detailsDialog.close();
  });

  // Initialize with saved user location if present, without triggering unprompted browser geolocation
  initSavedLocation();
})();
