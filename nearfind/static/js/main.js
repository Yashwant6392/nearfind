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
