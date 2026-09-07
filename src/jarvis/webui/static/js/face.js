/* The face: the centre of the interface rather than a page inside it.

   A reactor: a hot core inside a casing, with coils, filaments and motes in
   the space between the two. What the assistant is doing is carried by the
   size of the core, which is a channel that survives a reader who has asked
   for no motion: with every animation off, idle, listening, thinking and
   speaking are still four different pictures. A face that separated its
   states by the speed of a rotation would have nothing left to say to that
   reader, and the state is the one thing this drawing exists to report.

   Everything else the reactor does is how hard it is running rather than what
   it is doing: one energy figure sets the brightness of the coils, the speed
   of the machinery, how many motes are in the air and how often the casing
   throws a ripple. That is a second reading of the same state, which is why
   it is allowed to be carried in motion alone.

   It is painted from `var(--accent)`, read off the stylesheet rather than
   held here, so a theme drives the face for free and there is no second
   palette to keep in step with the first. The core is the accent taken
   towards white, which is light on the same colour rather than a colour of
   its own.

   The reading comes from `/api/visualizer/state`, which derives everything
   from Jarvis's own live objects. The words beside the face come from the
   event stream instead, because that is the only source that can say the
   page has lost the daemon: a poll that fails and a daemon that is idle look
   identical from here. */

import { api } from "./api.js";
import { t } from "./i18n.js";
import { displayPhase, phaseLabel } from "./phase.js";
import { el, icon, ICONS, motionAllowed } from "./ui.js";

const SIZE_KEY = "jarvis.faceSize";

const MIN_SIZE = 180;
const MAX_SIZE = 560;
const DEFAULT_SIZE = 400;

/* Eight times a second is what a mouth needs to agree with speech, and it is
   far more than anything else here needs: idle, listening and thinking change
   on a human timescale and are announced on the event stream the moment they
   change. So the fast rate is spent only while there is a waveform to follow,
   and the deck nudges the face to read again whenever the phase changes, so a
   state still arrives at once rather than up to the slow interval late.

   It is not a free choice. The server closes every connection it answers, so
   a poll is a new socket that the operating system then holds for minutes;
   eight a second, all day, exhausts the pool. */
const POLL_SPEAKING_MS = 125;
const POLL_IDLE_MS = 400;

/* How large the core is drawn, as a share of the casing it sits in. This
   table is the state contract: four values far enough apart to be told apart
   in a still picture across a room. */
const DISC = {
  idle: 0.52,
  listening: 0.80,
  thinking: 0.52,
  speaking: 0.66,
};

/* How hard the reactor is running in each state, which is what everything
   that moves is scaled by. Idle is deliberately not zero: a reactor at rest
   is still lit, and a face that went dark between two questions would read as
   a daemon that had stopped. */
const ENERGY = {
  idle: 0.34,
  listening: 0.72,
  thinking: 0.88,
  speaking: 1.0,
};

/* How fast the coils turn, in radians a second. Thinking is the quick one
   because it is the state a reader is most often waiting through, and the one
   where the speed is telling them something they want to know. */
const SPIN = {
  idle: 0.12,
  listening: 0.26,
  thinking: 0.70,
  speaking: 0.38,
};

/* How far a waveform may push the edge of the core, as a share of its radius.
   Past roughly a tenth the outline stops reading as a circle that is speaking
   and starts reading as a shape that is not a circle. */
const SPEECH_DEFORMATION = 0.07;

/* The reactor's geometry, every radius a share of the casing ring. The coils
   sit just inside the casing so that the widest core the face ever draws
   still clears them, and the ripples and the motes live outside it, where
   nothing they do can be mistaken for the core changing size. */
const COIL_INNER = 0.845;
const COIL_OUTER = 0.945;
const COIL_COUNT = 12;
const COIL_GAP = 0.09; // radians left dark between one coil and the next
const TICK_RING = 1.16;
const TICK_COUNT = 60;
const FILAMENT_COUNT = 24;
const RIPPLE_COUNT = 3;
const RIPPLE_REACH = 1.30;

