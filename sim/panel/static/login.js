// Sign-in for the shared panel: posts the PIN, the server sets a 12 h session cookie.
const THEME_KEY = "ems-panel-theme";
try {
  const saved = localStorage.getItem(THEME_KEY);
  const dark = saved ? saved === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
} catch { /* private mode */ }

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("login-error");
  const pin = document.getElementById("pin");
  err.textContent = "";
  if (pin.value.trim().length < 6) { err.textContent = "The PIN has at least 6 characters."; pin.focus(); return; }
  try {
    const resp = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-EMS-Panel": "1" },
      body: JSON.stringify({ pin: pin.value }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) { window.location.replace("/"); return; }
    err.textContent = data.error || `Sign-in failed (HTTP ${resp.status})`;
  } catch {
    err.textContent = "Can't reach the panel. Is the simulator still running?";
  }
  pin.select();
});
