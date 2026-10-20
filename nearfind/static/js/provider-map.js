(function () {
  const mapEl = document.getElementById("providerMap");
  if (!mapEl) return;

  const radius = document.getElementById("radiusFilter");
  const category = document.getElementById("categoryFilter");
  const sort = document.getElementById("sortFilter");
  const list = document.getElementById("queryCards");
  const detail = document.getElementById("queryDetail");
  const count = document.getElementById("demandCount");
  const template = document.getElementById("queryTemplate");
  const refreshButton = document.getElementById("refreshRequests");
  const locationText = document.getElementById("providerLocationText");
  const availToggle = document.getElementById("providerAvailabilityToggle");
  const availLabel = document.getElementById("availabilityStatusLabel");
  const updateCoordsBtn = document.getElementById("updateProviderLocationBtn");

  const markers = new Map();
  const queriesById = new Map();
  let providerMarker;
  let activeQueryId;
  let fetchingQueries = false;

  document.querySelectorAll("[data-customer-navigation]").forEach((link) => {
    const url = window.NearFindNavigationUrl?.(link.dataset.lat, link.dataset.lng);
    if (!url) {
      link.hidden = true;
      return;
    }
    link.href = url;
  });
  document.querySelectorAll("[data-customer-call]").forEach((link) => {
    const url = window.NearFindPhoneUrl?.(link.dataset.phone);
    if (!url) {
      link.hidden = true;
      return;
    }
    link.href = url;
  });

  let lat = parseFloat(mapEl.dataset.lat);
  let lng = parseFloat(mapEl.dataset.lng);
  const map = L.map(mapEl).setView(Number.isFinite(lat) && Number.isFinite(lng) ? [lat, lng] : [20.5937, 78.9629], Number.isFinite(lat) ? 13 : 5);
  const tileLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  tileLayer.on("tileerror", () => {
    if (count) {
      count.className = "notice location-status unavailable";
      count.textContent = "Unable to load map tiles. Check your connection.";
    }
  });

  window.addEventListener("resize", () => map.invalidateSize());
  if (window.ResizeObserver) {
    new ResizeObserver(() => map.invalidateSize()).observe(mapEl);
  }
  window.setTimeout(() => map.invalidateSize(), 150);

  const icon = (color, pulse = false) => L.divIcon({
    className: `nf-marker ${color} ${pulse ? "pulse" : ""}`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });

  if (Number.isFinite(lat) && Number.isFinite(lng)) {
    providerMarker = L.marker([lat, lng], { icon: icon("blue") }).addTo(map).bindPopup("<strong>Your Store / Location</strong>");
  }

  function updateBrowserLocation() {
    if (!navigator.geolocation) {
      window.NearFindToast?.("Geolocation is not supported by your browser.", "error");
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      navigator.geolocation.getCurrentPosition(
        async (pos) => {
          try {
            lat = pos.coords.latitude;
            lng = pos.coords.longitude;
            mapEl.dataset.lat = lat;
            mapEl.dataset.lng = lng;
            const body = { lat, lng };
            await window.NearFindFetch("/user/update-location", {
              method: "POST",
              headers: { "Content-Type": "application/json", "X-CSRFToken": window.NearFindCSRFToken || "" },
              body: JSON.stringify(body),
            });
            const next = [lat, lng];
            if (!providerMarker) {
              providerMarker = L.marker(next, { icon: icon("blue") }).addTo(map);
            } else {
              providerMarker.setLatLng(next);
            }
            providerMarker.bindPopup("<strong>Your Store / Location</strong>");
            map.setView(next, 13);
            if (locationText) locationText.textContent = `${lat.toFixed(4)}, ${lng.toFixed(4)}`;
            window.NearFindToast?.("Location coordinates updated successfully.", "success");
          } catch (err) {
            window.NearFindToast?.("Failed to save updated coordinates.", "error");
          } finally {
            resolve();
          }
        },
        (err) => {
          window.NearFindToast?.(err.code === 1 ? "Location permission denied." : "Unable to detect location.", "error");
          resolve();
        },
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
      );
    });
  }

  // Interactive Availability / Location Sharing Toggle
  if (availToggle) {
    availToggle.addEventListener("change", async () => {
      const enable = availToggle.checked;
      availToggle.disabled = true;
      if (availLabel) availLabel.textContent = "Updating availability…";

      try {
        if (enable) {
          if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
            // Need coordinates first
            await updateBrowserLocation();
          }
          if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
            availToggle.checked = false;
            if (availLabel) availLabel.textContent = "Location paused (Offline)";
            window.NearFindToast?.("Please set your location coordinates before turning on live availability.", "error");
            return;
          }
          await window.NearFindFetch("/provider/location", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": window.NearFindCSRFToken || "" },
            body: JSON.stringify({ lat, lng, sharing_enabled: true }),
          });
          if (availLabel) availLabel.textContent = "Sharing location (Online)";
          window.NearFindToast?.("Live availability active. Nearby seekers can see you are open.", "success");
        } else {
          await window.NearFindFetch("/location/stop", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": window.NearFindCSRFToken || "" },
          });
          if (availLabel) availLabel.textContent = "Location paused (Offline)";
          window.NearFindToast?.("Location sharing paused.", "success");
        }
      } catch (err) {
        availToggle.checked = !enable;
        if (availLabel) availLabel.textContent = enable ? "Location paused (Offline)" : "Sharing location (Online)";
        window.NearFindToast?.(err.message || "Could not update availability state.", "error");
      } finally {
        availToggle.disabled = false;
      }
    });
  }

  const editCoordsBtn = document.getElementById("editProviderCoordsBtn");
  const manualCoordsCard = document.getElementById("providerManualCoordsCard");
  const manualLatInput = document.getElementById("manualProviderLat");
  const manualLngInput = document.getElementById("manualProviderLng");
  const saveManualCoordsBtn = document.getElementById("saveProviderManualCoordsBtn");
  const cancelManualCoordsBtn = document.getElementById("cancelProviderManualCoordsBtn");

  editCoordsBtn?.addEventListener("click", () => {
    if (!manualCoordsCard) return;
    manualCoordsCard.style.display = manualCoordsCard.style.display === "none" ? "block" : "none";
  });

  cancelManualCoordsBtn?.addEventListener("click", () => {
    if (manualCoordsCard) manualCoordsCard.style.display = "none";
  });

  saveManualCoordsBtn?.addEventListener("click", async () => {
    if (!manualLatInput || !manualLngInput) return;
    const nextLat = parseFloat(manualLatInput.value.trim());
    const nextLng = parseFloat(manualLngInput.value.trim());
    if (!Number.isFinite(nextLat) || nextLat < -90 || nextLat > 90) {
      window.NearFindToast?.("Latitude must be a valid number between -90 and 90.", "error");
      manualLatInput.focus();
      return;
    }
    if (!Number.isFinite(nextLng) || nextLng < -180 || nextLng > 180) {
      window.NearFindToast?.("Longitude must be a valid number between -180 and 180.", "error");
      manualLngInput.focus();
      return;
    }
    saveManualCoordsBtn.disabled = true;
    saveManualCoordsBtn.textContent = "Saving…";
    try {
      await window.NearFindFetch("/user/update-location", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": window.NearFindCSRFToken || "" },
        body: JSON.stringify({ lat: nextLat, lng: nextLng }),
      });
      lat = nextLat;
      lng = nextLng;
      mapEl.dataset.lat = nextLat;
      mapEl.dataset.lng = nextLng;
      const nextPos = [lat, lng];
      if (!providerMarker) {
        providerMarker = L.marker(nextPos, { icon: icon("blue") }).addTo(map);
      } else {
        providerMarker.setLatLng(nextPos);
      }
      providerMarker.bindPopup("<strong>Your Store / Location</strong>");
      map.setView(nextPos, 13);
      if (locationText) locationText.textContent = `${lat.toFixed(4)}, ${lng.toFixed(4)}`;
      if (manualCoordsCard) manualCoordsCard.style.display = "none";
      window.NearFindToast?.("Coordinates updated successfully.", "success");
      fetchQueries();
    } catch (err) {
      window.NearFindToast?.(err.message || "Failed to save coordinates.", "error");
    } finally {
      saveManualCoordsBtn.disabled = false;
      saveManualCoordsBtn.textContent = "Save Coords";
    }
  });

  if (updateCoordsBtn) {
    updateCoordsBtn.addEventListener("click", () => {
      updateCoordsBtn.disabled = true;
      updateCoordsBtn.textContent = "GPS…";
      updateBrowserLocation().finally(() => {
        updateCoordsBtn.disabled = false;
        updateCoordsBtn.textContent = "GPS";
        fetchQueries();
      });
    });
  }

  function formatTime(value) {
    if (!value) return "No expiry set";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "No expiry set";
    return date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  }

  function urgencyLabel(value) {
    return (value || "normal").replace("_", " ");
  }

  function showDetail(query) {
    if (!detail) return;
    if (!query) {
      detail.className = "query-detail panel empty";
      detail.textContent = "Select a request card or map marker to view details and submit an offer.";
      return;
    }
    detail.className = "query-detail panel";
    detail.replaceChildren();

    const title = document.createElement("h2");
    title.style.margin = "0 0 6px";
    title.style.fontSize = "20px";
    title.style.color = "#FFFFFF";
    title.textContent = query.item_name;

    const meta = document.createElement("p");
    meta.className = "muted";
    meta.style.fontSize = "13px";
    meta.style.margin = "0 0 10px";
    meta.textContent = `${query.distance_label} · ${query.category} · ${urgencyLabel(query.urgency).toUpperCase()}`;

    const seeker = document.createElement("p");
    seeker.style.fontSize = "13.5px";
    seeker.style.margin = "0 0 8px";
    seeker.textContent = `Requested by ${query.seeker_name || "Seeker"} near ${query.location || "saved location"}.`;

    const desc = document.createElement("p");
    desc.style.fontSize = "14px";
    desc.style.lineHeight = "1.5";
    desc.style.margin = "0 0 14px";
    desc.style.color = "var(--body)";
    desc.textContent = query.description;

    detail.append(title, meta, seeker, desc);

    if (query.image_url) {
      const img = document.createElement("img");
      img.src = query.image_url;
      img.alt = `${query.item_name} reference`;
      img.style.maxHeight = "180px";
      img.style.width = "100%";
      img.style.objectFit = "cover";
      img.style.borderRadius = "8px";
      img.style.marginBottom = "14px";
      detail.appendChild(img);
    }

    const foot = document.createElement("div");
    foot.style.display = "flex";
    foot.style.justifyContent = "space-between";
    foot.style.alignItems = "center";
    foot.style.gap = "10px";
    foot.style.flexWrap = "wrap";
    foot.style.marginTop = "12px";
    foot.style.paddingTop = "12px";
    foot.style.borderTop = "1px solid rgba(255,255,255,0.08)";

    const expiry = document.createElement("span");
    expiry.className = "muted";
    expiry.style.fontSize = "12px";
    expiry.textContent = `Expires ${formatTime(query.expires_at)}`;

    const link = document.createElement("a");
    link.className = "btn primary";
    link.href = `/provider/respond/${query.id}`;
    link.textContent = "Submit Offer";
    link.style.padding = "8px 18px";

    foot.append(expiry, link);
    detail.appendChild(foot);
  }

  function showEmpty(message) {
    list.replaceChildren();
    queriesById.clear();
    activeQueryId = undefined;
    markers.forEach((marker) => marker.remove());
    markers.clear();
    showDetail();
    if (count) count.textContent = message;
  }

  function setActiveQuery(id) {
    activeQueryId = id;
    list.querySelectorAll(".query-card").forEach((card) => {
      const match = card.dataset.queryId === id;
      card.classList.toggle("active", match);
      if (match) {
        try {
          card.scrollIntoView({ behavior: "smooth", block: "nearest" });
        } catch (_) {}
      }
    });
    const marker = markers.get(id);
    if (marker) {
      marker.openPopup();
      map.panTo(marker.getLatLng(), { animate: true });
    }
    showDetail(queriesById.get(id));
  }

  function renderCard(query) {
    const node = template.content.firstElementChild.cloneNode(true);
    node.dataset.queryId = query.id;
    node.querySelector("[data-title]").textContent = query.item_name;
    node.querySelector("[data-meta]").textContent = `${query.distance_label} · ${formatTime(query.created_at)}`;
    
    const distEl = node.querySelector("[data-distance]");
    if (distEl) distEl.textContent = query.distance_label;
    const catEl = node.querySelector("[data-category]");
    if (catEl) catEl.textContent = query.category;

    const urgencyEl = node.querySelector("[data-urgency]");
    if (urgencyEl) {
      const uLabel = urgencyLabel(query.urgency);
      urgencyEl.textContent = uLabel;
      urgencyEl.className = `status-chip ${query.urgency === 'very_urgent' || query.urgency === 'urgent' ? 'open' : 'available'}`;
    }

    node.querySelector("[data-location]").textContent = "📍 " + (query.location || "Saved request location");
    node.querySelector("[data-description]").textContent = query.description;
    
    const respondLink = node.querySelector("[data-respond]");
    if (respondLink) {
      respondLink.href = `/provider/respond/${query.id}`;
      respondLink.textContent = "Submit Offer";
    }

    const inspectBtn = node.querySelector("[data-inspect]");
    if (inspectBtn) {
      inspectBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        setActiveQuery(query.id);
      });
    }

    node.querySelector("[data-expiration]").textContent = `Expires ${formatTime(query.expires_at)}`;
    
    const imageSlot = node.querySelector("[data-image]");
    if (query.image_url) {
      const img = document.createElement("img");
      img.src = query.image_url;
      img.alt = `${query.item_name} reference`;
      img.style.height = "72px";
      img.style.width = "100%";
      img.style.objectFit = "cover";
      img.style.borderRadius = "6px";
      imageSlot.appendChild(img);
    } else {
      imageSlot.remove();
    }

    node.addEventListener("mouseenter", () => markers.get(query.id)?.openPopup());
    node.addEventListener("click", (event) => {
      if (event.target.closest("a")) return;
      setActiveQuery(query.id);
    });
    return node;
  }

  function renderQueries(queries) {
    const openQueries = queries.filter((query) => query.status === "open");
    if (!openQueries.length) {
      showEmpty("No nearby open requests match these filters.");
      return;
    }
    list.replaceChildren();
    queriesById.clear();
    const liveIds = new Set(openQueries.map((query) => query.id));
    markers.forEach((marker, id) => {
      if (!liveIds.has(id)) {
        marker.remove();
        markers.delete(id);
      }
    });
    if (activeQueryId && !liveIds.has(activeQueryId)) activeQueryId = undefined;
    openQueries.forEach((query) => {
      queriesById.set(query.id, query);
      list.appendChild(renderCard(query));
      const popup = document.createElement("div");
      popup.style.padding = "4px";
      const title = document.createElement("strong");
      title.style.display = "block";
      title.style.fontSize = "14px";
      title.textContent = query.item_name;
      const meta = document.createElement("p");
      meta.style.margin = "4px 0 8px";
      meta.style.fontSize = "12px";
      meta.textContent = `${query.distance_label} · ${urgencyLabel(query.urgency)}`;
      const link = document.createElement("a");
      link.className = "btn small primary";
      link.href = `/provider/respond/${query.id}`;
      link.textContent = "Submit Offer";
      link.style.display = "inline-block";
      link.style.padding = "4px 10px";
      link.style.fontSize = "11px";
      popup.append(title, meta);
      if (query.image_url) {
        const img = document.createElement("img");
        img.src = query.image_url;
        img.alt = "";
        img.className = "popup-img";
        img.style.maxHeight = "90px";
        img.style.width = "100%";
        img.style.objectFit = "cover";
        img.style.borderRadius = "4px";
        img.style.marginBottom = "6px";
        popup.appendChild(img);
      }
      popup.appendChild(link);
      if (markers.has(query.id)) {
        markers.get(query.id).setLatLng([query.lat, query.lng]).setPopupContent(popup).off("click");
      } else {
        markers.set(query.id, L.marker([query.lat, query.lng], { icon: icon("yellow", true) }).addTo(map).bindPopup(popup));
      }
      markers.get(query.id).on("click", () => setActiveQuery(query.id));
    });
    if (count) {
      count.textContent = openQueries.length === 1 ? "1 person nearby is looking for products." : `${openQueries.length} people nearby are looking for products.`;
    }
    setActiveQuery(queriesById.has(activeQueryId) ? activeQueryId : openQueries[0].id);
    if (openQueries.length) {
      const group = L.featureGroup([...markers.values(), ...(providerMarker ? [providerMarker] : [])]);
      map.fitBounds(group.getBounds().pad(0.2), { maxZoom: 14 });
    }
  }

  async function fetchQueries() {
    if (fetchingQueries || document.hidden) return;
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
      showEmpty("Set your location coordinates above (GPS or Manual) to discover nearby requests.");
      return;
    }
    fetchingQueries = true;
    if (count) count.textContent = "Loading nearby requests...";
    if (refreshButton) refreshButton.disabled = true;
    try {
      const params = new URLSearchParams({ radius_km: radius.value, category: category.value, sort: sort.value });
      const { payload } = await window.NearFindFetch(`/query/list?${params.toString()}`, { cache: "no-store" });
      renderQueries(payload.data.queries);
    } catch (error) {
      showEmpty(error.message || "Could not load nearby requests. Check your connection and try again.");
    } finally {
      fetchingQueries = false;
      if (refreshButton) refreshButton.disabled = false;
    }
  }

  let timer;
  const debouncedFetch = () => {
    clearTimeout(timer);
    timer = setTimeout(fetchQueries, 250);
  };
  [radius, category, sort].forEach((el) => el.addEventListener("input", debouncedFetch));
  refreshButton?.addEventListener("click", fetchQueries);

  // Directly fetch queries using saved coordinates without waiting for unprompted browser geolocation
  fetchQueries();

  const pollTimer = setInterval(() => {
    if (!document.hidden) fetchQueries();
  }, 30000);

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) fetchQueries();
  });
  window.addEventListener("pagehide", () => {
    clearTimeout(timer);
    clearInterval(pollTimer);
  }, { once: true });
})();
