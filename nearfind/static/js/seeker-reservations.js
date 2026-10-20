(function () {
  const container = document.getElementById("seekerReservationsSection");
  if (!container) return;

  const bedContentEl = document.getElementById("bedReservationContent");
  const driverContentEl = document.getElementById("driverTripContent");
  const bedSyncIndicator = document.getElementById("bedSyncIndicator");
  const driverSyncIndicator = document.getElementById("driverSyncIndicator");

  let isFetching = false;
  let pollTimer = null;
  const POLL_INTERVAL = 5000;

  function getCsrfToken() {
    const meta = document.querySelector("meta[name='csrf-token']");
    if (meta && meta.content) return meta.content;
    return window.NearFindCSRFToken || "";
  }

  function setSyncStatus(isLive, message) {
    const text = isLive ? "● Live" : (message || "● Syncing...");
    const color = isLive ? "var(--green)" : "var(--gold)";
    if (bedSyncIndicator) {
      bedSyncIndicator.textContent = text;
      bedSyncIndicator.style.color = color;
    }
    if (driverSyncIndicator) {
      driverSyncIndicator.textContent = text;
      driverSyncIndicator.style.color = color;
    }
  }

  function titleCase(str) {
    if (!str) return "";
    return String(str)
      .replace(/_/g, " ")
      .replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }

  function renderBedSection(data) {
    if (!bedContentEl) return;
    const activeHold = data && data.active_hold;
    const recentHolds = (data && data.recent_holds) || [];

    if (activeHold) {
      const card = document.createElement("div");
      card.className = "active-reservation-content";

      const headerRow = document.createElement("div");
      headerRow.style.cssText = "display: flex; justify-content: space-between; align-items: flex-start; gap: 8px;";

      const title = document.createElement("h3");
      title.style.cssText = "margin: 0; font-size: 18px;";
      title.textContent = activeHold.hospital_name || "Hospital Facility";

      const chip = document.createElement("span");
      chip.className = "status-chip available";
      chip.textContent = "Active Hold";

      headerRow.appendChild(title);
      headerRow.appendChild(chip);
      card.appendChild(headerRow);

      const bedP = document.createElement("p");
      bedP.style.cssText = "margin: 6px 0 4px; font-weight: 600;";
      bedP.appendChild(document.createTextNode("Reserved Bed: "));
      const bedStrong = document.createElement("strong");
      bedStrong.style.color = "var(--gold)";
      bedStrong.textContent = (activeHold.bed_type_label || titleCase(activeHold.bed_type || "General")) + " Bed";
      bedP.appendChild(bedStrong);
      card.appendChild(bedP);

      if (activeHold.hospital_address) {
        const addrP = document.createElement("p");
        addrP.className = "muted";
        addrP.style.cssText = "margin: 0 0 6px; font-size: 13px;";
        addrP.textContent = activeHold.hospital_address;
        card.appendChild(addrP);
      }

      if (activeHold.expires_at) {
        const expP = document.createElement("p");
        expP.className = "notice";
        expP.style.cssText = "margin: 6px 0 12px; font-size: 13px;";
        expP.textContent = "Held until " + activeHold.expires_at;
        card.appendChild(expP);
      }

      const actions = document.createElement("div");
      actions.className = "actions";
      actions.style.cssText = "margin-top: 12px; gap: 8px;";

      if (activeHold.hospital_phone) {
        const callBtn = document.createElement("a");
        callBtn.className = "btn small primary";
        callBtn.href = "tel:" + activeHold.hospital_phone;
        callBtn.textContent = "Call (" + activeHold.hospital_phone + ")";
        actions.appendChild(callBtn);
      }

      if (activeHold.hospital_lat != null && activeHold.hospital_lng != null) {
        const dirBtn = document.createElement("a");
        dirBtn.className = "btn small";
        dirBtn.href = "https://www.google.com/maps/dir/?api=1&destination=" + encodeURIComponent(activeHold.hospital_lat + "," + activeHold.hospital_lng);
        dirBtn.target = "_blank";
        dirBtn.rel = "noopener noreferrer";
        dirBtn.textContent = "Directions";
        actions.appendChild(dirBtn);
      }

      const cancelForm = document.createElement("form");
      cancelForm.method = "post";
      cancelForm.action = "/hospitals/holds/" + activeHold.id + "/cancel";
      cancelForm.style.display = "inline";
      cancelForm.className = "cancel-hold-form";

      const csrfInput = document.createElement("input");
      csrfInput.type = "hidden";
      csrfInput.name = "csrf_token";
      csrfInput.value = getCsrfToken();
      cancelForm.appendChild(csrfInput);

      const cancelBtn = document.createElement("button");
      cancelBtn.type = "submit";
      cancelBtn.className = "btn ghost small";
      cancelBtn.textContent = "Cancel Hold";
      cancelForm.appendChild(cancelBtn);

      cancelForm.addEventListener("submit", function (e) {
        if (!confirm("Cancel this emergency bed hold?")) {
          e.preventDefault();
        }
      });
      actions.appendChild(cancelForm);
      card.appendChild(actions);

      bedContentEl.replaceChildren(card);
    } else if (recentHolds.length > 0) {
      const hold = recentHolds[0];
      const card = document.createElement("div");
      card.className = "recent-reservation-content";

      const headerRow = document.createElement("div");
      headerRow.style.cssText = "display: flex; justify-content: space-between; align-items: flex-start; gap: 8px;";

      const title = document.createElement("h3");
      title.style.cssText = "margin: 0; font-size: 18px;";
      title.textContent = hold.hospital_name || "Hospital Facility";

      const statusClass = hold.effective_status === "admitted"
        ? "matched"
        : (hold.effective_status === "expired" ? "expired" : "selected");

      const chip = document.createElement("span");
      chip.className = "status-chip " + statusClass;
      chip.textContent = titleCase(hold.effective_status);

      headerRow.appendChild(title);
      headerRow.appendChild(chip);
      card.appendChild(headerRow);

      const bedP = document.createElement("p");
      bedP.style.cssText = "margin: 6px 0 4px;";
      bedP.textContent = (hold.bed_type_label || titleCase(hold.bed_type || "General")) + " Bed";
      card.appendChild(bedP);

      if (hold.effective_status === "admitted") {
        const note = document.createElement("p");
        note.className = "notice";
        note.style.cssText = "color: var(--green); font-size: 13px;";
        note.textContent = "✓ Hospital admission confirmed. Patient admitted.";
        card.appendChild(note);
      } else if (hold.effective_status === "cancelled") {
        const note = document.createElement("p");
        note.className = "muted";
        note.style.cssText = "font-size: 13px;";
        note.textContent = "Hold was cancelled and capacity restored.";
        card.appendChild(note);
      } else if (hold.effective_status === "expired") {
        const note = document.createElement("p");
        note.className = "muted";
        note.style.cssText = "font-size: 13px;";
        note.textContent = "Hold duration elapsed.";
        card.appendChild(note);
      }

      const actions = document.createElement("div");
      actions.style.marginTop = "12px";
      const link = document.createElement("a");
      link.className = "btn small";
      link.href = "/seeker/emergency-bed";
      link.textContent = "Find emergency bed";
      actions.appendChild(link);
      card.appendChild(actions);

      bedContentEl.replaceChildren(card);
    } else {
      const card = document.createElement("div");
      card.className = "empty-reservation";

      const emptyText = document.createElement("p");
      emptyText.className = "muted";
      emptyText.style.cssText = "margin: 0 0 10px;";
      emptyText.textContent = "No active emergency bed reservations.";
      card.appendChild(emptyText);

      const link = document.createElement("a");
      link.className = "btn small";
      link.href = "/seeker/emergency-bed";
      link.textContent = "Find emergency bed";
      card.appendChild(link);

      bedContentEl.replaceChildren(card);
    }
  }

  function renderDriverSection(data) {
    if (!driverContentEl) return;
    const activeTrip = data && data.active_trip;
    const recentTrips = (data && data.recent_trips) || [];

    if (activeTrip) {
      const card = document.createElement("div");
      card.className = "active-trip-content";

      const headerRow = document.createElement("div");
      headerRow.style.cssText = "display: flex; justify-content: space-between; align-items: flex-start; gap: 8px;";

      const leftInfo = document.createElement("div");
      const routeStrong = document.createElement("strong");
      routeStrong.style.fontSize = "15px";
      routeStrong.textContent = (activeTrip.pickup_address || "Pickup") + " → " + (activeTrip.destination_address || "Destination");

      const timeP = document.createElement("p");
      timeP.className = "muted";
      timeP.style.cssText = "margin: 2px 0 0; font-size: 13px;";
      timeP.textContent = (activeTrip.requested_start || "") + " · " + (activeTrip.duration_minutes || 60) + " mins";

      leftInfo.appendChild(routeStrong);
      leftInfo.appendChild(timeP);

      const statusChipClass = activeTrip.status === "open"
        ? "open"
        : (["in_progress", "completed"].includes(activeTrip.status) ? "available" : "selected");

      const chip = document.createElement("span");
      chip.className = "status-chip " + statusChipClass;
      chip.textContent = activeTrip.status_label || titleCase(activeTrip.status);

      headerRow.appendChild(leftInfo);
      headerRow.appendChild(chip);
      card.appendChild(headerRow);

      if (activeTrip.driver) {
        const driverBox = document.createElement("div");
        driverBox.className = "driver-info-box";
        driverBox.style.cssText = "margin-top: 12px; padding: 10px; background: rgba(255,255,255,0.05); border-radius: 6px;";

        const driverName = document.createElement("p");
        driverName.style.cssText = "margin: 0; font-weight: 700;";
        driverName.textContent = "👤 " + (activeTrip.driver.name || "Assigned Driver");
        driverBox.appendChild(driverName);

        if (activeTrip.driver.experience_years || activeTrip.driver.licence_category) {
          const driverMeta = document.createElement("p");
          driverMeta.className = "muted";
          driverMeta.style.cssText = "margin: 2px 0 0; font-size: 12px;";
          const parts = [];
          if (activeTrip.driver.experience_years) parts.push(activeTrip.driver.experience_years + " yrs exp");
          if (activeTrip.driver.licence_category) parts.push(activeTrip.driver.licence_category);
          driverMeta.textContent = parts.join(" · ");
          driverBox.appendChild(driverMeta);
        }
        card.appendChild(driverBox);
      } else if (activeTrip.status === "open") {
        const noticeP = document.createElement("p");
        noticeP.className = "notice";
        noticeP.style.cssText = "margin: 10px 0 4px; font-size: 13px;";
        noticeP.textContent = "Matching available drivers within request radius...";
        card.appendChild(noticeP);
      }

      const actions = document.createElement("div");
      actions.className = "actions";
      actions.style.cssText = "margin-top: 12px; gap: 8px;";

      if (activeTrip.driver && activeTrip.driver.phone) {
        const callBtn = document.createElement("a");
        callBtn.className = "btn small primary";
        callBtn.href = "tel:" + activeTrip.driver.phone;
        callBtn.textContent = "Call Driver";
        actions.appendChild(callBtn);
      }

      const viewReqBtn = document.createElement("a");
      viewReqBtn.className = "btn small";
      viewReqBtn.href = "/seeker/driver-request?request_id=" + encodeURIComponent(activeTrip.request_id || activeTrip.id);
      viewReqBtn.textContent = "View Request";
      actions.appendChild(viewReqBtn);

      if (activeTrip.can_cancel && activeTrip.booking_id) {
        const cancelForm = document.createElement("form");
        cancelForm.method = "post";
        cancelForm.action = "/seeker/driver-bookings/" + activeTrip.booking_id + "/cancel";
        cancelForm.style.display = "inline";
        cancelForm.className = "cancel-booking-form";

        const csrfInput = document.createElement("input");
        csrfInput.type = "hidden";
        csrfInput.name = "csrf_token";
        csrfInput.value = getCsrfToken();
        cancelForm.appendChild(csrfInput);

        const cancelBtn = document.createElement("button");
        cancelBtn.type = "submit";
        cancelBtn.className = "btn ghost small";
        cancelBtn.textContent = "Cancel Booking";
        cancelForm.appendChild(cancelBtn);

        cancelForm.addEventListener("submit", function (e) {
          if (!confirm("Cancel this driver booking?")) {
            e.preventDefault();
          }
        });
        actions.appendChild(cancelForm);
      }

      card.appendChild(actions);
      driverContentEl.replaceChildren(card);
    } else if (recentTrips.length > 0) {
      const trip = recentTrips[0];
      const card = document.createElement("div");
      card.className = "recent-trip-content";

      const headerRow = document.createElement("div");
      headerRow.style.cssText = "display: flex; justify-content: space-between; align-items: flex-start; gap: 8px;";

      const leftInfo = document.createElement("div");
      const routeStrong = document.createElement("strong");
      routeStrong.style.fontSize = "14px";
      routeStrong.textContent = (trip.pickup_address || "Pickup") + " → " + (trip.destination_address || "Destination");

      const timeP = document.createElement("p");
      timeP.className = "muted";
      timeP.style.cssText = "margin: 2px 0 0; font-size: 13px;";
      timeP.textContent = trip.requested_start || "";

      leftInfo.appendChild(routeStrong);
      leftInfo.appendChild(timeP);

      const isCompleted = trip.status === "completed";
      const chip = document.createElement("span");
      chip.className = "status-chip " + (isCompleted ? "matched" : "expired");
      chip.textContent = trip.status_label || titleCase(trip.status);

      headerRow.appendChild(leftInfo);
      headerRow.appendChild(chip);
      card.appendChild(headerRow);

      const noteP = document.createElement("p");
      if (isCompleted) {
        noteP.className = "notice";
        noteP.style.cssText = "color: var(--green); margin-top: 8px; font-size: 13px;";
        noteP.textContent = "✓ Trip completed.";
      } else {
        noteP.className = "muted";
        noteP.style.cssText = "margin-top: 8px; font-size: 13px;";
        noteP.textContent = "Trip " + (trip.status_label || trip.status).toLowerCase() + ".";
      }
      card.appendChild(noteP);

      const actions = document.createElement("div");
      actions.style.cssText = "margin-top: 12px; display: flex; gap: 8px;";

      const viewDetailsBtn = document.createElement("a");
      viewDetailsBtn.className = "btn small";
      viewDetailsBtn.href = "/seeker/driver-request?request_id=" + encodeURIComponent(trip.request_id || trip.id);
      viewDetailsBtn.textContent = "View Details";
      actions.appendChild(viewDetailsBtn);

      const reqDriverBtn = document.createElement("a");
      reqDriverBtn.className = "btn small";
      reqDriverBtn.href = "/seeker/driver-request";
      reqDriverBtn.textContent = "Request a driver";
      actions.appendChild(reqDriverBtn);

      card.appendChild(actions);
      driverContentEl.replaceChildren(card);
    } else {
      const card = document.createElement("div");
      card.className = "empty-reservation";

      const emptyText = document.createElement("p");
      emptyText.className = "muted";
      emptyText.style.cssText = "margin: 0 0 10px;";
      emptyText.textContent = "No driver trips or requests yet.";
      card.appendChild(emptyText);

      const link = document.createElement("a");
      link.className = "btn small";
      link.href = "/seeker/driver-request";
      link.textContent = "Request a driver for my car";
      card.appendChild(link);

      driverContentEl.replaceChildren(card);
    }
  }

  async function syncReservations() {
    if (isFetching || document.hidden) return;
    isFetching = true;
    try {
      const fetchFunc = window.NearFindFetch || (async function (url, opts) {
        const res = await fetch(url, opts);
        const payload = await res.json();
        return { response: res, payload: payload };
      });
      const { payload } = await fetchFunc("/seeker/active-reservations", { cache: "no-store" });
      if (payload && payload.success && payload.data) {
        renderBedSection(payload.data.bed_reservations);
        renderDriverSection(payload.data.driver_trips);
        setSyncStatus(true);
      }
    } catch (err) {
      setSyncStatus(false, "● Reconnecting...");
    } finally {
      isFetching = false;
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(syncReservations, POLL_INTERVAL);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  startPolling();

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopPolling();
    } else {
      syncReservations();
      startPolling();
    }
  });

  window.addEventListener("pagehide", stopPolling, { once: true });
  window.addEventListener("beforeunload", stopPolling, { once: true });
})();
