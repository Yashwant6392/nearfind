(function () {
  const shell = document.querySelector("[data-conversation-id]");
  if (!shell) return;
  const conversationId = shell.dataset.conversationId;
  const currentUser = shell.dataset.currentUser;
  const messagesEl = document.getElementById("chatMessages");
  const errorEl = document.getElementById("chatError");
  const form = document.getElementById("chatForm");
  const input = document.getElementById("chatInput");
  const send = document.getElementById("chatSend");
  const originalSendText = send.textContent;
  const role = shell.dataset.role;
  let queryStatus = shell.dataset.queryStatus;
  const enableLocation = document.getElementById("enableLocationSharing");
  const stopLocation = document.getElementById("stopLocationSharing");
  const locationStatus = document.getElementById("locationShareStatus");
  let loading = false;
  let sending = false;
  let timer;
  let locationWatch;
  let sharing = false;
  let lastSentAt = 0;
  let lastPosition;

  function setLocationStatus(message, enabled = sharing) {
    if (locationStatus) locationStatus.textContent = message;
    if (enableLocation) enableLocation.hidden = enabled;
    if (stopLocation) stopLocation.hidden = !enabled;
  }

  async function sendLocation(position) {
    if (document.hidden || !sharing) return;
    const now = Date.now();
    const next = { lat: position.coords.latitude, lng: position.coords.longitude, accuracy_m: position.coords.accuracy };
    const moved = !lastPosition || Math.abs(next.lat - lastPosition.lat) > 0.00005 || Math.abs(next.lng - lastPosition.lng) > 0.00005;
    if (now - lastSentAt < 10000 && !moved) return;
    const { payload } = await window.NearFindFetch("/location/update", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": window.NearFindCSRFToken || "" },
      body: JSON.stringify({ ...next, sharing_enabled: true }),
    });
    lastSentAt = now;
    lastPosition = next;
    setLocationStatus("Location Sharing: ON · Last updated: just now", true);
  }

  function stopLocationSharing() {
    if (!sharing && !locationWatch) return;
    if (locationWatch !== undefined) navigator.geolocation?.clearWatch(locationWatch);
    locationWatch = undefined;
    sharing = false;
    setLocationStatus("Location sharing is off.", false);
    window.NearFindFetch("/location/stop", {
      method: "POST",
      headers: { "X-CSRFToken": window.NearFindCSRFToken || "" },
      keepalive: true,
    }).catch(() => {});
  }

  function startLocationSharing() {
    if (role !== "provider" || sharing || !navigator.geolocation) {
      if (!navigator.geolocation) setLocationStatus("Location is unavailable in this browser.", false);
      return;
    }
    sharing = true;
    setLocationStatus("Requesting location permission...", true);
    locationWatch = navigator.geolocation.watchPosition(
      (position) => sendLocation(position).catch(() => setLocationStatus("Unable to update location.", true)),
      (error) => {
        sharing = false;
        if (locationWatch !== undefined) navigator.geolocation.clearWatch(locationWatch);
        locationWatch = undefined;
        setLocationStatus(error.code === 1 ? "Location permission denied." : "Location unavailable.", false);
      },
      { enableHighAccuracy: true, maximumAge: 10000, timeout: 15000 }
    );
  }

  function render(messages) {
    messagesEl.replaceChildren();
    if (!messages.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No messages yet. Start the conversation.";
      messagesEl.appendChild(empty);
      return;
    }
    messages.forEach((message) => {
      const bubble = document.createElement("article");
      bubble.className = `chat-bubble ${message.sender_id === currentUser ? "sent" : "received"}`;
      const text = document.createElement("p");
      text.textContent = message.message;
      const time = document.createElement("time");
      time.textContent = message.created_at ? new Date(message.created_at).toLocaleString() : "";
      bubble.append(text, time);
      messagesEl.appendChild(bubble);
    });
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  async function loadMessages() {
    if (loading) return;
    loading = true;
    try {
      const { payload } = await window.NearFindFetch(`/chat/${conversationId}/messages`, { cache: "no-store" });
      queryStatus = payload.data.query_status || queryStatus;
      if (queryStatus === "resolved") stopLocationSharing();
      errorEl.hidden = true;
      render(payload.data.messages || []);
      await window.NearFindFetch(`/chat/${conversationId}/read`, {
        method: "POST",
        headers: { "X-CSRFToken": window.NearFindCSRFToken || "" },
      });
    } catch (error) {
      errorEl.hidden = false;
      errorEl.textContent = error.message || "Unable to load messages. Please try again.";
    } finally {
      loading = false;
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message || sending) return;
    sending = true;
    send.disabled = true;
    send.textContent = "Please wait...";
    try {
      await window.NearFindFetch(`/chat/${conversationId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": window.NearFindCSRFToken || "" },
        body: JSON.stringify({ message }),
      });
      input.value = "";
      await loadMessages();
    } catch (error) {
      errorEl.hidden = false;
      errorEl.textContent = error.message || "Unable to send message.";
    } finally {
      sending = false;
      send.disabled = false;
      send.textContent = originalSendText;
      form.dataset.submitting = "false";
      input.focus();
    }
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  enableLocation?.addEventListener("click", startLocationSharing);
  stopLocation?.addEventListener("click", stopLocationSharing);

  const poll = () => {
    if (!document.hidden) loadMessages();
  };
  loadMessages();
  timer = setInterval(poll, 4000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadMessages();
  });
  window.addEventListener("pagehide", () => {
    clearInterval(timer);
    stopLocationSharing();
  }, { once: true });
})();
