/* The face: the centre of the interface rather than a page inside it.

   The face itself is the vendored, AGPL-3.0-licensed ai-visualizer gallery,
   which is a self-contained page with its own canvas, its own animation
   loop, and its own palette. It is framed rather than reimplemented, and it
   is never edited: harmonising it with the interface is done entirely on
   this side of the frame, by the bed it sits on, the aura behind it, the
   rings around it, and how large it is drawn.

   Picking a face is a control here. The gallery has a picker of its own, but
   reaching it meant loading the gallery's index inside the frame and
   choosing there, which put a second, differently-styled navigation inside
   the page. One face is loaded directly, and which one is this browser's
   preference, the same way the theme is. */

import { api } from "./api.js";
import { t } from "./i18n.js";
import { el, icon, ICONS } from "./ui.js";

const FACE_KEY = "jarvis.face";
const SIZE_KEY = "jarvis.faceSize";

const DEFAULT_FACE = "radial";
export const MIN_SIZE = 180;
export const MAX_SIZE = 560;
const DEFAULT_SIZE = 320;

function remembered(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function remember(key, value) {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    /* The choice still holds for this session. */
  }
}

function clampSize(value) {
  const number = Number.parseInt(value, 10);
  if (!Number.isFinite(number)) return DEFAULT_SIZE;
  return Math.min(MAX_SIZE, Math.max(MIN_SIZE, number));
}

/* A face id becomes a path, so it is checked against the gallery the daemon
   actually reports rather than trusted. A stale preference for a face that
   has since been removed falls back instead of framing a 404. */
function faceSource(id) {
  return `/visualizer/faces/${encodeURIComponent(id)}/index.html`;
}

export function mountFace(stage, { onSend, onMicToggle } = {}) {
  const state = {
    faces: [],
    face: remembered(FACE_KEY, DEFAULT_FACE),
    size: clampSize(remembered(SIZE_KEY, DEFAULT_SIZE)),
    name: "Jarvis",
  };

  const frame = el("iframe", {
    class: "face-frame",
    src: faceSource(state.face),
    title: t("face.title"),
  });
  const shell = el("div", { class: "face-shell" }, [
    el("div", { class: "face-rings" }, [el("span"), el("span"), el("span")]),
    frame,
  ]);

  const nameNode = el("div", { class: "face-name", text: state.name });
  const stateNode = el("div", { class: "face-state" });

  /* ── Speaking to it ─────────────────────────────────────────────── */

  const input = el("input", {
    type: "text",
    "aria-label": t("face.askLabel"),
    placeholder: t("face.askPlaceholder"),
  });
  const mic = el(
    "button",
    {
      type: "button",
      class: "dock-button face-mic",
      "aria-label": t("face.microphone"),
      "aria-pressed": "false",
      onclick: () => onMicToggle && onMicToggle(mic),
    },
    [icon(ICONS.microphone)],
  );
  const send = el(
    "button",
    {
      type: "button",
      class: "dock-button",
      "aria-label": t("face.send"),
      onclick: () => submit(),
    },
    [icon(ICONS.send)],
  );

  async function submit() {
    const text = input.value.trim();
    if (!text || !onSend) return;
    input.value = "";
    send.disabled = true;
    try {
      await onSend(text);
    } finally {
      send.disabled = false;
    }
  }

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submit();
  });

  const dock = el("div", { class: "face-dock" }, [mic, input, send]);

  /* ── Dressing it ────────────────────────────────────────────────── */

  const facePicker = el("select", { name: "face", onchange: () => setFace(facePicker.value) });
  const sizePicker = el("input", {
    type: "range",
    name: "size",
    min: String(MIN_SIZE),
    max: String(MAX_SIZE),
    step: "20",
    value: String(state.size),
    oninput: () => setSize(sizePicker.value),
  });

  const settings = el("div", { class: "face-settings", hidden: true }, [
    el("label", {}, [
      el("span", { text: t("face.whichFace") }),
      facePicker,
    ]),
    el("label", {}, [
      el("span", { text: t("face.size") }),
      sizePicker,
    ]),
  ]);

  const settingsToggle = el(
    "button",
    {
      type: "button",
      class: "face-settings-open",
      "aria-label": t("face.customise"),
      "aria-expanded": "false",
      onclick: () => {
        const open = settings.hidden;
        settings.hidden = !open;
        settingsToggle.setAttribute("aria-expanded", open ? "true" : "false");
      },
    },
    [icon(ICONS.sliders)],
  );

  function setFace(id) {
    state.face = id;
    remember(FACE_KEY, id);
    frame.src = faceSource(id);
  }

  function setSize(value) {
    state.size = clampSize(value);
    remember(SIZE_KEY, state.size);
    // Written on the element rather than the stylesheet: this is one
    // reader's preference about one face, not a retune of the interface.
    shell.style.setProperty("--face-size", `${state.size}px`);
  }

  setSize(state.size);

  stage.append(shell, nameNode, stateNode, dock, settingsToggle, settings);

  /* ── What it is doing ───────────────────────────────────────────── */

  /* The face animates the phase itself. The words under it are for the
     reader who cannot tell an idle face from a listening one, and for the
     one who asked for no motion at all. */
  function paintPhase(phase, connected) {
    const known = phase && t(`phase.${phase}`) !== `phase.${phase}`;
    stateNode.replaceChildren(
      el("span", { class: `state-pill${phase === "idle" ? "" : " live"}` }, [
        el("span", { class: "state-pill-dot", dataset: { phase: phase || "offline" } }),
        el("span", {
          text: connected === false
            ? t("common.reconnecting")
            : known ? t(`phase.${phase}`) : t("phase.offline"),
        }),
      ]),
    );
  }

  async function loadConfig() {
    try {
      const config = await api.visualizerConfig();
      state.faces = config.faces || [];
      state.name = config.name || "Jarvis";
      nameNode.textContent = state.name;

      const ids = state.faces.map((face) => face.id);
      if (ids.length && !ids.includes(state.face)) setFace(ids[0]);

      facePicker.replaceChildren(
        ...state.faces.map((face) =>
          el("option", {
            value: face.id,
            text: face.title || face.id,
            selected: face.id === state.face,
          }),
        ),
      );
    } catch {
      /* Without the gallery listing the framed face still loads; only the
         picker has nothing to offer, which it shows as an empty select. */
    }
  }

  loadConfig();
  paintPhase("idle");

  return {
    paintPhase,
    setName(name) {
      if (!name) return;
      state.name = name;
      nameNode.textContent = name;
    },
    micButton: mic,
  };
}
