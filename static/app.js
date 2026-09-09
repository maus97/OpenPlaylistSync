document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-loading-message]");
  if (!form || event.defaultPrevented) return;
  const overlay = document.getElementById("page-loading");
  const message = document.getElementById("page-loading-message");
  if (!overlay || !message) return;
  message.textContent = form.dataset.loadingMessage;
  overlay.hidden = false;
  document.body.setAttribute("aria-busy", "true");
  const submitter = event.submitter;
  if (submitter) {
    submitter.dataset.loadingDisabled = "true";
    submitter.disabled = true;
  }
});

// Browsers can restore a submitted page from their back/forward cache.
window.addEventListener("pageshow", () => {
  const overlay = document.getElementById("page-loading");
  if (overlay) overlay.hidden = true;
  document.body.removeAttribute("aria-busy");
  document.querySelectorAll('[data-loading-disabled="true"]').forEach((button) => {
    button.disabled = false;
    delete button.dataset.loadingDisabled;
  });
});
