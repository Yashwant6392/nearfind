(function () {
  const mapEl = document.getElementById("seekerMap");
  if (!mapEl) return;

  let query = window.NEARFIND_QUERY;
  const responsesList = document.getElementById("responsesList");
  const providerCount = document.getElementById("providerCount");
  const template = document.getElementById("responseTemplate");
  const resolveBtn = document.getElementById("resolveBtn");
  const queryStatus = document.getElementById("queryStatus");
  const markers = new Map();
  const knownResponses = new Set();
  const selectingResponses = new Set();
  const terminalStatuses = new Set(["resolved", "expired", "closed"]);
  let fetchingResponses = false;
  let resolving = false;
  let pollTimer;

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

  function addImage(slot, src, alt) {
    slot.replaceChildren();
    if (src) {
      const img = document.createElement("img");
      img.src = src;
      img.alt = alt;
      slot.appendChild(img);
    } else {
      const placeholder = document.createElement("span");
      placeholder.textContent = "No image";
      slot.appendChild(placeholder);
    }
  }

  function actionUrls(response) {
    const phone = response.phone || "";
    const provider = response.business_name || response.provider_name || "provider";
    const message = encodeURIComponent(`Hi ${provider}, I found your NearFind response for ${query.item_name}. Is it still available?`);
    const latLng = `${response.provider_lat},${response.provider_lng}`;
    return {
      call: phone ? `tel:${phone}` : "",
      whatsapp: phone ? `https://wa.me/${phone.replace(/\D/g, "")}?text=${message}` : "",
      directions: response.provider_lat && response.provider_lng ? `https://www.openstreetmap.org/directions?to=${latLng}` : "",
    };
  }

  function responsePopup(response) {
    const popup = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = response.business_name || response.provider_name || "Provider";
    const meta = document.createElement("p");
    meta.textContent = `${response.distance ?? "?"} km away - ${response.status}`;
    const message = document.createElement("p");
    message.textContent = response.message || "No message provided";
    const price = document.createElement("strong");
    price.textContent = response.price || "Price not shared";
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
      const res = await fetch("/query/select-provider", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": window.NearFindCSRFToken || "" },
        body: JSON.stringify({ query_id: query.id, response_id: responseId }),
      });
      const payload = await res.json();
      if (payload.success) {
        window.NearFindToast("Provider selected.");
        await fetchResponses({ force: true });
      } else {
        window.NearFindToast(payload.error || "Could not select provider.", "error");
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
    metaParts.push(`${response.distance ?? "?"} km away`);
    node.querySelector("[data-name]").textContent = providerName;
    node.querySelector("[data-meta]").textContent = metaParts.join(" - ");
    const chip = node.querySelector("[data-status]");
    chip.textContent = response.status === "selected" ? "Provider Selected" : response.status;
    chip.classList.add(response.status);
    node.querySelector("[data-message]").textContent = response.message || "";
    node.querySelector("[data-price]").textContent = response.price || "Price not shared";
    addImage(node.querySelector("[data-reference]"), query.image_url, "Your reference");
    addImage(node.querySelector("[data-product]"), response.image_url, "Provider product");
    const urls = actionUrls(response);
    const select = node.querySelector("[data-select]");
    select.disabled = response.status !== "available" || query.status !== "open";
    select.textContent = response.status === "selected" ? "Provider Selected" : "Select Provider";
    select.addEventListener("click", () => selectProvider(response.id, select));
    const call = node.querySelector("[data-call]");
    const whatsapp = node.querySelector("[data-whatsapp]");
    const directions = node.querySelector("[data-directions]");
    const chat = node.querySelector("[data-chat]");
    call.href = urls.call || "#";
    whatsapp.href = urls.whatsapp || "#";
    directions.href = urls.directions || "#";
    if (response.status === "selected") {
      chat.href = `/chat/response/${response.id}`;
      chat.hidden = false;
    }
    [call, whatsapp, directions].forEach((link) => {
      if (link.getAttribute("href") === "#") link.classList.add("disabled");
    });
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
    responsesList.replaceChildren();
    providerCount.textContent = responses.length === 0
      ? "Waiting for responses..."
      : responses.length === 1 ? "1 provider found" : `${responses.length} providers found`;
    if (responses.length === 0) {
      showListMessage("No provider responses yet. This page updates automatically.");
      updateMarkers(responses);
      syncResolveButton();
      return;
    }
    responses.forEach((response) => responsesList.appendChild(renderResponse(response)));
    updateMarkers(responses);
    syncResolveButton();
  }

  async function fetchResponses(options = {}) {
    if (fetchingResponses && !options.force) return;
    fetchingResponses = true;
    if (!knownResponses.size) providerCount.textContent = "Loading responses...";
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000);
    try {
      const res = await fetch(`/query/responses/${query.id}`, { cache: "no-store", signal: controller.signal });
      let payload;
      try {
        payload = await res.json();
      } catch (error) {
        throw new Error(`Response endpoint returned HTTP ${res.status}`);
      }
      if (!res.ok) {
        const message = payload.error || `Response endpoint returned HTTP ${res.status}`;
        console.error("NearFind response API error", { status: res.status, error: payload.error });
        window.NearFindToast(message, "error");
        providerCount.textContent = "Could not load responses";
        showListMessage(message, "empty");
        return;
      }
      if (!payload.success) {
        window.NearFindToast(payload.error || "Could not load responses.", "error");
        providerCount.textContent = "Could not load responses";
        showListMessage(payload.error || "Could not load responses. Please try again.", "empty");
        return;
      }
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
      window.NearFindToast("Could not load responses. Check your connection and try again.", "error");
      providerCount.textContent = "Could not load responses";
      showListMessage("Could not load responses. Check your connection and try again.", "empty");
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
        const res = await fetch("/query/resolve", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": window.NearFindCSRFToken || "" },
          body: JSON.stringify({ query_id: query.id }),
        });
        const payload = await res.json();
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
          window.NearFindToast(payload.error || "Could not resolve request.", "error");
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

  syncResolveButton();
  fetchResponses();
  pollTimer = setInterval(() => {
    if (terminalStatuses.has(query.status)) {
      stopPollingIfTerminal();
      return;
    }
    fetchResponses();
  }, 3000);
  window.addEventListener("pagehide", () => {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = undefined;
  }, { once: true });
})();