/* How quickly the core follows a change of state and the reactor follows a
   change of energy, as a share of the gap closed per second. Fast enough that
   a state has visibly arrived within a breath, slow enough that the core is
   seen opening rather than found already open. */
const SETTLE = 7;

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

export function mountFace(stage, { onSend, onMicToggle, onConversationToggle } = {}) {
  const state = {
    size: clampSize(remembered(SIZE_KEY, DEFAULT_SIZE)),
    reading: "idle",
    wave: null,
    level: 0,
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

  /* Reading a custom property is a style resolution, and every layer of the
     reactor wants the accent at a different opacity, so the colour is
     resolved to three numbers once and every shade after that is arithmetic.
     The theme is the only thing that changes it and it announces itself on
     the root element, so the answer is kept until that changes.

     A one-pixel canvas does the resolving, because it accepts every notation
     a theme might have written the accent in: a hex, an `rgb()` and an
     `oklch()` all come back as three channels. */
  const probe = document.createElement("canvas");
  probe.width = 1;
  probe.height = 1;
  const probeContext = probe.getContext("2d", { willReadFrequently: true });

  let paintedFor = null;
  let channels = [255, 255, 255];
  function readAccent() {
    const theme = document.documentElement.dataset.theme;
    if (theme === paintedFor) return;
    paintedFor = theme;
    const declared = getComputedStyle(document.documentElement)
      .getPropertyValue("--accent")
      .trim();
    if (!declared) return;
    probeContext.clearRect(0, 0, 1, 1);
    probeContext.fillStyle = declared;
    probeContext.fillRect(0, 0, 1, 1);
    const pixel = probeContext.getImageData(0, 0, 1, 1).data;
    /* A notation the browser cannot parse leaves the pixel untouched, and a
       face painted in nothing at all is worse than one still wearing the
       palette it already had. */
    if (pixel[3] > 0) channels = [pixel[0], pixel[1], pixel[2]];
  }

  /* The accent at an opacity. */
  function paint(alpha) {
    return `rgba(${channels[0]}, ${channels[1]}, ${channels[2]}, ${alpha})`;
  }

  /* The accent taken towards white, which is how hot a part of the reactor is
     rather than a second colour: the core glows, it does not change hue. */
  function heat(towards, alpha) {
    const hotter = channels.map((value) => Math.round(value + (255 - value) * towards));
    return `rgba(${hotter[0]}, ${hotter[1]}, ${hotter[2]}, ${alpha})`;
  }

  /* What the machinery has turned through so far, rather than the angle a
     clock says it should be at. Speed changes with the state, and an angle
     computed from the time would jump the moment the multiplier changed:
     everything that turns integrates instead, so a reactor spooling up
     accelerates rather than skipping forward.

     None of it advances while motion is refused, which is what makes a still
     face genuinely still rather than slowly moving. */
  const turned = { coils: 0, ticks: 0, filaments: 0, motes: 0, ripples: 0, mark: 0 };
  let charge = 0;
  let breathed = 0;
  let openness = null;
  let running = null;
  let last = null;

  /* Where one mote sits in the ring of them, from its index alone: a
     golden-ratio walk, so a dozen of them take a dozen different lanes and
     speeds without a table saying so and without two ever pairing up. */
  function moteAt(index) {
    const spread = (index * 0.618) % 1;
    const pace = 0.35 + ((index * 0.37) % 1) * 0.9;
    return { spread, pace, lane: index % 2 ? 1 : -1 };
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
    readAccent();
    const reading = state.reading;
    const ring = size * 0.40;

    /* How much time this frame may move things on by. A tab that was hidden
       for a minute comes back to one frame rather than to a minute of
       rotation applied at once, so the reactor is where it was left rather
       than spun round while nobody was looking. */
    const step = moving && last !== null ? Math.min(0.05, Math.max(0, seconds - last)) : 0;
    last = moving ? seconds : null;

    /* The state, and how hard the reactor is running it, both eased. A core
       that jumped between two sizes would report the state as a cut rather
       than as a change. With motion refused they snap, because there the
       still picture has to be the state exactly. */
    const wantOpen = DISC[reading];
    const wantRunning = ENERGY[reading];
    if (!moving || openness === null) {
      openness = wantOpen;
      running = wantRunning;
    } else {
      const settle = Math.min(1, step * SETTLE);
      openness += (wantOpen - openness) * settle;
      running += (wantRunning - running) * settle;
    }

    const spin = SPIN[reading];
    turned.coils += step * spin;
    turned.ticks -= step * spin * 0.34;
    turned.filaments -= step * spin * 0.62;
    turned.motes += step * 0.30;
    turned.ripples += step * (0.22 + running * 0.45);
    turned.mark += step * 1.5;
    charge += step * (1.1 + running * 1.6);
    breathed += step * 1.15;

    const energy = running;
    const level = reading === "speaking" ? Math.min(1, state.level || 0) : 0;

    context.save();
    context.translate(size / 2, size / 2);

    /* ── The light it sits in ─────────────────────────────────────────── */

    /* So the centre of the page reads as depth rather than as a sticker on a
       flat surface, and so the reactor is a source of light rather than a
       shape that happens to be bright. */
    const glow = 0.10 + energy * 0.10 + level * 0.05;
    const aura = context.createRadialGradient(0, 0, ring * 0.3, 0, 0, ring * 1.9);
    aura.addColorStop(0, paint(glow));
    aura.addColorStop(0.45, paint(glow * 0.34));
    aura.addColorStop(1, "rgba(0, 0, 0, 0)");
    context.fillStyle = aura;
    context.beginPath();
    context.arc(0, 0, ring * 1.9, 0, Math.PI * 2);
    context.fill();

    /* ── The casing ──────────────────────────────────────────────────── */

    /* The outer graduations. Fine, dim, and slowly counter-turning: they are
       the instrument the reactor is mounted in rather than part of it, and
       they are what makes the whole drawing read as machined. */
    context.save();
    context.rotate(turned.ticks);
    context.strokeStyle = paint(0.10 + energy * 0.12);
    context.lineWidth = Math.max(1, size * 0.003);
    for (let index = 0; index < TICK_COUNT; index += 1) {
      const angle = (index / TICK_COUNT) * Math.PI * 2;
      const from = ring * TICK_RING;
      const to = from + ring * (index % 5 === 0 ? 0.055 : 0.025);
      context.beginPath();
      context.moveTo(Math.cos(angle) * from, Math.sin(angle) * from);
      context.lineTo(Math.cos(angle) * to, Math.sin(angle) * to);
      context.stroke();
    }
    context.restore();

    /* The casing ring. It never changes, so it is the fixed thing the core
       inside it is read against: how open the face is, is how much of the
       casing the core has taken. */
    context.strokeStyle = paint(0.32);
    context.lineWidth = Math.max(1, size * 0.005);
    context.beginPath();
    context.arc(0, 0, ring, 0, Math.PI * 2);
    context.stroke();

    /* ── The coils ───────────────────────────────────────────────────── */

    /* Twelve segments in the band between the core and the casing, turning,
       with a charge running round them. This band is what stops the face
       reading as a coloured circle: it is lit machinery, and it is lit in the
       still picture too, because how brightly it burns is the state as well. */
    const arc = (Math.PI * 2) / COIL_COUNT;
    for (let index = 0; index < COIL_COUNT; index += 1) {
      const from = turned.coils + index * arc;
      const pulse = Math.max(0, Math.sin(charge - index * 0.52));
      const lit = 0.34 + energy * 0.20 + Math.pow(pulse, 6) * (0.28 + level * 0.2);
      context.fillStyle = paint(lit);
      context.beginPath();
      context.arc(0, 0, ring * COIL_OUTER, from, from + arc - COIL_GAP);
      context.arc(0, 0, ring * COIL_INNER, from + arc - COIL_GAP, from, true);
      context.closePath();
      context.fill();
    }

    /* ── The core ────────────────────────────────────────────────────── */

    /* Idle breathes, because a reactor that is perfectly still reads as a
       picture of an assistant rather than as one that is running. */
    const breath = reading === "idle" && moving ? 1 + 0.025 * Math.sin(breathed) : 1;
    const radius = ring * openness * breath;

    /* Filaments: hairlines reaching from the core out towards the coils, the
       other way round the ring. Only where there is room for them, so the
       widest core never has them crossing it. */
    const filamentFrom = radius * 1.06;
    const filamentTo = ring * (COIL_INNER - 0.02);
    if (filamentTo > filamentFrom) {
      context.save();
      context.rotate(turned.filaments);
      context.strokeStyle = paint(0.07 + energy * 0.09);
      context.lineWidth = Math.max(1, size * 0.002);
      for (let index = 0; index < FILAMENT_COUNT; index += 1) {
        const angle = (index / FILAMENT_COUNT) * Math.PI * 2;
        context.beginPath();
        context.moveTo(Math.cos(angle) * filamentFrom, Math.sin(angle) * filamentFrom);
        context.lineTo(Math.cos(angle) * filamentTo, Math.sin(angle) * filamentTo);
        context.stroke();
      }
      context.restore();
    }

    /* The halo the core casts on the space just outside it, painted before
       the core so the body of the core stays the accent exactly: what a
       reader matches against the rest of the interface is that colour, and a
       glow laid over it would leave the largest object on the page reading as
       a shade nothing else in the palette has. */
    const halo = context.createRadialGradient(0, 0, radius * 0.92, 0, 0, radius * 1.9);
    halo.addColorStop(0, paint(0.16 + energy * 0.16 + level * 0.12));
    halo.addColorStop(1, "rgba(0, 0, 0, 0)");
    context.fillStyle = halo;
    context.beginPath();
    context.arc(0, 0, radius * 1.9, 0, Math.PI * 2);
    context.fill();

    /* The core itself: hot at the centre, the accent from a little over half
       its radius outwards. The waveform pushes its edge while it speaks. */
    const core = context.createRadialGradient(0, 0, 0, 0, 0, radius);
    core.addColorStop(0, heat(0.78, 1));
    core.addColorStop(0.16, heat(0.52, 1));
    core.addColorStop(0.34, heat(0.18, 1));
    core.addColorStop(0.55, paint(1));
    core.addColorStop(1, paint(1));
    context.fillStyle = core;

    const wave = reading === "speaking" && moving ? smoothed(state.wave || []) : null;
    context.beginPath();
    if (wave) {
      const steps = 160;
      for (let point = 0; point <= steps; point += 1) {
        const angle = (point / steps) * Math.PI * 2;
        const at = (point / steps) * wave.length;
        const low = Math.floor(at) % wave.length;
        const high = (low + 1) % wave.length;
        const blend = at - Math.floor(at);
        const value = wave[low] * (1 - blend) + wave[high] * blend;
        const reach = radius * (1 + (value - 0.5) * 2 * SPEECH_DEFORMATION);
        const x = Math.cos(angle) * reach;
        const y = Math.sin(angle) * reach;
        if (point === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      }
      context.closePath();
    } else {
      context.arc(0, 0, radius, 0, Math.PI * 2);
    }
    context.fill();

    /* The rim, and the plating rings inside the hot centre. Both are drawn as
       light added to what is already there, so they brighten the core without
       repainting any of it in a colour of their own. */
    context.save();
    context.globalCompositeOperation = "lighter";
    context.strokeStyle = heat(0.85, 0.20 + energy * 0.20);
    context.lineWidth = Math.max(1, size * 0.006);
    context.stroke();
    context.strokeStyle = heat(1, 0.10 + energy * 0.06);
    context.lineWidth = Math.max(1, size * 0.0025);
    for (const at of [0.30, 0.44]) {
      context.beginPath();
      context.arc(0, 0, radius * at, 0, Math.PI * 2);
      context.stroke();
    }
    context.restore();

    /* ── What it throws off ──────────────────────────────────────────── */

    /* Ripples leaving the core, or arriving at it. Listening is the one state
       where the reactor is taking something in rather than giving it out, and
       running the rings inwards is what that looks like. */
    for (let index = 0; index < RIPPLE_COUNT; index += 1) {
      const raw = (turned.ripples + index / RIPPLE_COUNT) % 1;
      const phase = reading === "listening" ? 1 - raw : raw;
      const at = radius * 1.02 + (ring * RIPPLE_REACH - radius * 1.02) * phase;
      const strength = reading === "listening" ? raw : 1 - raw;
      context.strokeStyle = paint(strength * (0.10 + energy * 0.22 + level * 0.14));
      context.lineWidth = Math.max(1, size * (0.001 + 0.004 * strength));
      context.beginPath();
      context.arc(0, 0, at, 0, Math.PI * 2);
      context.stroke();
    }

    /* Motes in the air around the casing. They are the cheapest way to say
       the reactor is doing work, and there are more of them in the air the
       harder it is working. */
    const motes = 4 + Math.round(energy * 9);
    context.save();
    context.globalCompositeOperation = "lighter";
    for (let index = 0; index < motes; index += 1) {
      const { spread, pace, lane } = moteAt(index);
      const angle = index * 2.399 + turned.motes * pace * lane;
      const at = ring * (1.02 + spread * 0.26) + Math.sin(charge * 0.4 + index) * ring * 0.015;
      context.fillStyle = heat(0.35, 0.22 + spread * 0.3);
      context.beginPath();
      context.arc(
        Math.cos(angle) * at,
        Math.sin(angle) * at,
        Math.max(1, size * (0.0035 + spread * 0.004)),
        0,
        Math.PI * 2,
      );
      context.fill();
    }
    context.restore();

    /* Thinking puts one bright mark on the casing and walks it round, with a
       tail behind it. It is the only thing drawn on the casing ring itself,
       so it stays legible against everything else that turns, and it is
       parked at the top when nothing may move, so the state is still a
       different picture in a still one. */
    if (reading === "thinking") {
      context.lineCap = "round";
      const from = moving ? turned.mark : -Math.PI / 2;
      const span = Math.PI * 0.45;
      const tail = [[0, 1, 0.011], [0.8, 0.35, 0.007], [1.5, 0.15, 0.005]];
      for (const [back, alpha, width] of tail) {
        context.strokeStyle = paint(alpha);
        context.lineWidth = Math.max(2, size * width);
        context.beginPath();
        context.arc(0, 0, ring, from - back * span, from - back * span + span);
        context.stroke();
      }
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

  /* Draw once, for the cases where nothing else will. With motion allowed
     the loop is already redrawing sixty times a second and this is a no-op;
     with motion refused there is no loop, so a new reading, a resize or a
     change of theme is the only thing that ever repaints. */
  function repaintIfStill() {
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
  let pollRate = null;

  function pollEvery(ms) {
    if (pollRate === ms) return;
    pollRate = ms;
    clearInterval(polling);
    polling = setInterval(takeReading, ms);
  }

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
      state.level = named === "speaking" ? reading.level || 0 : 0;
      pollEvery(named === "speaking" ? POLL_SPEAKING_MS : POLL_IDLE_MS);
      if (changed) repaintIfStill();
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

  /* How loud what the microphone is sending is, beside the button that
     opened it. It is painted from JavaScript, so the stylesheet's
     reduced-motion rule cannot reach it: it asks for itself, and when the
     answer is no it is not built at all. Nothing is carried by it alone —
     whether the microphone is open is on the button, in its pressed state
     and in its accessible name. */
  const level = motionAllowed()
    ? el("span", { class: "face-mic-level", "aria-hidden": "true" }, [el("span")])
    : null;

  /* Holding the follow-up window open, so no question needs the wake word.
     It is a switch on the thing you are talking to rather than a setting,
     because it is turned on for the next few minutes and off again after
     them, and because the assistant also ends it on its own when it is asked
     to stop. Its state therefore follows the runtime and never the last
     thing this page clicked. */
  const conversation = el(
    "button",
    {
      type: "button",
      class: "dock-button face-conversation",
      "aria-label": t("conversation.modeTitle"),
      "aria-pressed": "false",
      onclick: () => onConversationToggle && onConversationToggle(),
    },
    [icon(ICONS.conversation)],
  );

  const dock = el("div", { class: "face-dock" }, [
    mic, level, input, conversation, send,
  ]);

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
    repaintIfStill();
  }

  setSize(state.size);

  /* The three bands between the face and the thing you type into. The stage
     owns the order they are in; what goes in each is somebody else's
     business and the deck's to fill.

     The glance and the exchange are the same room used twice. At rest the
     face is the largest thing on the page and one line under it says what
     would otherwise cost four panels to find out; once something has been
     said, that line has been answered and the conversation takes the room
     instead. Only one of the two is ever in the layout, so neither is a
     band of empty box waiting for the other. */
  const glance = el("div");
  const exchange = el("div");
  const gate = el("div");

  /* Which of the two the stage is showing. Written where a stylesheet can
     see it, because what changes between them is the layout rather than the
     contents of anything. */
  stage.dataset.stage = "resting";
  function setConversing(is) {
    stage.dataset.stage = is ? "talking" : "resting";
    // The face is capped against the room the exchange left it, and the
    // drawing is made at whatever width that settles on. With motion refused
    // nothing else would repaint it at the new size.
    repaintIfStill();
  }

  // In the order they are read. The size control sits at the top of the
  // stage and the dock along the bottom of it, so appending the control last
  // put a keyboard on it after the thing below it.
  stage.append(settingsToggle, settings, portrait, glance, exchange, gate, dock);

  /* ── What it is doing, in words ─────────────────────────────────── */

  /* The face draws the assistant's own reading; this says what the page
     knows, which is a different fact the moment the connection drops. It is
     also what a reader who cannot tell an idle disc from a listening one is
     actually reading. */
  function paintPhase(phase, reading) {
    const label = phaseLabel(phase, reading);
    const shown = displayPhase(phase, reading);
    canvas.setAttribute("aria-label", label);
    stateNode.replaceChildren(
      el("span", { class: `state-pill${shown === "idle" ? "" : " live"}` }, [
        el("span", { class: "state-pill-dot", dataset: { phase: shown || "offline" } }),
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

  /* A theme changes the accent the face is painted in, and with motion
     refused there is no loop to notice: the page around the face would
     change colour and the largest thing on it would keep the palette it was
     first drawn in. The attribute that carries the theme is watched instead. */
  const themeWatch = new MutationObserver(() => repaintIfStill());
  themeWatch.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });

  paintPhase("idle");
  startPainting();
  takeReading();
  pollEvery(POLL_IDLE_MS);

  return {
    paintPhase,
    /* The deck calls this when the daemon announces a new phase, so a state
       change is drawn at once rather than at the next slow tick. */
    poll: takeReading,
    setName(name) {
      if (!name) return;
      state.name = name;
      nameNode.textContent = name;
    },
    micButton: mic,
    micLevel: level,
    conversationButton: conversation,
    /* The bands between the face and the dock, for the deck to fill. */
    glanceSlot: glance,
    exchangeSlot: exchange,
    gateSlot: gate,
    setConversing,
    destroy() {
      stopPainting();
      clearInterval(polling);
      themeWatch.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
    },
  };
}
