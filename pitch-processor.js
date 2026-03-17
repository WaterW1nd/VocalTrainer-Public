/**
 * pitch-processor.js — AudioWorkletProcessor for real-time YIN pitch detection.
 * Must be placed in the same folder as index.html.
 * Loaded via: audioContext.audioWorklet.addModule('pitch-processor.js')
 *
 * The processor accumulates samples into a ring buffer, then every ~40 ms
 * runs the YIN algorithm on a 2048-sample window and posts the result.
 *
 * Reference: de Cheveigné & Kawahara (2002). YIN, a fundamental frequency
 * estimator for speech and music. JASA 111(4).
 */

// ─── Constants ────────────────────────────────────────────────────────────────
const YIN_THRESHOLD          = 0.10;   // lower = more strict; 0.10 is a good default
const YIN_BUFFER_SIZE        = 2048;   // must be a power-of-2; window for analysis
const POST_INTERVAL_SAMPLES  = 1764;   // post every ~40 ms at 44100 Hz
const SILENCE_RMS_FLOOR      = 0.01;   // RMS below this is treated as silence
const MIDI_NOTE_MIN          = 24;     // C1 — lower bound of accepted vocal range
const MIDI_NOTE_MAX          = 96;     // C7 — upper bound of accepted vocal range
const A4_HZ                  = 440;    // reference pitch for MIDI conversion
const A4_MIDI                = 69;     // MIDI note number of A4

class PitchProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer    = new Float32Array(YIN_BUFFER_SIZE);
    this._writeIdx  = 0;
    this._filled    = false;      // true once the ring buffer has been filled once
    this._countdown = POST_INTERVAL_SAMPLES;
    this._threshold = YIN_THRESHOLD;

    // Receive threshold updates from main thread
    this.port.onmessage = (e) => {
      if (e.data && e.data.type === 'threshold') {
        this._threshold = e.data.value;
      }
    };
  }

  // ─── Main process callback (called by audio engine every render quantum) ───
  /**
   * @param {Float32Array[][]} inputs
   * @returns {boolean}
   */
  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const samples = input[0]; // mono; if stereo only channel 0 is used

    for (let i = 0; i < samples.length; i++) {
      this._buffer[this._writeIdx] = samples[i];
      this._writeIdx = (this._writeIdx + 1) % YIN_BUFFER_SIZE;
      if (this._writeIdx === 0) this._filled = true;

      this._countdown--;
      if (this._countdown <= 0 && this._filled) {
        this._countdown = POST_INTERVAL_SAMPLES;
        this._analyzeAndPost();
      }
    }
    return true; // keep processor alive
  }

  // ─── Analysis: copy ring buffer into linear order, then run YIN ───────────
  /** @returns {void} */
  _analyzeAndPost() {
    // Reconstruct linear buffer from ring
    const linear = new Float32Array(YIN_BUFFER_SIZE);
    const start  = this._writeIdx; // oldest sample index
    for (let i = 0; i < YIN_BUFFER_SIZE; i++) {
      linear[i] = this._buffer[(start + i) % YIN_BUFFER_SIZE];
    }

    // Compute RMS to detect silence
    let rms = 0;
    for (let i = 0; i < YIN_BUFFER_SIZE; i++) rms += linear[i] * linear[i];
    rms = Math.sqrt(rms / YIN_BUFFER_SIZE);

    if (rms < SILENCE_RMS_FLOOR) {
      this.port.postMessage({ hz: 0, midiNote: -1, rms });
      return;
    }

    const hz = this._yin(linear);
    let midiNote = -1;
    if (hz > 0) {
      midiNote = Math.round(A4_MIDI + 12 * Math.log2(hz / A4_HZ));
      if (midiNote < MIDI_NOTE_MIN || midiNote > MIDI_NOTE_MAX) midiNote = -1;
    }

    this.port.postMessage({ hz, midiNote, rms });
  }

  // ─── YIN core ─────────────────────────────────────────────────────────────
  /**
   * @param {Float32Array} buf
   * @returns {number} Detected fundamental frequency in Hz, or 0 if none found.
   */
  _yin(buf) {
    const N    = buf.length;
    const half = N >> 1;
    const yin  = new Float32Array(half);

    // Step 1: Difference function
    for (let tau = 0; tau < half; tau++) {
      let sum = 0;
      for (let j = 0; j < half; j++) {
        const d = buf[j] - buf[j + tau];
        sum += d * d;
      }
      yin[tau] = sum;
    }

    // Step 2: Cumulative mean normalised difference
    yin[0] = 1;
    let running = 0;
    for (let tau = 1; tau < half; tau++) {
      running += yin[tau];
      yin[tau] = (yin[tau] * tau) / running;
    }

    // Step 3: Absolute threshold — find first tau where yin < threshold
    let tau = -1;
    for (let t = 2; t < half; t++) {
      if (yin[t] < this._threshold) {
        while (t + 1 < half && yin[t + 1] < yin[t]) t++;
        tau = t;
        break;
      }
    }

    if (tau === -1) return 0; // no pitch found

    // Step 4: Parabolic interpolation for sub-sample accuracy
    let betterTau = tau;
    if (tau > 0 && tau < half - 1) {
      const s0 = yin[tau - 1];
      const s1 = yin[tau];
      const s2 = yin[tau + 1];
      const d  = 2 * s1 - s0 - s2;
      if (d !== 0) betterTau = tau + (s2 - s0) / (2 * d);
    }

    return sampleRate / betterTau;
  }
}

registerProcessor('pitch-processor', PitchProcessor);
