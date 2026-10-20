(() => {
  const mapElement = document.getElementById("driverRequestMap");
  if (!mapElement || !window.L) return;

  const request = window.NEARFIND_DRIVER_REQUEST;
  const initialBooking = window.NEARFIND_DRIVER_BOOKING;

  // Mode 1: Live Trip Map & Driver Tracking for an existing request
  if (request && request.id) {
    const defaultCenter = [20, 0];
    const initialLat = request.pickup_lat != null ? request.pickup_lat : defaultCenter[0];
    const initialLng = request.pickup_lng != null ? request.pickup_lng : defaultCenter[1];

    const statusEl = document.getElementById("driverTrackingStatus");
    const distanceEl = document.getElementById("driverDistanceInfo");
    const lastUpdatedEl = document.getElementById("driverLastUpdated");

    const map = L.map(mapElement).setView([initialLat, initialLng], 13);
    const tileLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 19,
    }).addTo(map);

    tileLayer.on("tileerror", () => {
      if (statusEl) {
        statusEl.className = "notice location-status unavailable";
        statusEl.textContent = "Unable to load map tiles. Please check your internet connection.";
      }
    });

    window.addEventListener("resize", () => map.invalidateSize());
    if (window.ResizeObserver) {
      new ResizeObserver(() => map.invalidateSize()).observe(mapElement);
    }

    // Add pickup point marker
    if (request.pickup_lat != null && request.pickup_lng != null) {
      const pickupIcon = L.divIcon({
        className: "nf-marker blue",
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      });
      L.marker([request.pickup_lat, request.pickup_lng], { icon: pickupIcon })
        .addTo(map)
        .bindPopup(`<strong>Pickup</strong><br>${request.pickup_address || "Pickup location"}`);
    }

    // Add destination point marker
    if (request.destination_lat != null && request.destination_lng != null) {
      const destIcon = L.divIcon({
        className: "nf-marker green",
        iconSize: [22, 22],
        iconAnchor: [11, 11],
      });
      L.marker([request.destination_lat, request.destination_lng], { icon: destIcon })
        .addTo(map)
        .bindPopup(`<strong>Destination</strong><br>${request.destination_address || "Destination"}`);
    }

    // Fit map bounds to show route endpoints
    if (
      request.pickup_lat != null &&
      request.pickup_lng != null &&
      request.destination_lat != null &&
      request.destination_lng != null
    ) {
      const bounds = L.latLngBounds([
        [request.pickup_lat, request.pickup_lng],
        [request.destination_lat, request.destination_lng],
      ]);
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
    }

    let driverMarker = null;
    let pollTimer = null;
    let isFetching = false;
    let isTerminal = Boolean(
      request.status === "completed" ||
      request.status === "cancelled" ||
      request.status === "expired" ||
      (initialBooking && ["completed", "cancelled_by_driver", "cancelled_by_seeker"].includes(initialBooking.status))
    );
    const POLL_INTERVAL = 5000;

    function formatTimeAgo(timestamp) {
      if (!timestamp) return "time unknown";
      const time = new Date(timestamp).getTime();
      if (!Number.isFinite(time)) return "time unknown";
      const minutes = Math.max(0, Math.floor((Date.now() - time) / 60000));
      if (minutes < 1) return "just now";
      if (minutes < 60) return `${minutes}m ago`;
      const hours = Math.floor(minutes / 60);
      return `${hours}h ago`;
    }

    function updateDriverMarker(lat, lng, isStale, tripStatus, driverName) {
      if (lat == null || lng == null || !Number.isFinite(Number(lat)) || !Number.isFinite(Number(lng))) {
        if (driverMarker) {
          map.removeLayer(driverMarker);
          driverMarker = null;
        }
        return;
      }
      const latLng = [Number(lat), Number(lng)];
      const markerClass = `nf-marker yellow pulse ${isStale ? "stale" : ""}`.trim();
      const driverIcon = L.divIcon({
        className: markerClass,
        iconSize: [26, 26],
        iconAnchor: [13, 13],
      });

      const popupContent = `<strong>${driverName || "Driver"}</strong><br>Trip status: ${tripStatus || "In Progress"}`;

      if (!driverMarker) {
        driverMarker = L.marker(latLng, { icon: driverIcon }).addTo(map);
        driverMarker.bindPopup(popupContent);
      } else {
        driverMarker.setLatLng(latLng);
        driverMarker.setIcon(driverIcon);
        driverMarker.setPopupContent(popupContent);
      }
    }

    async function fetchDriverLocation() {
      if (isFetching || isTerminal || document.hidden) return;
      isFetching = true;
      try {
        const response = await fetch(
          `/seeker/driver-request/${encodeURIComponent(request.id)}/driver-location`,
          {
            headers: { Accept: "application/json" },
            cache: "no-store",
          }
        );
        const result = await response.json().catch(() => ({}));

        if (!response.ok || !result.success) {
          const errorMsg = result.error || "Driver location unavailable";
          if (
            response.status === 400 &&
            (errorMsg.includes("completed") || errorMsg.includes("cancelled"))
          ) {
            isTerminal = true;
            stopPolling();
            if (driverMarker) {
              map.removeLayer(driverMarker);
              driverMarker = null;
            }
            if (statusEl) {
              statusEl.className = "notice location-status";
              statusEl.textContent = errorMsg;
            }
            return;
          }
          if (response.status === 400 && errorMsg.includes("unassigned")) {
            if (statusEl) {
              statusEl.className = "notice location-status";
              statusEl.textContent = "Driver matching in progress... No driver assigned yet.";
            }
            return;
          }
          if (response.status === 403) {
            isTerminal = true;
            stopPolling();
            if (statusEl) {
              statusEl.className = "notice location-status unavailable";
              statusEl.textContent = "Access denied: you are not authorized to track this driver.";
            }
            return;
          }
          if (statusEl) {
            statusEl.className = "notice location-status unavailable";
            statusEl.textContent = errorMsg;
          }
          return;
        }

        const data = result.data;
        if (!data || data.lat == null || data.lng == null) {
          if (driverMarker) {
            map.removeLayer(driverMarker);
            driverMarker = null;
          }
          if (statusEl) {
            statusEl.className = "notice location-status unavailable";
            statusEl.textContent = "Driver location sharing is off or coordinates are unavailable.";
          }
          if (distanceEl) distanceEl.textContent = "";
          return;
        }

        // Display a driver marker only when a valid driver is assigned and the associated trip/request is actually in progress.
        const isInProgress =
          data.trip_status === "in_progress" ||
          (initialBooking && initialBooking.status === "in_progress");

        if (isInProgress) {
          updateDriverMarker(
            data.lat,
            data.lng,
            data.is_stale,
            data.trip_status,
            data.driver_name
          );

          if (statusEl) {
            if (data.is_stale) {
              statusEl.className = "notice location-status stale";
              statusEl.textContent = `Driver location may be outdated · updated ${formatTimeAgo(data.updated_at)}`;
            } else {
              statusEl.className = "notice location-status available";
              statusEl.textContent = `Driver en route · updated ${formatTimeAgo(data.updated_at)}`;
            }
          }

          if (distanceEl && data.distance_km != null) {
            distanceEl.textContent = `Driver is ~${data.distance_km} km from pickup`;
          }

          if (lastUpdatedEl && data.updated_at) {
            try {
              lastUpdatedEl.textContent = `Last update: ${new Date(data.updated_at).toLocaleTimeString()}`;
            } catch (_) {}
          }
        } else if (data.trip_status === "accepted") {
          // Driver assigned but trip has not begun yet
          if (driverMarker) {
            map.removeLayer(driverMarker);
            driverMarker = null;
          }
          if (statusEl) {
            statusEl.className = "notice location-status";
            statusEl.textContent = "Driver assigned · Waiting for trip to begin";
          }
          if (distanceEl && data.distance_km != null) {
            distanceEl.textContent = `Driver is ~${data.distance_km} km away`;
          }
        }
      } catch (err) {
        if (statusEl) {
          statusEl.className = "notice location-status unavailable";
          statusEl.textContent = "Network error updating driver location. Retrying...";
        }
      } finally {
        isFetching = false;
      }
    }

    function startPolling() {
      if (pollTimer || isTerminal) return;
      fetchDriverLocation();
      pollTimer = setInterval(fetchDriverLocation, POLL_INTERVAL);
    }

    function stopPolling() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    if (!isTerminal) {
      startPolling();
      document.addEventListener("visibilitychange", () => {
        if (document.hidden) {
          stopPolling();
        } else {
          startPolling();
        }
      });
      window.addEventListener("beforeunload", stopPolling);
      window.addEventListener("pagehide", stopPolling);
    } else {
      if (statusEl) {
        statusEl.className = "notice location-status";
        statusEl.textContent = `Trip ${request.status || "finalized"}. Tracking unavailable.`;
      }
    }

    window.setTimeout(() => map.invalidateSize(), 100);
    return;
  }

  // Mode 2: Form point picker mode when creating a new request
  const userLat = parseFloat(mapElement.dataset.userLat);
  const userLng = parseFloat(mapElement.dataset.userLng);
  const hasUserCoords = Number.isFinite(userLat) && Number.isFinite(userLng);
  const startCenter = hasUserCoords ? [userLat, userLng] : [20.5937, 78.9629];
  const startZoom = hasUserCoords ? 13 : 5;

  const status = document.getElementById("driverMapStatus");
  const map = L.map(mapElement).setView(startCenter, startZoom);
  const tileLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19,
  }).addTo(map);

  tileLayer.on("tileerror", () => {
    if (status) {
      status.className = "notice location-status unavailable";
      status.textContent = "Unable to load map tiles. Please check your internet connection.";
    }
  });

  const pickupMarkerIcon = L.divIcon({
    className: "nf-marker point-marker pickup-marker",
    html: '<span class="marker-letter">A</span>',
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  });

  const destMarkerIcon = L.divIcon({
    className: "nf-marker point-marker dest-marker",
    html: '<span class="marker-letter">B</span>',
    iconSize: [26, 26],
    iconAnchor: [13, 13],
  });

  const fields = {
    pickup: {
      lat: document.getElementById("pickupLat"),
      lng: document.getElementById("pickupLng"),
      marker: null,
    },
    destination: {
      lat: document.getElementById("destinationLat"),
      lng: document.getElementById("destinationLng"),
      marker: null,
    },
  };
  const offset = document.getElementById("timezoneOffset");
  let selected = "pickup";

  if (offset) offset.value = String(new Date().getTimezoneOffset());

  const setSelected = (key) => {
    selected = key;
    const pickupBtn = document.getElementById("selectPickupPoint");
    const destBtn = document.getElementById("selectDestinationPoint");
    if (pickupBtn) {
      pickupBtn.setAttribute("aria-pressed", String(key === "pickup"));
      pickupBtn.classList.toggle("active", key === "pickup");
    }
    if (destBtn) {
      destBtn.setAttribute("aria-pressed", String(key === "destination"));
      destBtn.classList.toggle("active", key === "destination");
    }
  };

  const updateRouteView = () => {
    if (fields.pickup.marker && fields.destination.marker) {
      const bounds = L.latLngBounds([
        fields.pickup.marker.getLatLng(),
        fields.destination.marker.getLatLng(),
      ]);
      map.fitBounds(bounds, { padding: [45, 45], maxZoom: 15 });
    }
    map.invalidateSize();
  };

  const setPoint = (key, latitude, longitude, addressText = null) => {
    const point = fields[key];
    if (!point || !point.lat || !point.lng) return;
    const isPickup = key === "pickup";
    point.lat.value = latitude.toFixed(6);
    point.lng.value = longitude.toFixed(6);

    const icon = isPickup ? pickupMarkerIcon : destMarkerIcon;
    const label = isPickup ? "Pickup Point (A)" : "Destination Point (B)";

    if (point.marker) map.removeLayer(point.marker);
    point.marker = L.marker([latitude, longitude], { icon }).addTo(map);
    point.marker.bindPopup(`<strong>${label}</strong><br>${latitude.toFixed(5)}, ${longitude.toFixed(5)}`);

    const textInput = document.getElementById(key);
    if (addressText && textInput) {
      textInput.value = addressText;
    } else if (textInput && !textInput.value.trim()) {
      textInput.value = `${latitude.toFixed(5)}, ${longitude.toFixed(5)}`;
    }

    const tagEl = document.getElementById(`${key}StatusTag`);
    if (tagEl) {
      tagEl.style.display = "inline-block";
      tagEl.textContent = `✓ ${latitude.toFixed(4)}, ${longitude.toFixed(4)}`;
    }

    const badgeSummary = document.getElementById(`${key}BadgeSummary`);
    if (badgeSummary) {
      badgeSummary.innerHTML = `${isPickup ? 'Pickup (A)' : 'Destination (B)'}: <strong style="color: ${isPickup ? '#60A5FA' : 'var(--lime)'};">${latitude.toFixed(4)}, ${longitude.toFixed(4)}</strong>`;
    }

    if (status) {
      status.className = "notice location-status available";
      status.textContent = `${label} set to ${latitude.toFixed(5)}, ${longitude.toFixed(5)}.`;
    }

    updateRouteView();
  };

  document.getElementById("selectPickupPoint")?.addEventListener("click", () => {
    setSelected("pickup");
    if (status) status.textContent = "Click map to set Pickup Point (A).";
  });

  document.getElementById("selectDestinationPoint")?.addEventListener("click", () => {
    setSelected("destination");
    if (status) status.textContent = "Click map to set Destination Point (B).";
  });

  map.on("click", (event) => {
    setPoint(selected, event.latlng.lat, event.latlng.lng);
    // If setting pickup point, suggest destination next if destination is not yet set
    if (selected === "pickup" && !fields.destination.marker) {
      setSelected("destination");
      if (status) {
        status.className = "notice location-status available";
        status.textContent = `Pickup Point (A) set. Click map to set Destination Point (B).`;
      }
    }
  });

  // Restore pre-filled coordinates if present (e.g. on validation re-render)
  const initialPLat = parseFloat(fields.pickup.lat?.value);
  const initialPLng = parseFloat(fields.pickup.lng?.value);
  if (Number.isFinite(initialPLat) && Number.isFinite(initialPLng)) {
    setPoint("pickup", initialPLat, initialPLng);
  }

  const initialDLat = parseFloat(fields.destination.lat?.value);
  const initialDLng = parseFloat(fields.destination.lng?.value);
  if (Number.isFinite(initialDLat) && Number.isFinite(initialDLng)) {
    setPoint("destination", initialDLat, initialDLng);
  }

  // Geolocation Handler for Pickup
  const handleCurrentLocation = () => {
    if (!navigator.geolocation) {
      if (status) {
        status.className = "notice location-status unavailable";
        status.textContent = "Geolocation is not supported by your browser. Click on the map to set pickup point.";
      }
      return;
    }

    const btns = [
      document.getElementById("useMyCurrentLocationBtn"),
      document.getElementById("useDriverPickupLocation"),
    ].filter(Boolean);

    btns.forEach((btn) => {
      btn.disabled = true;
      btn.dataset.origHtml = btn.dataset.origHtml || btn.innerHTML;
      btn.innerHTML = `<span style="display:inline-block;animation:spin .6s linear infinite;">⏳</span> Locating...`;
    });

    if (status) {
      status.className = "notice location-status";
      status.textContent = "Acquiring your current device location…";
    }

    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        setPoint("pickup", coords.latitude, coords.longitude, "Current Location");
        map.setView([coords.latitude, coords.longitude], 14);

        if (status) {
          status.className = "notice location-status available";
          status.textContent = `✓ Current location detected for Pickup (${coords.latitude.toFixed(5)}, ${coords.longitude.toFixed(5)}). Next: click map to set Destination.`;
        }
        if (!fields.destination.marker) {
          setSelected("destination");
        }
        btns.forEach((btn) => {
          btn.disabled = false;
          btn.innerHTML = btn.dataset.origHtml;
        });
        updateRouteView();
      },
      (err) => {
        btns.forEach((btn) => {
          btn.disabled = false;
          btn.innerHTML = btn.dataset.origHtml;
        });
        if (status) {
          status.className = "notice location-status unavailable";
          if (err.code === 1) {
            status.textContent = "Location permission denied. Click on the map to choose your pickup point.";
          } else if (err.code === 2) {
            status.textContent = "Location unavailable. Click on the map to choose your pickup point.";
          } else {
            status.textContent = "Unable to detect location. Click on the map to choose your pickup point.";
          }
        }
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 },
    );
  };

  document.getElementById("useMyCurrentLocationBtn")?.addEventListener("click", handleCurrentLocation);
  document.getElementById("useDriverPickupLocation")?.addEventListener("click", handleCurrentLocation);

  // Form submit validation guard
  const formEl = mapElement.closest("form") || document.querySelector(".seeker-driver-request-form");
  if (formEl) {
    formEl.addEventListener("submit", (e) => {
      const pLat = parseFloat(fields.pickup.lat?.value);
      const pLng = parseFloat(fields.pickup.lng?.value);
      const dLat = parseFloat(fields.destination.lat?.value);
      const dLng = parseFloat(fields.destination.lng?.value);

      if (!Number.isFinite(pLat) || !Number.isFinite(pLng)) {
        e.preventDefault();
        if (status) {
          status.className = "notice location-status unavailable";
          status.textContent = "Pickup point coordinates are required. Click map to set point A or use current location.";
        }
        setSelected("pickup");
        map.invalidateSize();
        return false;
      }

      if (!Number.isFinite(dLat) || !Number.isFinite(dLng)) {
        e.preventDefault();
        if (status) {
          status.className = "notice location-status unavailable";
          status.textContent = "Destination point coordinates are required. Click map to set point B.";
        }
        setSelected("destination");
        map.invalidateSize();
        return false;
      }
    });
  }

  // Amenities card keyboard accessibility & session persistence
  const amenityCards = document.querySelectorAll(".driver-amenity-card");
  amenityCards.forEach((card) => {
    const input = card.querySelector(".driver-amenity-input");
    if (!input) return;

    const syncCard = () => {
      card.classList.toggle("checked", input.checked);
      try {
        const stored = JSON.parse(sessionStorage.getItem("nearfind_amenities") || "[]");
        const set = new Set(stored);
        if (input.checked) set.add(input.value);
        else set.delete(input.value);
        sessionStorage.setItem("nearfind_amenities", JSON.stringify(Array.from(set)));
      } catch (_) {}
    };

    input.addEventListener("change", syncCard);
    syncCard();

    card.addEventListener("keydown", (e) => {
      if (e.key === " " || e.key === "Enter") {
        e.preventDefault();
        input.checked = !input.checked;
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
  });

  // Restore amenities from sessionStorage if page was reloaded
  try {
    const storedAmenities = JSON.parse(sessionStorage.getItem("nearfind_amenities") || "[]");
    storedAmenities.forEach((val) => {
      const inp = document.querySelector(`.driver-amenity-input[value="${val}"]`);
      if (inp && !inp.checked) {
        inp.checked = true;
        inp.closest(".driver-amenity-card")?.classList.add("checked");
      }
    });
  } catch (_) {}

  window.addEventListener("resize", () => map.invalidateSize());
  if (window.ResizeObserver) {
    new ResizeObserver(() => map.invalidateSize()).observe(mapElement);
  }
  window.setTimeout(() => map.invalidateSize(), 150);
})();
