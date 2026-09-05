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
  const markers = new Map();
  const queriesById = new Map();
  let providerMarker;
  let activeQueryId;

  const lat = parseFloat(mapEl.dataset.lat);
  const lng = parseFloat(mapEl.dataset.lng);
  const map = L.map(mapEl).setView(Number.isFinite(lat) && Number.isFinite(lng) ? [lat, lng] : [20.5937, 78.9629], Number.isFinite(lat) ? 13 : 5);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  const icon = (color, pulse = false) => L.divIcon({
    className: `nf-marker ${color} ${pulse ? "pulse" : ""}`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });

  if (Number.isFinite(lat) && Number.isFinite(lng)) {
    providerMarker = L.marker([lat, lng], { icon: icon("blue") }).addTo(map).bindPopup("Your location");
  }

  async function updateBrowserLocation() {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(async (pos) => {
      const body = { lat: pos.coords.latitude, lng: pos.coords.longitude };
      await fetch("/user/update-location", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": window.NearFindCSRFToken || "" },
        body: JSON.stringify(body),
      });
      const next = [body.lat, body.lng];
      if (!providerMarker) providerMarker = L.marker(next, { icon: icon("blue") }).addTo(map);
      providerMarker.setLatLng(next);
      if (locationText) locationText.textContent = `${body.lat.toFixed(5)}, ${body.lng.toFixed(5)}`;
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
      detail.className = "query-detail empty";
      detail.textContent = "Select a request card or marker to inspect details.";
      return;
    }
    detail.className = "query-detail";
    detail.replaceChildren();
    const title = document.createElement("h2");
    title.textContent = query.item_name;
    const meta = document.createElement("p");
    meta.className = "muted";
    meta.textContent = `${query.distance} km away - ${query.category} - ${urgencyLabel(query.urgency)}`;
    const seeker = document.createElement("p");
    seeker.textContent = `Requested by ${query.seeker_name || "Seeker"} near ${query.location || "the saved request location"}.`;
    const desc = document.createElement("p");
    desc.textContent = query.description;
    const expiry = document.createElement("p");
    expiry.className = "notice";
    expiry.textContent = `Expires ${formatTime(query.expires_at)}`;
    const link = document.createElement("a");
    link.className = "btn primary";
    link.href = `/provider/respond/${query.id}`;
    link.textContent = "Respond to Request";
    detail.append(title, meta, seeker, desc);
    if (query.image_url) {
      const img = document.createElement("img");
      img.src = query.image_url;
      img.alt = `${query.item_name} reference`;
      detail.appendChild(img);
    }
    detail.append(expiry, link);
  }

  function showEmpty(message) {
    list.replaceChildren();
    queriesById.clear();
    activeQueryId = undefined;
    markers.forEach((marker) => marker.remove());
    markers.clear();
    showDetail();
    count.textContent = message;
  }

  function setActiveQuery(id) {
    activeQueryId = id;
    list.querySelectorAll(".query-card").forEach((card) => {
      card.classList.toggle("active", card.dataset.queryId === id);
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
    node.querySelector("[data-meta]").textContent = `${query.distance} km away - ${query.category} - ${formatTime(query.created_at)}`;
    node.querySelector("[data-urgency]").textContent = urgencyLabel(query.urgency);
    node.querySelector("[data-location]").textContent = query.location || "Saved request location";
    node.querySelector("[data-description]").textContent = query.description;
    node.querySelector("[data-respond]").href = `/provider/respond/${query.id}`;
    node.querySelector("[data-expiration]").textContent = `Expires ${formatTime(query.expires_at)}`;
    const imageSlot = node.querySelector("[data-image]");
    if (query.image_url) {
      const img = document.createElement("img");
      img.src = query.image_url;
      img.alt = `${query.item_name} reference`;
      imageSlot.appendChild(img);
    }
    node.addEventListener("mouseenter", () => markers.get(query.id)?.openPopup());
    node.addEventListener("click", (event) => {
      if (event.target.closest("a")) return;
      setActiveQuery(query.id);
    });
    return node;
  }

  function renderQueries(queries) {
    if (!queries.length) {
      showEmpty("No nearby open requests match these filters.");
      return;
    }
    list.replaceChildren();
    queriesById.clear();
    const liveIds = new Set(queries.map((query) => query.id));
    markers.forEach((marker, id) => {
      if (!liveIds.has(id)) {
        marker.remove();
        markers.delete(id);
      }
    });
    queries.forEach((query) => {
      queriesById.set(query.id, query);
      list.appendChild(renderCard(query));
      const popup = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = query.item_name;
      const meta = document.createElement("p");
      meta.textContent = `${query.distance} km away - ${urgencyLabel(query.urgency)}`;
      const link = document.createElement("a");
      link.href = `/provider/respond/${query.id}`;
      link.textContent = "I Have It";
      popup.append(title, meta);
      if (query.image_url) {
        const img = document.createElement("img");
        img.src = query.image_url;
        img.alt = "";
        img.className = "popup-img";
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
    count.textContent = queries.length === 1 ? "1 person nearby is looking for products." : `${queries.length} people nearby are looking for products.`;
    setActiveQuery(queriesById.has(activeQueryId) ? activeQueryId : queries[0].id);
    if (queries.length) {
      const group = L.featureGroup([...markers.values(), ...(providerMarker ? [providerMarker] : [])]);
      map.fitBounds(group.getBounds().pad(0.2), { maxZoom: 14 });
    }
  }

  async function fetchQueries() {
    count.textContent = "Loading nearby requests...";
    if (refreshButton) refreshButton.disabled = true;
    try {
      const params = new URLSearchParams({ radius_km: radius.value, category: category.value, sort: sort.value });
      const res = await fetch(`/query/list?${params.toString()}`);
      const payload = await res.json();
      if (!payload.success) {
        showEmpty(payload.error || "Could not load nearby requests.");
        return;
      }
      renderQueries(payload.data.queries);
    } catch (error) {
      showEmpty("Could not load nearby requests. Check your connection and try again.");
    } finally {
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
  updateBrowserLocation().finally(fetchQueries);
  setInterval(fetchQueries, 30000);
})();
