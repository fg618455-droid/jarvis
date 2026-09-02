/* Which paint the instrument wears.

   A theme is a block of colour tokens in `tokens.css` and a row in the list
   below. Nothing else in the interface knows a theme exists: every view
   reads `var(--accent)` and gets whatever the active block says it is, so
   adding a theme is adding a block and a row rather than touching a view.

   The choice is this browser's, not the daemon's. It is a preference about
   looking at a screen rather than a fact about the assistant, so it lives in
   `localStorage` and never reaches `config.json`. Two people on two machines
   reading the same daemon can therefore disagree about the palette without
   either of them writing to the other's configuration. */

const STORAGE_KEY = "jarvis.theme";

/* `label` is the name as written, not a translation key: a theme is called
   the same thing in every language, the way a colour swatch is. */
export const THEMES = [
  { id: "graphite", label: "Graphite" },
  { id: "arc", label: "Arc" },
  { id: "ember", label: "Ember" },
];

export const DEFAULT_THEME = "graphite";

function known(id) {
  return THEMES.some((theme) => theme.id === id) ? id : null;
}

/* Reading storage can throw outright: a browser with site data disabled
   raises on access rather than returning null. A theme is a preference, so
   failing to recall one is not worth an error on the page. */
function stored() {
  try {
    return known(localStorage.getItem(STORAGE_KEY));
  } catch {
    return null;
  }
}

export function activeTheme() {
  return known(document.documentElement.dataset.theme) || stored() || DEFAULT_THEME;
}

export function applyTheme(id) {
  const theme = known(id) || DEFAULT_THEME;
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* The page still wears it for this session. */
  }
  return theme;
}

/* Called before the first view is built, so nothing paints twice. */
export function startTheme() {
  return applyTheme(activeTheme());
}
