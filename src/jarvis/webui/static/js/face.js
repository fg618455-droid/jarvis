/* The face: the centre of the interface rather than a page inside it.

   One circle inside one ring. What the assistant is doing is carried by the
   size of the disc, which is a channel that survives a reader who has asked
   for no motion: with every animation off, idle, listening, thinking and
   speaking are still four different pictures. A face that separated its
   states by the speed of a rotation would have nothing left to say to that
   reader, and the state is the one thing this drawing exists to report.

   It is painted from `var(--accent)`, read off the stylesheet rather than
   held here, so a theme drives the face for free and there is no second
   palette to keep in step with the first.

   The reading comes from `/api/visualizer/state`, which derives everything
   from Jarvis's own live objects. The words beside the face come from the
   event stream instead, because that is the only source that can say the
   page has lost the daemon: a poll that fails and a daemon that is idle look
   identical from here. */

import { api } from "./api.js";
import { t } from "./i18n.js";
import { el, icon, ICONS, motionAllowed } from "./ui.js";

const SIZE_KEY = "jarvis.faceSize";

const MIN_SIZE = 180;
export const MAX_SIZE = 560;
const DEFAULT_SIZE = 400;

/* Eight times a second: fast enough that speech and the mouth agree, slow
   enough that an idle assistant is not being asked constantly. */
const POLL_MS = 125;

/* How large the disc is drawn, as a share of the ring it sits in. This table
   is the state contract: four values far enough apart to be told apart in a
   still picture across a room. */
const DISC = {
  idle: 0.52,
  listening: 0.80,
  thinking: 0.52,
  speaking: 0.66,
};

/* How far a waveform may push the edge of the disc, as a share of its radius.
   Past roughly a tenth the outline stops reading as a circle that is speaking
   and starts reading as a shape that is not a circle. */
const SPEECH_DEFORMATION = 0.07;

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

/* A waveform straight from the speakers is jagged, and jaggedness at this
   size reads as noise in the drawing rather than as sound. Each point is
   averaged with its neighbours around the ring, so the outline stays a
   circle that is moving rather than a star. */
function smoothed(samples) {
  const peak = samples.reduce((most, value) => Math.max(most, Math.abs(value)), 0);
  if (!peak) return null;
  const unit = samples.map((value) => Math.abs(value) / peak);
  return unit.map((_, index) => {
    const before = unit[(index - 1 + unit.length) % unit.length];
    const after = unit[(index + 1) % unit.length];
    return (before + unit[index] * 2 + after) / 4;
  });
}

