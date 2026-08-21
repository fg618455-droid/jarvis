/* The browser end of the microphone: capture, frame, and stream to the daemon.
 *
 * Frames go out as they are captured rather than after the utterance ends.
 * Endpointing already happens on the daemon side, and waiting for it here
 * would add the length of whatever was said to every single reply.
 */

const SAMPLE_RATE = 16000;
// 20 ms, matching the listener's default VAD frame. Smaller frames buy no
// responsiveness (the daemon batches into VAD frames anyway) and cost a
// WebSocket message each.
const FRAME_SAMPLES = 320;

function socketUrl() {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/api/voice/stream${location.search}`;
}

/** Linear resample to the listener's rate, for browsers that ignore the
 *  requested context rate. Quality is adequate for speech at these ratios
 *  and the alternative is refusing to work at all on those browsers. */
function resample(input, fromRate, toRate) {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const out = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const at = i * ratio;
    const low = Math.floor(at);
    const high = Math.min(low + 1, input.length - 1);
    out[i] = input[low] + (input[high] - input[low]) * (at - low);
  }
  return out;
}

export function createMic({ onState, onError } = {}) {
  let ctx = null;
  let stream = null;
  let node = null;
  let socket = null;
  let running = false;

  const setState = (state) => { if (onState) onState(state); };

  async function start() {
    if (running) return;
    running = true;
    setState("starting");

    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          // Jarvis suppresses its own speech by other means, but the
          // browser's own processing still helps in a room with speakers.
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });

      ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
      await ctx.audioWorklet.addModule("/static/js/mic-worklet.js");

      // A browser may hand back a context at its own rate regardless of what
      // was asked for. Resample on the main thread in that case so the
      // worklet only ever sees the rate the daemon expects.
      const needsResample = ctx.sampleRate !== SAMPLE_RATE;

      socket = new WebSocket(socketUrl());
      socket.binaryType = "arraybuffer";
      await new Promise((resolve, reject) => {
        socket.addEventListener("open", resolve, { once: true });
        socket.addEventListener("error", () => reject(new Error("socket refused")), { once: true });
      });

      const source = ctx.createMediaStreamSource(stream);
      node = new AudioWorkletNode(ctx, "mic-frame-processor", {
        processorOptions: { frameSamples: needsResample ? FRAME_SAMPLES * 4 : FRAME_SAMPLES },
      });

      node.port.onmessage = (event) => {
        if (!socket || socket.readyState !== WebSocket.OPEN) return;
        let buffer = event.data;
        if (needsResample) {
          const asFloat = Int16Array.from(new Int16Array(buffer), (v) => v / 0x8000);
          const resampled = resample(asFloat, ctx.sampleRate, SAMPLE_RATE);
          const pcm = new Int16Array(resampled.length);
          for (let i = 0; i < resampled.length; i++) {
            const s = Math.max(-1, Math.min(1, resampled[i]));
            pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
          }
          buffer = pcm.buffer;
        }
        socket.send(buffer);
      };

      source.connect(node);
      // A worklet with no destination is not pulled in every browser. Route
      // it through a silent gain node so the graph keeps rendering without
      // playing the microphone back into the room.
      const mute = ctx.createGain();
      mute.gain.value = 0;
      node.connect(mute).connect(ctx.destination);

      socket.addEventListener("close", () => { if (running) stop(); });
      setState("listening");
    } catch (err) {
      running = false;
      await teardown();
      setState("idle");
      if (onError) onError(err);
      throw err;
    }
  }

  async function teardown() {
    try { node?.port && (node.port.onmessage = null); } catch { /* already gone */ }
    try { node?.disconnect(); } catch { /* already gone */ }
    try { stream?.getTracks().forEach((track) => track.stop()); } catch { /* already gone */ }
    try { socket?.close(); } catch { /* already gone */ }
    try { await ctx?.close(); } catch { /* already gone */ }
    node = null; stream = null; socket = null; ctx = null;
  }

  async function stop() {
    if (!running) return;
    running = false;
    await teardown();
    setState("idle");
  }

  return {
    start,
    stop,
    toggle: () => (running ? stop() : start()),
    get running() { return running; },
  };
}
