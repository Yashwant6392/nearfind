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
      if (notification.type === "provider_response") return `/seeker/responses/${notification.query_id}`;
      if (notification.type === "new_request") return "/provider/dashboard";
      if (["chat_message", "provider_selected", "request_resolved"].includes(notification.type) && notification.response_id) {
        return `/chat/response/${notification.response_id}`;
      }
      return "/provider/dashboard";
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
            await fetch(`/notifications/${notification.id}/read`, {
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
      notificationRequest = fetch("/notifications", { cache: "no-store" })
        .then((response) => response.json())
        .then((payload) => {
          if (!payload.success) return;
          const notifications = payload.data.notifications || [];
          const unreadCount = payload.data.unread_count || 0;
          badge.textContent = unreadCount;
          badge.hidden = unreadCount === 0;
          announceNewNotifications(notifications);
          renderNotifications(notifications);
        })
        .catch(() => {})
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
    const notificationTimer = setInterval(() => {
      if (!document.hidden) loadNotifications();
    }, 10000);
    window.addEventListener("pagehide", () => clearInterval(notificationTimer), { once: true });
  }

  async function postLocation(lat, lng) {
    const res = await fetch("/user/update-location", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
      body: JSON.stringify({ lat, lng }),
    });
    return res.ok;
  }

  document.querySelectorAll("[data-location-form]").forEach((form) => {
    const status = form.querySelector("#locationStatus") || document.getElementById("locationStatus");
    const latInput = form.querySelector("input[name='lat']");
    const lngInput = form.querySelector("input[name='lng']");
    if (!navigator.geolocation) {
      if (status) status.textContent = "Enter coordinates manually. Browser geolocation is unavailable.";
      return;
    }
    navigator.geolocation.getCurrentPosition(
      async (position) => {
        const lat = position.coords.latitude.toFixed(6);
        const lng = position.coords.longitude.toFixed(6);
        if (latInput && !latInput.value) latInput.value = lat;
        if (lngInput && !lngInput.value) lngInput.value = lng;
        if (document.body.dataset.loggedIn) await postLocation(lat, lng);
        if (status) status.textContent = "Location captured for nearby discovery.";
      },
      () => {
        if (status) status.textContent = "Location permission was denied. Enter coordinates manually.";
      },
      { enableHighAccuracy: true, timeout: 10000 }
    );
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
        button.textContent = "Please wait...";
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
