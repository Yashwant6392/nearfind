(function () {
  const mapEl = document.getElementById("seekerMap");
  if (!mapEl) return;

  let query = window.NEARFIND_QUERY;
  const responsesList = document.getElementById("responsesList");
  const providerCount = document.getElementById("providerCount");
  const template = document.getElementById("responseTemplate");
  const responseSort = document.getElementById("responseSort");
  const responseFilter = document.getElementById("responseFilter");
  const details = document.getElementById("responseDetails");
  const resolveBtn = document.getElementById("resolveBtn");
  const queryStatus = document.getElementById("queryStatus");

  // Lightbox elements
  const lightbox = document.getElementById("imageLightbox");
  const lightboxImg = document.getElementById("lightboxImage");
  const lightboxCaption = document.getElementById("lightboxCaption");
  const lightboxClose = document.querySelector("[data-close-lightbox]");

  const markers = new Map();
  const knownResponses = new Set();
  const selectingResponses = new Set();
  const terminalStatuses = new Set(["resolved", "expired", "closed"]);
  let fetchingResponses = false;
  let resolving = false;
  let pollTimer;
  let locationPollTimer;
  let selectedResponseId;
  let liveProviderLocation;
  let responseSnapshot = [];
  let detailResponseId;

  const map = L.map(mapEl).setView([query.lat, query.lng], 13);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  const icon = (color, fresh = false) => L.divIcon({
    className: `nf-marker ${color} ${fresh ? "new" : ""}`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
  const seekerMarker = L.marker([query.lat, query.lng], { icon: icon("blue") }).addTo(map).bindPopup("Your request location");

  function openLightbox(src, caption) {
    if (!lightbox || !src) return;
    if (lightboxImg) lightboxImg.src = src;
    if (lightboxCaption) lightboxCaption.textContent = caption || "";
    if (typeof lightbox.showModal === "function") {
      lightbox.showModal();
    } else {
      lightbox.setAttribute("open", "");
    }
  }

  function closeLightbox() {
    if (!lightbox) return;
    if (typeof lightbox.close === "function") {
      lightbox.close();
    } else {
      lightbox.removeAttribute("open");
    }
  }

  lightboxClose?.addEventListener("click", closeLightbox);
  lightbox?.addEventListener("click", (event) => {
    if (event.target === lightbox) closeLightbox();
  });

  function addImage(slot, src, alt, caption) {
    if (!slot) return;
    slot.replaceChildren();
    if (src) {
      const img = document.createElement("img");
      img.src = src;
      img.alt = alt || "Product image";
      img.addEventListener("error", () => addImage(slot, "", alt, caption));
      slot.appendChild(img);

      const hint = document.createElement("span");
      hint.className = "zoom-hint";
      hint.textContent = "🔍 Enlarge";
      slot.appendChild(hint);

      const trigger = () => openLightbox(src, caption || alt);
      slot.onclick = trigger;
      slot.onkeydown = (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          trigger();
        }
      };
    } else {
      const placeholder = document.createElement("span");
      placeholder.className = "muted";
      placeholder.textContent = "No image";
      slot.appendChild(placeholder);
      slot.onclick = null;
      slot.onkeydown = null;
    }
  }

  function numericPrice(value) {
    if (value === null || value === undefined || value === "") return null;
    const numeric = Number(String(value).replace(/[^\d.-]/g, ""));
    return Number.isFinite(numeric) ? numeric : null;
  }

  function formatPrice(value) {
    const numeric = numericPrice(value);
    if (numeric === null) return "Price not provided";
    return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 }).format(numeric);
  }

  function formatDistance(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "Distance unavailable";
    const distance = Number(value);
    if (distance < 0) return "Distance unavailable";
    const label = distance < 0.1 ? `${Math.round(distance * 1000)} m` : `${distance.toFixed(2)} km`;
    return `${label} straight-line`;
  }

  function formatResponseTime(value) {
    if (!value) return "Response time unavailable";
    const timestamp = new Date(value).getTime();
    if (!Number.isFinite(timestamp)) return "Response time unavailable";
    const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60000));
    if (minutes < 1) return "Just now";
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
    const days = Math.floor(hours / 24);
    return `${days} day${days === 1 ? "" : "s"} ago`;
  }

  function statusLabel(status) {
    return { available: "Available", selected: "Selected", rejected: "Not selected" }[status] || "Status unavailable";
  }

  function sortedResponses(responses) {
    const sort = responseSort?.value || "best";
    return [...responses].sort((left, right) => {
      const availability = Number(right.status === "available") - Number(left.status === "available");
      if (sort === "best" && availability) return availability;
      if (sort === "similarity") {
        const leftSim = left.similarity && Number.isFinite(Number(left.similarity.score)) ? Number(left.similarity.score) : -1;
        const rightSim = right.similarity && Number.isFinite(Number(right.similarity.score)) ? Number(right.similarity.score) : -1;
        if (leftSim !== rightSim) return rightSim - leftSim;
      }
      const leftDistance = left.distance === null ? Infinity : Number(left.distance);
      const rightDistance = right.distance === null ? Infinity : Number(right.distance);
      const leftPrice = numericPrice(left.price) ?? Infinity;
      const rightPrice = numericPrice(right.price) ?? Infinity;
      if (sort === "price" && leftPrice !== rightPrice) return leftPrice - rightPrice;
      if (sort === "distance" && leftDistance !== rightDistance) return leftDistance - rightDistance;
      if (sort === "time") return new Date(left.created_at || 0) - new Date(right.created_at || 0);
      if (leftDistance !== rightDistance) return leftDistance - rightDistance;
      if (leftPrice !== rightPrice) return leftPrice - rightPrice;
      return new Date(left.created_at || 0) - new Date(right.created_at || 0);
    });
  }

  function filteredResponses(responses) {
    const filter = responseFilter?.value || "all";
    if (filter === "high_match") return responses.filter((response) => (response.similarity?.score ?? 0) >= 80);
    if (filter === "price") return responses.filter((response) => numericPrice(response.price) !== null);
    if (filter === "image") return responses.filter((response) => Boolean(response.image_url));
    if (filter === "nearby") return responses.filter((response) => response.distance !== null && Number(response.distance) <= 2);
    return responses;
  }

  function actionUrls(response) {
    const phone = window.NearFindPhoneUrl?.(response.phone) || "";
    const provider = response.business_name || response.provider_name || "provider";
    const message = encodeURIComponent(`Hi ${provider}, I found your NearFind response for ${query.item_name}. Is it still available?`);
    const directions = response.status === "selected"
      ? (window.NearFindNavigationUrl?.(response.provider_lat, response.provider_lng, query.lat, query.lng) || "")
      : "";
    return {
      call: phone,
      whatsapp: phone ? `https://wa.me/${phone.replace(/^tel:/, "").replace(/\D/g, "")}?text=${message}` : "",
      directions,
    };
  }

  function providerLocationState(location) {
    if (!location) return { state: "unavailable", message: "Location unavailable. Sharing may be off." };
    if (!Number.isFinite(Number(location.lat)) || !Number.isFinite(Number(location.lng))) {
      return { state: "unavailable", message: "Coordinates unavailable." };
    }
    const updated = new Date(location.updated_at).getTime();
    if (!Number.isFinite(updated)) return { state: "stale", message: "Location may be outdated." };
    const ageMinutes = Math.max(0, Math.floor((Date.now() - updated) / 60000));
    if (ageMinutes > 15) return { state: "stale", message: "Location may be outdated." };
    if (ageMinutes < 1) return { state: "available", message: "Location available · updated recently." };
    return { state: "available", message: `Location available · updated ${ageMinutes} minute${ageMinutes === 1 ? "" : "s"} ago.` };
  }

  function setProviderLocationMessage(location) {
    const result = providerLocationState(location);
    const card = responsesList.querySelector(`[data-response-id="${selectedResponseId}"]`);
    const cardStatus = card?.querySelector("[data-location-status]");
    if (cardStatus) {
      cardStatus.hidden = false;
      cardStatus.className = `notice location-status ${result.state}`;
      cardStatus.textContent = result.message;
    }
    const detailStatus = details?.querySelector("[data-detail-location]");
    if (detailStatus && selectedResponseId) {
      detailStatus.className = `notice location-status ${result.state}`;
      detailStatus.textContent = result.message;
    }
    if (location && Number.isFinite(Number(location.lat)) && Number.isFinite(Number(location.lng))) {
      const liveNavUrl = window.NearFindNavigationUrl?.(location.lat, location.lng, query.lat, query.lng) || "";
      const cardNav = card?.querySelector("[data-navigation]") || card?.querySelector("[data-directions]");
      if (cardNav && liveNavUrl) {
        cardNav.href = liveNavUrl;
        cardNav.classList.remove("disabled");
        cardNav.removeAttribute("aria-disabled");
        cardNav.title = "Open road directions in Google Maps";
      }
      const detailNav = details?.querySelector("[data-detail-navigation]") || details?.querySelector("[data-detail-directions]");
      if (detailNav && liveNavUrl) {
        detailNav.href = liveNavUrl;
        detailNav.classList.remove("disabled");
        detailNav.removeAttribute("aria-disabled");
        detailNav.title = "Open road directions in Google Maps";
      }
    }
  }

  function responsePopup(response) {
    const popup = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = response.business_name || response.provider_name || "Provider";
    const meta = document.createElement("p");
    meta.textContent = `${formatDistance(response.distance)} - ${statusLabel(response.status)}`;
    const message = document.createElement("p");
    message.textContent = response.message || "No message provided";
    const price = document.createElement("strong");
    price.textContent = formatPrice(response.price);
    popup.append(title, meta, message, price);
    if (response.image_url) {
      const image = document.createElement("img");
      image.src = response.image_url;
      image.alt = "Provider product";
      image.className = "popup-img";
      popup.appendChild(image);
    }
    return popup;
  }

  function syncResolveButton() {
    if (!resolveBtn) return;
    resolveBtn.hidden = query.status !== "matched";
    resolveBtn.disabled = query.status !== "matched" || resolving;
  }

  function stopPollingIfTerminal() {
    if (terminalStatuses.has(query.status) && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = undefined;
    }
    if (terminalStatuses.has(query.status) && locationPollTimer) {
      clearInterval(locationPollTimer);
      locationPollTimer = undefined;
    }
  }

  async function fetchSelectedProviderLocation() {
    if (!selectedResponseId || terminalStatuses.has(query.status) || document.hidden) return;
    try {
      const { payload } = await window.NearFindFetch(`/query/${query.id}/provider-location`, { cache: "no-store" });
      liveProviderLocation = payload.data;
      const marker = markers.get(selectedResponseId);
      if (!liveProviderLocation) {
        marker?.remove();
        markers.delete(selectedResponseId);
        setProviderLocationMessage(null);
        return;
      }
      if (marker) {
        marker.setLatLng([liveProviderLocation.lat, liveProviderLocation.lng]);
      }
      setProviderLocationMessage(liveProviderLocation);
    } catch (error) {
      setProviderLocationMessage(null);
    }
  }

  function showListMessage(message, kind = "empty") {
    responsesList.replaceChildren();
    const item = document.createElement("div");
    item.className = kind;
    item.textContent = message;
    responsesList.appendChild(item);
  }

  async function selectProvider(responseId, button) {
    if (!confirm("Choose this provider and mark other available responses as rejected?")) return;
    if (selectingResponses.has(responseId)) return;
    selectingResponses.add(responseId);
    const originalText = button?.textContent;
    if (button) {
      button.disabled = true;
      button.textContent = "Selecting...";
    }
    try {
      const { payload } = await window.NearFindFetch("/query/select-provider", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": window.NearFindCSRFToken || "" },
        body: JSON.stringify({ query_id: query.id, response_id: responseId }),
      });
      if (payload.success) {
        window.NearFindToast("Provider selected.");
        await fetchResponses({ force: true });
      } else {
        window.NearFindToast(window.NearFindErrorMessage(payload, "Could not select provider."), "error");
      }
    } catch (error) {
      window.NearFindToast("Could not select provider. Please try again.", "error");
    } finally {
      selectingResponses.delete(responseId);
      if (button) {
        button.disabled = false;
        button.textContent = originalText || "Select Provider";
      }
    }
  }

  function renderResponse(response) {
    const node = template.content.firstElementChild.cloneNode(true);
    const providerName = response.provider_name || "Provider";
    const metaParts = [];
    if (response.business_name) metaParts.push(response.business_name);
    metaParts.push(response.provider_type || "provider");
    metaParts.push(formatDistance(response.distance));
    node.querySelector("[data-name]").textContent = providerName;
    node.querySelector("[data-meta]").textContent = metaParts.join(" - ");

    const chip = node.querySelector("[data-status]");
    chip.textContent = statusLabel(response.status);
    chip.className = `status-chip ${response.status}`;

    if (response.status === "selected") {
      node.classList.add("selected");
    }

    node.querySelector("[data-message]").textContent = response.message || "";
    node.querySelector("[data-price]").textContent = formatPrice(response.price);
    node.querySelector("[data-distance]").textContent = formatDistance(response.distance);
    node.querySelector("[data-time]").textContent = formatResponseTime(response.created_at);

    addImage(node.querySelector("[data-reference]"), query.image_url, "Your reference", "Your reference product image");
    addImage(node.querySelector("[data-product]"), response.image_url, "Provider product", `${providerName}'s product image`);

    const similarityBox = node.querySelector("[data-similarity-box]");
    if (similarityBox) {
      if (response.similarity && response.similarity.score !== null && response.similarity.score !== undefined) {
        similarityBox.hidden = false;
        const scoreEl = node.querySelector("[data-similarity-score]");
        const levelEl = node.querySelector("[data-similarity-level]");
        const disclaimerEl = node.querySelector("[data-similarity-disclaimer]");
        if (scoreEl) {
          scoreEl.textContent = `Estimated Visual Similarity: ${response.similarity.score}%`;
        }
        if (levelEl) {
          levelEl.textContent = response.similarity.level || "";
          levelEl.className = `similarity-level match-${(response.similarity.level || "").toLowerCase().replace(/\s+/g, "-")}`;
        }
        if (disclaimerEl) {
          disclaimerEl.textContent = response.similarity.disclaimer || "Similarity score is an automated visual estimate and does not guarantee that products are identical or authentic.";
        }
      } else {
        similarityBox.hidden = true;
      }
    }

    const urls = actionUrls(response);
    const select = node.querySelector("[data-select]");
    select.disabled = response.status !== "available" || query.status !== "open";
    select.textContent = response.status === "selected" ? "Provider Selected" : "Select Provider";
    select.addEventListener("click", () => selectProvider(response.id, select));

    const call = node.querySelector("[data-call]");
    const whatsapp = node.querySelector("[data-whatsapp]");
    const navigation = node.querySelector("[data-navigation]") || node.querySelector("[data-directions]");
    const viewOnMap = node.querySelector("[data-view-on-map]");
    const chat = node.querySelector("[data-chat]");
    const locationStatus = node.querySelector("[data-location-status]");
    const detailsButton = node.querySelector("[data-details]");

    if (urls.call) {
      call.href = urls.call;
      call.classList.remove("disabled");
      call.removeAttribute("aria-disabled");
    } else {
      call.href = "#";
      call.classList.add("disabled");
      call.setAttribute("aria-disabled", "true");
    }

    if (urls.whatsapp) {
      whatsapp.href = urls.whatsapp;
      whatsapp.classList.remove("disabled");
      whatsapp.removeAttribute("aria-disabled");
    } else {
      whatsapp.href = "#";
      whatsapp.classList.add("disabled");
      whatsapp.setAttribute("aria-disabled", "true");
    }

    if (response.status === "selected") {
      node.dataset.responseId = response.id;
      chat.href = `/chat/response/${response.id}`;
      chat.hidden = false;

      const navCheck = window.NearFindCheckNavigationEndpoints?.(response.provider_lat, response.provider_lng, query.lat, query.lng);
      navigation.hidden = false;
      if (urls.directions && navCheck?.canNavigate) {
        navigation.href = urls.directions;
        navigation.classList.remove("disabled");
        navigation.removeAttribute("aria-disabled");
        navigation.title = "Open road route in external navigation (Google Maps)";
      } else {
        navigation.href = "#";
        navigation.classList.add("disabled");
        navigation.setAttribute("aria-disabled", "true");
        navigation.title = navCheck?.message || "Provider location is not yet available for navigation.";
      }
      navigation.addEventListener("click", (e) => {
        if (!urls.directions || !navCheck?.canNavigate || navigation.getAttribute("href") === "#") {
          e.preventDefault();
          const errMessage = navCheck?.message || "Provider location coordinates are unavailable.";
          window.NearFindToast(errMessage, "error");
        }
      });

      locationStatus.hidden = false;
      if (navCheck?.canNavigate) {
        locationStatus.className = "notice location-status available";
        locationStatus.textContent = "Provider selected. Road route navigation is ready.";
      } else {
        locationStatus.className = "notice location-status unavailable";
        locationStatus.textContent = "Waiting for shared provider location. Road navigation will activate when coordinates are available.";
      }
    } else {
      navigation.hidden = true;
      if (response.status === "rejected") {
        locationStatus.hidden = false;
        locationStatus.className = "notice";
        locationStatus.textContent = "This provider was not selected.";
      }
    }

    const hasCoords = Number.isFinite(Number(response.provider_lat)) && Number.isFinite(Number(response.provider_lng));
    if (viewOnMap) {
      if (hasCoords) {
        viewOnMap.disabled = false;
        viewOnMap.title = "Center and view marker on map";
        viewOnMap.addEventListener("click", () => {
          map.flyTo([response.provider_lat, response.provider_lng], 15, { duration: 0.8 });
          markers.get(response.id)?.openPopup();
          if (window.innerWidth <= 960) {
            mapEl.scrollIntoView({ behavior: "smooth", block: "center" });
          }
        });
      } else {
        viewOnMap.disabled = true;
        viewOnMap.title = "Coordinates not available on map";
      }
    }

    detailsButton.addEventListener("click", () => showDetails(response));
    return node;
  }

  function updateMarkers(responses) {
    const liveIds = new Set(responses.map((response) => response.id));
    markers.forEach((marker, id) => {
      if (!liveIds.has(id)) {
        marker.remove();
        markers.delete(id);
      }
    });
    responses.forEach((response) => {
      if (response.provider_lat === null || response.provider_lng === null || response.status === "rejected") {
        markers.get(response.id)?.remove();
        markers.delete(response.id);
        return;
      }
      const fresh = !knownResponses.has(response.id);
      if (markers.has(response.id)) {
        markers.get(response.id).setLatLng([response.provider_lat, response.provider_lng]).bindPopup(responsePopup(response));
      } else {
        markers.set(response.id, L.marker([response.provider_lat, response.provider_lng], { icon: icon("green", fresh) }).addTo(map).bindPopup(responsePopup(response)));
      }
      if (fresh && knownResponses.size) window.NearFindToast("Provider response received.");
      knownResponses.add(response.id);
    });
    if (responses.length) {
      const group = L.featureGroup([seekerMarker, ...markers.values()]);
      map.fitBounds(group.getBounds().pad(0.2), { maxZoom: 14 });
    }
  }

  function renderResponses(responses) {
    responseSnapshot = responses;
    responsesList.replaceChildren();
    const selected = responses.find((response) => response.status === "selected");
    selectedResponseId = selected?.id;
    liveProviderLocation = null;
    const responseLabel = responses.length === 1 ? "1 provider found" : `${responses.length} providers found`;
    providerCount.textContent = responses.length === 0
      ? "Waiting for responses..."
      : selected ? responseLabel : `${responseLabel} - No provider selected yet`;
    if (responses.length === 0) {
      showListMessage("No providers have responded yet. Keep this page open. We'll show new responses when they arrive.");
      updateMarkers(responses);
      syncResolveButton();
      map.invalidateSize();
      return;
    }
    const visibleResponses = filteredResponses(sortedResponses(responses));
    if (!visibleResponses.length) {
      showListMessage("No responses match this filter.");
    } else {
      visibleResponses.forEach((response) => responsesList.appendChild(renderResponse(response)));
    }
    updateMarkers(responses);
    fetchSelectedProviderLocation();
    syncResolveButton();
    map.invalidateSize();
  }

  function showDetails(response) {
    if (!details) return;
    detailResponseId = response.id;
    details.querySelector("[data-detail-name]").textContent = response.provider_name || "Provider";
    details.querySelector("[data-detail-meta]").textContent = [response.business_name, response.provider_type].filter(Boolean).join(" - ") || "Provider information";
    details.querySelector("[data-detail-message]").textContent = response.message || "No response message provided.";
    details.querySelector("[data-detail-price]").textContent = formatPrice(response.price);
    details.querySelector("[data-detail-distance]").textContent = formatDistance(response.distance);
    details.querySelector("[data-detail-time]").textContent = formatResponseTime(response.created_at);
    addImage(details.querySelector("[data-detail-image]"), response.image_url, "Provider product", `${response.provider_name || "Provider"} Product`);

    const detailSimBox = details.querySelector("[data-detail-similarity-box]");
    if (detailSimBox) {
      if (response.similarity && response.similarity.score !== null && response.similarity.score !== undefined) {
        detailSimBox.hidden = false;
        const detailScore = details.querySelector("[data-detail-similarity-score]");
        const detailLevel = details.querySelector("[data-detail-similarity-level]");
        const detailDisclaimer = details.querySelector("[data-detail-similarity-disclaimer]");
        if (detailScore) {
          detailScore.textContent = `Estimated Visual Similarity: ${response.similarity.score}%`;
        }
        if (detailLevel) {
          detailLevel.textContent = response.similarity.level || "";
          detailLevel.className = `similarity-level match-${(response.similarity.level || "").toLowerCase().replace(/\s+/g, "-")}`;
        }
        if (detailDisclaimer) {
          detailDisclaimer.textContent = response.similarity.disclaimer || "Similarity score is an automated visual estimate and does not guarantee that products are identical or authentic.";
        }
      } else {
        detailSimBox.hidden = true;
      }
    }

    const select = details.querySelector("[data-detail-select]");
    select.hidden = response.status !== "available" || query.status !== "open";
    select.disabled = false;
    select.textContent = "Select Provider";
    select.onclick = () => selectProvider(response.id, select);

    const chat = details.querySelector("[data-detail-chat]");
    chat.hidden = response.status !== "selected";
    chat.href = `/chat/response/${response.id}`;

    const navigation = details.querySelector("[data-detail-navigation]") || details.querySelector("[data-detail-directions]");
    const call = details.querySelector("[data-detail-call]");
    const viewMap = details.querySelector("[data-detail-view-map]");
    const locationStatus = details.querySelector("[data-detail-location]");
    const urls = actionUrls(response);

    if (response.status === "selected") {
      const navCheck = window.NearFindCheckNavigationEndpoints?.(response.provider_lat, response.provider_lng, query.lat, query.lng);
      navigation.hidden = false;
      if (urls.directions && navCheck?.canNavigate) {
        navigation.href = urls.directions;
        navigation.classList.remove("disabled");
        navigation.removeAttribute("aria-disabled");
        navigation.title = "Open road route in external navigation (Google Maps)";
      } else {
        navigation.href = "#";
        navigation.classList.add("disabled");
        navigation.setAttribute("aria-disabled", "true");
        navigation.title = navCheck?.message || "Provider location is not yet available for navigation.";
      }
      navigation.onclick = (e) => {
        if (!urls.directions || !navCheck?.canNavigate || navigation.getAttribute("href") === "#") {
          e.preventDefault();
          const errMessage = navCheck?.message || "Provider location coordinates are unavailable.";
          window.NearFindToast(errMessage, "error");
        }
      };
      locationStatus.className = navCheck?.canNavigate ? "notice location-status available" : "notice location-status unavailable";
      locationStatus.textContent = navCheck?.canNavigate
        ? "Provider selected. Road route navigation is ready."
        : "Waiting for shared provider location. Road navigation will activate when coordinates are available.";
    } else {
      navigation.hidden = true;
      locationStatus.className = "notice location-status unavailable";
      locationStatus.textContent = "Provider location and navigation are shown only after selection.";
    }

    if (urls.call) {
      call.hidden = false;
      call.href = urls.call;
    } else {
      call.hidden = true;
    }

    const hasCoords = Number.isFinite(Number(response.provider_lat)) && Number.isFinite(Number(response.provider_lng));
    if (viewMap) {
      if (hasCoords) {
        viewMap.disabled = false;
        viewMap.onclick = () => {
          if (typeof details.close === "function") details.close();
          map.flyTo([response.provider_lat, response.provider_lng], 15, { duration: 0.8 });
          markers.get(response.id)?.openPopup();
          if (window.innerWidth <= 960) {
            mapEl.scrollIntoView({ behavior: "smooth", block: "center" });
          }
        };
      } else {
        viewMap.disabled = true;
      }
    }

    if (typeof details.showModal === "function") details.showModal();
  }

  details?.querySelector("[data-close-details]").addEventListener("click", () => details.close());
  details?.addEventListener("click", (event) => {
    if (event.target === details) details.close();
  });

  async function fetchResponses(options = {}) {
    if (document.hidden || fetchingResponses && !options.force) return;
    fetchingResponses = true;
    if (!knownResponses.size) providerCount.textContent = "Loading responses...";
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000);
    try {
      const { payload } = await window.NearFindFetch(`/query/responses/${query.id}`, { cache: "no-store" });
      query = payload.data.query || query;
      if (queryStatus) {
        queryStatus.textContent = query.status;
        queryStatus.className = `status-chip ${query.status}`;
      }
      stopPollingIfTerminal();
      if (!payload.data || !Array.isArray(payload.data.responses)) {
        throw new Error("Response endpoint returned an invalid payload");
      }
      renderResponses(payload.data.responses);
    } catch (error) {
      console.error("NearFind response fetch failed", error);
      window.NearFindToast(error.message || "Could not load responses. Check your connection and try again.", "error");
      providerCount.textContent = "Could not load responses";
      showListMessage(error.message || "Could not load responses. Check your connection and try again.", "empty");
    } finally {
      clearTimeout(timeoutId);
      fetchingResponses = false;
    }
  }

  if (resolveBtn) {
    resolveBtn.addEventListener("click", async () => {
      if (!confirm("Mark this request resolved?")) return;
      if (resolving) return;
      resolving = true;
      const originalText = resolveBtn.textContent;
      resolveBtn.disabled = true;
      resolveBtn.textContent = "Resolving...";
      try {
        const { payload } = await window.NearFindFetch("/query/resolve", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": window.NearFindCSRFToken || "" },
          body: JSON.stringify({ query_id: query.id }),
        });
        if (payload.success) {
          window.NearFindToast("Request resolved.");
          query.status = "resolved";
          if (queryStatus) {
            queryStatus.textContent = "resolved";
            queryStatus.className = "status-chip resolved";
          }
          stopPollingIfTerminal();
          syncResolveButton();
        } else {
          window.NearFindToast(window.NearFindErrorMessage(payload, "Could not resolve request."), "error");
        }
      } catch (error) {
        window.NearFindToast("Could not resolve request. Please try again.", "error");
      } finally {
        resolving = false;
        resolveBtn.disabled = false;
        resolveBtn.textContent = originalText;
      }
    });
  }

  // Ensure map reflow on resize and container size adjustments
  window.addEventListener("resize", () => {
    map.invalidateSize();
  });
  if ("ResizeObserver" in window) {
    const resizeObserver = new ResizeObserver(() => {
      map.invalidateSize();
    });
    resizeObserver.observe(mapEl);
  }
  setTimeout(() => map.invalidateSize(), 150);
  setTimeout(() => map.invalidateSize(), 600);

  syncResolveButton();
  fetchResponses();
  pollTimer = setInterval(() => {
    if (terminalStatuses.has(query.status)) {
      stopPollingIfTerminal();
      return;
    }
    fetchResponses();
  }, 3000);
  locationPollTimer = setInterval(fetchSelectedProviderLocation, 5000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      fetchResponses({ force: true });
      fetchSelectedProviderLocation();
    }
  });
  [responseSort, responseFilter].forEach((control) => control?.addEventListener("change", () => renderResponses(responseSnapshot)));
  window.addEventListener("pagehide", () => {
    if (pollTimer) clearInterval(pollTimer);
    if (locationPollTimer) clearInterval(locationPollTimer);
    pollTimer = undefined;
  }, { once: true });
})();
