// Persist and toggle the [data-theme] attribute on <html>.
// Initial value is set inline in <head> to avoid flash-of-wrong-theme.

(function () {
  const root = document.documentElement;
  const button = document.getElementById("theme-toggle");
  if (!button) return;

  function currentTheme() {
    if (root.dataset.theme === "dark" || root.dataset.theme === "light") {
      return root.dataset.theme;
    }
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function applyAndStore(theme) {
    root.dataset.theme = theme;
    try { localStorage.setItem("theme", theme); } catch (e) {}
  }

  button.addEventListener("click", () => {
    applyAndStore(currentTheme() === "dark" ? "light" : "dark");
  });

  // If user hasn't set an explicit choice, follow OS changes live.
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  mq.addEventListener("change", (e) => {
    let stored = null;
    try { stored = localStorage.getItem("theme"); } catch (_) {}
    if (stored !== "dark" && stored !== "light") {
      delete root.dataset.theme;
    }
  });
})();
