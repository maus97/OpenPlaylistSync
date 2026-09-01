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
  if (submitter) submitter.disabled = true;
});
