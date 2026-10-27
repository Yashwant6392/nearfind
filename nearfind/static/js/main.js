(function () {
  const toast = (message, kind = "success") => {
    const stack = document.querySelector(".flash-stack") || document.body.appendChild(document.createElement("section"));
    stack.className = "flash-stack";
    const item = document.createElement("div");
    item.className = `toast ${kind}`;
    item.textContent = message;
    stack.appendChild(item);
    setTimeout(() => item.remove(), 4200);
  };
  window.NearFindToast = toast;
  const csrfToken = document.querySelector("meta[name='csrf-token']")?.content || "";
  window.NearFindCSRFToken = csrfToken;
  const errorMessage = (payload, fallback = "Something went wrong. Please try again.") => {
    const error = payload?.error;
    if (typeof error === "string") return error;
    return error?.message || fallback;
  };
  window.NearFindErrorMessage = errorMessage;

  const fetchJson = async (url, options = {}, timeout = 15000) => {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url, { ...options, signal: controller.signal });
      if (response.status === 401) {
        sessionStorage.setItem("nearfind-session-message", "Your session has expired. Please log in again.");
        window.location.assign("/login");
        throw new Error("Your session has expired. Please log in again.");
      }
      let payload = null;
      const contentType = response.headers.get("content-type") || "";
      if (contentType.includes("application/json")) {
        try {
          payload = await response.json();
        } catch (_) {
          payload = null;
        }
      }
      if (!response.ok) {
        const failureMessage = (payload && errorMessage(payload)) || `Server returned error (${response.status}).`;
        const failure = new Error(failureMessage);
        failure.status = response.status;
        failure.payload = payload;
        throw failure;
      }
      if (!payload) {
        throw new Error("The server returned an invalid response.");
      }
      if (payload.success === false) {
        const failure = new Error(errorMessage(payload, `Request failed (${response.status}).`));
        failure.status = response.status;
        failure.payload = payload;
        throw failure;
      }
      return { response, payload };
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The request timed out. Please try again.");
      if (error instanceof TypeError) throw new Error("Network connection failed. Please check your connection.");
      throw error;
    } finally {
      clearTimeout(timeoutId);
    }
  };
  window.NearFindFetch = fetchJson;
  const sessionMessage = sessionStorage.getItem("nearfind-session-message");
  if (sessionMessage) {
    sessionStorage.removeItem("nearfind-session-message");
    toast(sessionMessage, "error");
  }

  const notificationMenu = document.querySelector("[data-notifications]");
  if (notificationMenu) {
    const toggle = document.getElementById("notificationToggle");
    const panel = document.getElementById("notificationPanel");
    const badge = document.getElementById("notificationBadge");
    const list = document.getElementById("notificationList");
    const desktopButton = document.getElementById("desktopNotifications");
    let notificationRequest;
    let knownNotificationIds;

    const notificationTarget = (notification) => {
      const userRole = document.body ? document.body.dataset.role : "";
      if (notification.type === "provider_response") return `/seeker/responses/${notification.query_id}`;
      if (notification.type === "new_request") return "/provider/dashboard";
      if (["chat_message", "provider_selected", "request_resolved"].includes(notification.type) && notification.response_id) {
        return `/chat/response/${notification.response_id}`;
      }
      return userRole === "provider" ? "/provider/dashboard" : "/seeker/dashboard";
    };

    const updateDesktopButton = () => {
      if (!desktopButton || !("Notification" in window)) return;
      desktopButton.hidden = Notification.permission !== "default";
    };

    const announceNewNotifications = (notifications) => {
      const currentIds = new Set(notifications.map((notification) => notification.id));
      if (!knownNotificationIds) {
        knownNotificationIds = currentIds;
        return;
      }
      if ("Notification" in window && Notification.permission === "granted") {
        notifications.filter((notification) => !knownNotificationIds.has(notification.id) && !notification.is_read)
          .forEach((notification) => new Notification(notification.title, { body: notification.message }));
      }
      knownNotificationIds = currentIds;
    };

    const renderNotifications = (notifications) => {
      list.replaceChildren();
      if (!notifications.length) {
        const empty = document.createElement("p");
        empty.className = "empty";
        empty.textContent = "No notifications yet.";
        list.appendChild(empty);
        return;
      }
      notifications.forEach((notification) => {
        const item = document.createElement("a");
        item.className = `notification-item${notification.is_read ? " read" : " unread"}`;
        item.href = notificationTarget(notification);
        item.dataset.notificationId = notification.id;
        const title = document.createElement("strong");
        title.textContent = notification.title;
        const message = document.createElement("span");
        message.textContent = notification.message;
        item.append(title, message);
        item.addEventListener("click", async (event) => {
          if (notification.is_read) return;
          event.preventDefault();
          try {
            await fetchJson(`/notifications/${notification.id}/read`, {
              method: "POST",
              headers: { "X-CSRFToken": csrfToken },
            });
          } finally {
            window.location.assign(item.href);
          }
        });
        list.appendChild(item);
      });
    };

    const loadNotifications = async () => {
      if (notificationRequest) return notificationRequest;
      notificationRequest = fetchJson("/notifications", { cache: "no-store" })
        .then(({ payload }) => {
          const notifications = payload.data.notifications || [];
          const unreadCount = payload.data.unread_count || 0;
          badge.textContent = unreadCount;
          badge.hidden = unreadCount === 0;
          announceNewNotifications(notifications);
          renderNotifications(notifications);
        })
        .catch((error) => {
          list.replaceChildren();
          const empty = document.createElement("p");
          empty.className = "empty";
          empty.textContent = error.message;
          list.appendChild(empty);
        })
        .finally(() => {
          notificationRequest = undefined;
        });
      return notificationRequest;
    };

    toggle.addEventListener("click", async () => {
      const open = !panel.hidden;
      panel.hidden = open;
      toggle.setAttribute("aria-expanded", String(!open));
      if (!open) await loadNotifications();
    });
    desktopButton?.addEventListener("click", async () => {
      if (!("Notification" in window) || Notification.permission !== "default") return;
      await Notification.requestPermission();
      updateDesktopButton();
    });
    document.addEventListener("click", (event) => {
      if (!notificationMenu.contains(event.target)) {
        panel.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        panel.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
      }
    });
    updateDesktopButton();
    loadNotifications();

    let userActive = true;
    let idlePollCount = 0;
    const MAX_IDLE_POLLS = 10;
    const markActive = () => {
      userActive = true;
      idlePollCount = 0;
    };
    ["mousemove", "keydown", "scroll", "touchstart", "click"].forEach((evt) => {
      window.addEventListener(evt, markActive, { passive: true });
    });

    const notificationTimer = setInterval(() => {
      if (document.hidden) return;
      if (!userActive) {
        idlePollCount++;
        if (idlePollCount >= MAX_IDLE_POLLS) return; // pause background polling while user is completely idle
      }
      userActive = false;
      loadNotifications();
    }, 30000);
    window.addEventListener("pagehide", () => clearInterval(notificationTimer), { once: true });
  }

  async function postLocation(lat, lng) {
    await window.NearFindFetch("/user/update-location", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
      body: JSON.stringify({ lat, lng }),
    });
  }

  document.querySelectorAll("[data-location-form]").forEach((form) => {
    const status = form.querySelector("#locationStatus") || document.getElementById("locationStatus");
    const latInput = form.querySelector("input[name='lat']");
    const lngInput = form.querySelector("input[name='lng']");
    const useLocationButton = form.querySelector("#useCurrentLocation");

    const setStatus = (msg, stateClass = "") => {
      if (!status) return;
      status.textContent = msg;
      status.classList.remove("location-ready", "location-denied", "location-error");
      if (stateClass) status.classList.add(stateClass);
    };

    const validateCoords = () => {
      if (!latInput || !lngInput) return true;
      const latVal = latInput.value.trim();
      const lngVal = lngInput.value.trim();
      if (!latVal && !lngVal) {
        latInput.setCustomValidity("");
        lngInput.setCustomValidity("");
        return true;
      }
      if (!latVal || !lngVal) {
        if (!latVal) latInput.setCustomValidity("Both latitude and longitude are required if coordinates are provided.");
        if (!lngVal) lngInput.setCustomValidity("Both latitude and longitude are required if coordinates are provided.");
        return false;
      }
      const latNum = parseFloat(latVal);
      const lngNum = parseFloat(lngVal);
      let valid = true;
      if (isNaN(latNum) || latNum < -90 || latNum > 90) {
        latInput.setCustomValidity("Latitude must be a valid number between -90 and 90.");
        valid = false;
      } else {
        latInput.setCustomValidity("");
      }
      if (isNaN(lngNum) || lngNum < -180 || lngNum > 180) {
        lngInput.setCustomValidity("Longitude must be a valid number between -180 and 180.");
        valid = false;
      } else {
        lngInput.setCustomValidity("");
      }
      return valid;
    };

    if (latInput) latInput.addEventListener("input", validateCoords);
    if (lngInput) lngInput.addEventListener("input", validateCoords);

    // If coordinates are already prefilled (e.g. from session), validate and indicate ready state
    if (latInput && lngInput && latInput.value.trim() && lngInput.value.trim()) {
      const latNum = parseFloat(latInput.value.trim());
      const lngNum = parseFloat(lngInput.value.trim());
      if (Number.isFinite(latNum) && Number.isFinite(lngNum) && latNum >= -90 && latNum <= 90 && lngNum >= -180 && lngNum <= 180) {
        setStatus(`Saved location active (${latNum.toFixed(4)}, ${lngNum.toFixed(4)}) ✓`, "location-ready");
        if (useLocationButton) useLocationButton.textContent = "Refresh Location";
      }
    }

    const captureLocation = () => {
      if (!navigator.geolocation) {
        setStatus("Unable to detect location: Geolocation is not supported by your browser. Please enter coordinates manually.", "location-error");
        return;
      }
      if (useLocationButton) {
        useLocationButton.disabled = true;
        useLocationButton.textContent = "Getting your location…";
      }
      setStatus("Waiting for location permission…");

      navigator.geolocation.getCurrentPosition(
        async (position) => {
          const lat = position.coords.latitude.toFixed(6);
          const lng = position.coords.longitude.toFixed(6);
          if (latInput) {
            latInput.value = lat;
            latInput.dispatchEvent(new Event("input", { bubbles: true }));
          }
          if (lngInput) {
            lngInput.value = lng;
            lngInput.dispatchEvent(new Event("input", { bubbles: true }));
          }
          if (document.body.dataset.loggedIn) {
            try {
              await postLocation(lat, lng);
            } catch (_) {
              // non-blocking
            }
          }
          setStatus(`Location detected (${parseFloat(lat).toFixed(4)}, ${parseFloat(lng).toFixed(4)}) ✓`, "location-ready");
          if (useLocationButton) {
            useLocationButton.disabled = false;
            useLocationButton.textContent = "Location detected ✓";
          }
        },
        (error) => {
          if (useLocationButton) {
            useLocationButton.disabled = false;
            useLocationButton.textContent = "Use My Current Location";
          }
          if (error && error.code === 1) { // PERMISSION_DENIED
            setStatus("Location permission denied. Enter coordinates manually below or enable location in browser settings.", "location-denied");
          } else if (error && error.code === 2) { // POSITION_UNAVAILABLE
            setStatus("Location unavailable from device. Please enter coordinates manually below.", "location-error");
          } else if (error && error.code === 3) { // TIMEOUT
            setStatus("Location request timed out. Please retry or enter coordinates manually below.", "location-error");
          } else {
            setStatus("Unable to detect location. You can enter coordinates manually below.", "location-error");
          }
        },
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
      );
    };

    if (useLocationButton) {
      useLocationButton.addEventListener("click", (e) => {
        e.preventDefault();
        captureLocation();
      });
    }

    form.addEventListener("submit", (e) => {
      if (!validateCoords()) {
        e.preventDefault();
        if (form.dataset.submitting === "true") delete form.dataset.submitting;
        form.querySelectorAll("button[type='submit']").forEach((btn) => {
          btn.disabled = false;
          if (btn.dataset.originalText) btn.textContent = btn.dataset.originalText;
        });
        toast("Please provide valid coordinates (-90 to 90 lat, -180 to 180 lng) or clear both fields.", "error");
      }
    });
  });

  document.querySelectorAll("[data-upload-form] .upload-zone").forEach((zone) => {
    const input = zone.querySelector("input[type='file']");
    const img = zone.querySelector(".preview");
    const remove = zone.querySelector(".remove-image");
    const showFile = () => {
      const file = input.files && input.files[0];
      if (!file) {
        img.removeAttribute("src");
        zone.classList.remove("has-image");
        return;
      }
      if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 5 * 1024 * 1024) {
        toast("Upload a JPG, PNG or WEBP image under 5 MB.", "error");
        input.value = "";
        return;
      }
      img.src = URL.createObjectURL(file);
      zone.classList.add("has-image");
    };
    input.addEventListener("change", showFile);
    remove.addEventListener("click", (event) => {
      event.preventDefault();
      input.value = "";
      showFile();
    });
    ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("dragging");
    }));
    ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.remove("dragging");
    }));
    zone.addEventListener("drop", (event) => {
      input.files = event.dataTransfer.files;
      showFile();
    });
  });

  document.querySelectorAll("form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (form.dataset.submitting === "true") {
        event.preventDefault();
        return;
      }
      form.dataset.submitting = "true";
      form.querySelectorAll("button[type='submit']").forEach((button) => {
        button.disabled = true;
        button.dataset.originalText = button.textContent;
        button.textContent = button.dataset.loadingText || "Please wait...";
      });
    });
  });

  const roleSelect = document.getElementById("roleSelect");
  if (roleSelect) {
    const providerWrap = document.getElementById("providerTypeWrap");
    const businessWrap = document.getElementById("businessWrap");
    const params = new URLSearchParams(window.location.search);
    if (params.get("role")) roleSelect.value = params.get("role");
    const sync = () => {
      const provider = roleSelect.value === "provider";
      providerWrap.style.display = provider ? "" : "none";
      businessWrap.style.display = provider ? "" : "none";
    };
    roleSelect.addEventListener("change", sync);
    sync();
  }
})();
