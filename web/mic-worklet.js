// Microphone -> 48 kHz mono 16-bit PCM in 20 ms frames, which is what /api/call listens to.
// Runs at whatever rate the browser's audio context has; resamples if that isn't 48 kHz.
const FRAME = 960;

class Mic extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / 48000;
    this.pos = 0;
    this.last = 0;
    this.frame = new Int16Array(FRAME);
    this.fill = 0;
  }

  push(sample) {
    const clipped = Math.max(-1, Math.min(1, sample));
    this.frame[this.fill++] = clipped < 0 ? clipped * 32768 : clipped * 32767;
    if (this.fill === FRAME) {
      const out = this.frame.buffer;
      this.port.postMessage(out, [out]);
      this.frame = new Int16Array(FRAME);
      this.fill = 0;
    }
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    while (this.pos < input.length) {
      const i = Math.floor(this.pos);
      const before = i === 0 ? this.last : input[i - 1];
      const t = this.pos - i;
      this.push(before + (input[i] - before) * t);
      this.pos += this.step;
    }
    this.pos -= input.length;
    this.last = input[input.length - 1];
    return true;
  }
}

registerProcessor("mic", Mic);
