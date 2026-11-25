// Sky (light) or Midnight (dark), following the user's Telegram theme;
// ?theme=<name> overrides it for testing.
(function () {
  var root = document.documentElement, forced = null;
  try { forced = new URLSearchParams(location.search).get("theme"); } catch (e) {}
  var tg = window.Telegram && window.Telegram.WebApp;
  function apply() {
    var dark = tg && tg.colorScheme
      ? tg.colorScheme === "dark"
      : !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    var theme = forced || (dark ? "midnight" : "sky");
    root.setAttribute("data-theme", theme);
    if (tg) {
      try {
        tg.setHeaderColor(theme === "midnight" ? "#12223b" : "#bfe0fb");
        tg.setBackgroundColor(theme === "midnight" ? "#0c1524" : "#f2f8fe");
      } catch (e) {}
    }
  }
  window.applyTheme = apply;  // splash.js restores the theme's colours with it
  apply();
  if (tg && tg.onEvent) tg.onEvent("themeChanged", apply);
})();