export function mountFace(stage, { onSend, onMicToggle } = {}) {
  const state = {
    size: clampSize(remembered(SIZE_KEY, DEFAULT_SIZE)),
    reading: "idle",
    wave: null,
    name: "Jarvis",
  };

  const canvas = el("canvas", { class: "face-canvas", role: "img" });
  const shell = el("div", { class: "face-shell" }, [canvas]);
  const context = canvas.getContext("2d");

  const nameNode = el("div", { class: "face-name", text: state.name });
  const stateNode = el("div", { class: "face-state" });

  /* The face, its name, and what it is doing: one block, so the stage can
     hold it in the middle and stand the dock on the floor underneath. */
  const portrait = el("div", { class: "face-portrait" }, [shell, nameNode, stateNode]);

  /* ── The drawing ─────────────────────────────────────────────────── */

  /* Reading a custom property is a style resolution, which is too expensive
     to do sixty times a second. The theme is the only thing that changes it
     and it announces itself on the root element, so the answer is kept until
     that changes. */
  let paintedFor = null;
  let accent = "#ffffff";
  function accentColour() {
    const theme = document.documentElement.dataset.theme;
    if (theme !== paintedFor) {
      accent = getComputedStyle(document.documentElement)
        .getPropertyValue("--accent")
        .trim() || accent;
      paintedFor = theme;
    }
    return accent;
  }

  /* An alpha of the accent, whatever notation the theme wrote it in.
     `color-mix` keeps this working for a hex, an `rgb()`, and an `oklch()`
     alike, so a theme is never constrained in how it names its colours. */
  function fade(colour, alpha) {
    return `color-mix(in srgb, ${colour} ${Math.round(alpha * 100)}%, transparent)`;
  }

  function draw(seconds) {
    // The size the browser settled on, not the one that was asked for. In a
    // column too narrow for the preference the stylesheet caps it, and a
    // drawing made at the requested size would be cut off by the difference.
    const size = canvas.clientWidth || state.size;
    if (!size) return;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(size * dpr)) {
      canvas.width = Math.round(size * dpr);
      canvas.height = Math.round(size * dpr);
    }
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, size, size);

    const moving = motionAllowed();
    const time = moving ? seconds : 0;
    const colour = accentColour();
    const reading = state.reading;
    const ring = size * 0.40;

    context.save();
    context.translate(size / 2, size / 2);

    /* The light the drawing sits in, so the centre of the page reads as
       depth rather than as a sticker on a flat surface. */
    const aura = context.createRadialGradient(0, 0, ring * 0.3, 0, 0, ring * 1.5);
    aura.addColorStop(0, fade(colour, 0.16));
    aura.addColorStop(1, "transparent");
    context.fillStyle = aura;
    context.beginPath();
    context.arc(0, 0, ring * 1.5, 0, Math.PI * 2);
    context.fill();

    /* The ring. It never changes, so it is the fixed thing the disc inside
       it is read against: how open the face is, is how much of the ring the
       disc has taken. */
    context.strokeStyle = fade(colour, 0.32);
    context.lineWidth = Math.max(1, size * 0.005);
    context.beginPath();
    context.arc(0, 0, ring, 0, Math.PI * 2);
    context.stroke();

    /* The disc. Idle breathes, because a face that is perfectly still reads
       as a picture of an assistant rather than as one that is running. */
    const breath = reading === "idle" && moving ? 1 + 0.025 * Math.sin(time * 1.15) : 1;
    const radius = ring * DISC[reading] * breath;

    context.fillStyle = colour;
    const wave = reading === "speaking" && moving ? smoothed(state.wave || []) : null;
    if (wave) {
      context.beginPath();
      const steps = 160;
      for (let step = 0; step <= steps; step += 1) {
        const angle = (step / steps) * Math.PI * 2;
        const at = (step / steps) * wave.length;
        const low = Math.floor(at) % wave.length;
        const high = (low + 1) % wave.length;
        const blend = at - Math.floor(at);
        const value = wave[low] * (1 - blend) + wave[high] * blend;
        const reach = radius * (1 + (value - 0.5) * 2 * SPEECH_DEFORMATION);
        const x = Math.cos(angle) * reach;
        const y = Math.sin(angle) * reach;
        if (step === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      }
      context.closePath();
      context.fill();
    } else {
      context.beginPath();
      context.arc(0, 0, radius, 0, Math.PI * 2);
      context.fill();
    }

    /* Thinking puts one mark on the ring and walks it round. One moving part
       rather than an orbit of them, and parked at the top when nothing is
       allowed to move, so the state is still a different picture. */
    if (reading === "thinking") {
      context.strokeStyle = colour;
      context.lineWidth = Math.max(2, size * 0.011);
      context.lineCap = "round";
      const from = moving ? time * 1.5 : -Math.PI / 2;
      context.beginPath();
      context.arc(0, 0, ring, from, from + Math.PI * 0.45);
      context.stroke();
    }

    context.restore();
  }

  /* ── Keeping it painted ──────────────────────────────────────────── */

  let frame = null;
  let started = null;

  function loop(now) {
    if (started === null) started = now;
    draw((now - started) / 1000);
    frame = window.requestAnimationFrame(loop);
  }

  function repaint() {
    // With motion refused there is no loop at all, so a new reading is the
    // only thing that repaints and the picture is otherwise perfectly still.
    if (motionAllowed()) return;
    draw(0);
  }

  function startPainting() {
    if (motionAllowed()) frame = window.requestAnimationFrame(loop);
    else draw(0);
  }

  function stopPainting() {
    if (frame !== null) window.cancelAnimationFrame(frame);
    frame = null;
  }

  /* ── The reading ─────────────────────────────────────────────────── */

  let polling = null;

  async function takeReading() {
    // The server closes every connection it answers, so a poll is a fresh
    // socket rather than a reuse of one, and a socket that has been closed is
    // held by the operating system for minutes afterwards. Eight a second is
    // affordable while someone is watching the face and is not affordable for
    // a tab left open behind another window all day, which is why a hidden
    // page stops asking rather than merely stops drawing.
    if (document.hidden) return;
    try {
      const reading = await api.visualizerState();
      const named = DISC[reading.state] ? reading.state : "idle";
      const changed = named !== state.reading;
      state.reading = named;
      state.wave = named === "speaking" ? reading.samples || [] : null;
      if (changed) repaint();
    } catch {
      /* A poll that failed says nothing about the assistant, so the face
         holds its last honest reading rather than dropping to idle. What
         the page has actually lost is said in words beside it. */
    }
  }

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

  /* ── Sizing it ──────────────────────────────────────────────────── */

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
    el("label", {}, [el("span", { text: t("face.size") }), sizePicker]),
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

  function setSize(value) {
    state.size = clampSize(value);
    remember(SIZE_KEY, state.size);
    // Written on the element rather than the stylesheet: this is one reader's
    // preference about one face, not a retune of the interface. How much of
    // it survives a narrow column is the stylesheet's business.
    shell.style.setProperty("--face-size", `${state.size}px`);
    repaint();
  }

  setSize(state.size);

  stage.append(portrait, dock, settingsToggle, settings);

  /* ── What it is doing, in words ─────────────────────────────────── */

  /* The face draws the assistant's own reading; this says what the page
     knows, which is a different fact the moment the connection drops. It is
     also what a reader who cannot tell an idle disc from a listening one is
     actually reading. */
  function paintPhase(phase, connected) {
    const known = phase && t(`phase.${phase}`) !== `phase.${phase}`;
    const label = connected === false
      ? t("common.reconnecting")
      : known ? t(`phase.${phase}`) : t("phase.offline");
    canvas.setAttribute("aria-label", label);
    stateNode.replaceChildren(
      el("span", { class: `state-pill${phase === "idle" ? "" : " live"}` }, [
        el("span", { class: "state-pill-dot", dataset: { phase: phase || "offline" } }),
        el("span", { text: label }),
      ]),
    );
  }

  /* Coming back to the page asks at once rather than waiting out the tick
     that was skipped, so a face returned to is correct immediately. */
  function onVisibility() {
    if (!document.hidden) takeReading();
  }
  document.addEventListener("visibilitychange", onVisibility);

  paintPhase("idle");
  startPainting();
  takeReading();
  polling = setInterval(takeReading, POLL_MS);

  return {
    paintPhase,
    setName(name) {
      if (!name) return;
      state.name = name;
      nameNode.textContent = name;
    },
    micButton: mic,
    destroy() {
      stopPainting();
      clearInterval(polling);
      document.removeEventListener("visibilitychange", onVisibility);
    },
  };
}
