/* Audio worklet: microphone samples in, 16-bit PCM frames out.
 *
 * Runs on the audio thread, so it does the least it can get away with:
 * clamp, scale to 16-bit, and hand whole frames to the main thread. No
 * allocation per render quantum beyond the frame being filled, because a
 * late return here is a gap in what Jarvis hears.
 *
 * The graph is built at the listener's sample rate, so there is no
 * resampling to do. When a browser refuses that rate the main thread
 * resamples before the audio ever reaches this node.
 */

class MicFrameProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const frameSamples = options?.processorOptions?.frameSamples ?? 320;
    this._frame = new Int16Array(frameSamples);
    this._filled = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      // Clamp before scaling: values outside [-1, 1] are legal in Web Audio
      // and would wrap into loud noise once cast to 16-bit.
      const sample = Math.max(-1, Math.min(1, channel[i]));
      this._frame[this._filled++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;

      if (this._filled === this._frame.length) {
        // Transfer a copy: the frame keeps filling while this one travels.
        const out = this._frame.slice();
        this.port.postMessage(out.buffer, [out.buffer]);
        this._filled = 0;
      }
    }
    return true;
  }
}

registerProcessor("mic-frame-processor", MicFrameProcessor);
