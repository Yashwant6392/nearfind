(() => {
  const offset = document.getElementById("availabilityTimezoneOffset");
  const sharing = document.getElementById("sharingEnabled");
  const sharingLabel = document.getElementById("sharingSettingLabel");
  const latitude = document.getElementById("driverLatitude");
  const longitude = document.getElementById("driverLongitude");
  const status = document.getElementById("driverAvailabilityStatus");
  const locationButton = document.getElementById("useDriverCurrentLocation");
  const availToggle = document.getElementById("driverAvailabilityToggle");
  const availChip = document.getElementById("availabilityStatusChip");
  const availToggleText = document.getElementById("availabilityToggleStatusText");
  const publishBtn = document.getElementById("publishAvailabilityBtn");

  if (offset) offset.value = String(new Date().getTimezoneOffset());

  // Availability toggle
  if (availToggle) {
    availToggle.addEventListener("change", async () => {
      const active = availToggle.checked;
      availToggle.disabled = true;
      if (availToggleText) availToggleText.textContent = active ? "Activating availability…" : "Pausing availability…";
      const form = availToggle.closest("form") || document.querySelector(".driver-availability-form");
      const token = form?.elements?.csrf_token?.value || "";

      try {
        const resp = await fetch("/provider/driver-availability/toggle", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": token,
          },
          body: JSON.stringify({ active }),
        });
        const res = await resp.json();
        if (res.success) {
          if (active) {
            if (availChip) {
              availChip.className = "status-chip available";
              availChip.textContent = "Published";
            }
            if (availToggleText) availToggleText.textContent = "Schedule is active and visible to nearby seekers.";
            if (publishBtn) publishBtn.textContent = "Update Availability";
          } else {
            if (availChip) {
              availChip.className = "status-chip open";
              availChip.textContent = "Not Published";
            }
            if (availToggleText) availToggleText.textContent = "Schedule is paused. Set window and publish below.";
            if (publishBtn) publishBtn.textContent = "Publish Availability";
          }
        } else {
          throw new Error(res.error || "Failed to update availability status.");
        }
      } catch (err) {
        availToggle.checked = !active;
        if (availToggleText) availToggleText.textContent = `Error: ${err.message}`;
      } finally {
        availToggle.disabled = false;
      }
    });
  }

  locationButton?.addEventListener("click", () => {
    if (!sharing?.checked) {
      if (status) {
        status.className = "location-status unavailable";
        status.textContent = "Location sharing is disabled. Turn location sharing on before refreshing coordinates.";
      }
      return;
    }
    if (!navigator.geolocation) {
      if (status) {
        status.className = "location-status unavailable";
        status.textContent = "Geolocation is not supported by your browser. Enter coordinates manually.";
      }
      return;
    }

    locationButton.disabled = true;
    if (status) {
      status.className = "location-status";
      status.textContent = "Acquiring GPS fix from device...";
    }

    navigator.geolocation.getCurrentPosition(
      async ({ coords }) => {
        const latVal = coords.latitude.toFixed(6);
        const lngVal = coords.longitude.toFixed(6);
        if (latitude) latitude.value = latVal;
        if (longitude) longitude.value = lngVal;

        if (status) {
          status.className = "location-status";
          status.textContent = "Saving refreshed coordinates to server...";
        }

        const form = locationButton.form || locationButton.closest("form");
        const formData = new FormData();
        formData.set("csrf_token", form?.elements?.csrf_token?.value || "");
        formData.set("sharing_enabled", "on");
        formData.set("lat", latVal);
        formData.set("lng", lngVal);

        try {
          const response = await fetch("/provider/driver-location", {
            method: "POST",
            credentials: "same-origin",
            headers: { Accept: "application/json" },
            body: formData,
          });
          const result = await response.json();
          if (!response.ok || !result.success) {
            throw new Error(result.error?.message || result.error || "The location could not be refreshed.");
          }
          if (status) {
            status.className = "location-status available";
            status.textContent = `✓ Coordinates refreshed and saved (${latVal}, ${lngVal}). Discovery is active while fresh.`;
          }
        } catch (error) {
          if (status) {
            status.className = "location-status unavailable";
            status.textContent = error.message || "The location could not be refreshed.";
          }
        } finally {
          locationButton.disabled = false;
        }
      },
      (err) => {
        locationButton.disabled = false;
        if (status) {
          status.className = "location-status unavailable";
          if (err.code === 1) {
            status.textContent = "Location permission denied. Please allow location access in your browser settings.";
          } else if (err.code === 2) {
            status.textContent = "GPS position unavailable. Please enter coordinates manually.";
          } else if (err.code === 3) {
            status.textContent = "GPS acquisition timed out. Please try again.";
          } else {
            status.textContent = "Could not obtain device location. Please allow location access or enter coordinates manually.";
          }
        }
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 },
    );
  });

  sharing?.addEventListener("change", () => {
    if (sharingLabel) {
      sharingLabel.textContent = sharing.checked ? "On" : "Off";
      sharingLabel.style.color = sharing.checked ? "var(--lime)" : "var(--muted)";
    }
    if (status) {
      status.className = sharing.checked ? "location-status" : "location-status muted";
      status.textContent = sharing.checked
        ? "Location sharing is enabled. Your coordinates will be refreshed and used for nearby driver discovery."
        : "Location sharing is disabled. You will not appear in driver discovery.";
    }
  });
})();
