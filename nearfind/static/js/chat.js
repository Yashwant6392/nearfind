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
  let loading = false;
  let sending = false;
  let timer;

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
      const response = await fetch(`/chat/${conversationId}/messages`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || !payload.success) throw new Error(payload.error || "Unable to load messages");
      errorEl.hidden = true;
      render(payload.data.messages || []);
      await fetch(`/chat/${conversationId}/read`, {
        method: "POST",
        headers: { "X-CSRFToken": window.NearFindCSRFToken || "" },
      });
    } catch (error) {
      errorEl.hidden = false;
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
      const response = await fetch(`/chat/${conversationId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": window.NearFindCSRFToken || "" },
        body: JSON.stringify({ message }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.success) throw new Error(payload.error || "Unable to send message");
      input.value = "";
      await loadMessages();
    } catch (error) {
      errorEl.hidden = false;
      errorEl.textContent = "Unable to send message.";
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

  const poll = () => {
    if (!document.hidden) loadMessages();
  };
  loadMessages();
  timer = setInterval(poll, 4000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadMessages();
  });
  window.addEventListener("pagehide", () => clearInterval(timer), { once: true });
})();
