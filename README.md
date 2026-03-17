# Karaoke Vocal Trainer

A local, browser-based vocal training tool that plays your backing track and MIDI vocal melody while listening to your microphone, scores your pitch accuracy in real time, and shows you exactly which notes you hit, nearly hit, or missed. Built entirely in vanilla JavaScript with the Web Audio API and Canvas 2D — no framework, no server, no dependencies.

## Demo

<img width="1920" height="1109" alt="image" src="https://github.com/user-attachments/assets/e5440c6e-e47b-4384-ad03-097b91349b32" />


> The main screen shows a scrolling note-roll canvas (notes in blue/green/yellow moving right toward a vertical hit line), a glowing purple pitch ball tracking your live voice, an overview strip at the bottom, and the score/accuracy counters in the top bar.

## Features

- Drag-and-drop or file-picker loading of WAV/MP3 audio (multiple files mixed automatically) and a MIDI file with the vocal melody
- Real-time YIN pitch detection via `AudioWorkletProcessor` (~40 ms latency), with `ScriptProcessorNode` fallback for older browsers
- Scrolling note-roll canvas with colour-coded verdicts: green = hit, yellow = near, dark = miss
- Pitch ball that tracks your live voice with smooth interpolation
- Scoring: hit (≤1 semitone off) = 100 pts, near (≤2 semitones off) = 50 pts, miss = 0 pts; final accuracy shown as a percentage and star rating (1–3 stars)
- Lyrics support: paste raw text, auto-distributed across notes; shown on note tiles and in a resizable side panel
- Per-note lyric editor to fine-tune text assignments
- Transpose slider (−6 to +6 semitones) and practice speed mode (75%)
- Metronome with configurable BPM using Web Audio API scheduled oscillators
- Session recording: saves mixed audio (phonogram + mic) and mic-only track as `.webm`
- Save/load entire session as a `.vkt` project file (includes MIDI, audio, lyrics, and note bindings)
- Overview strip with click-to-seek navigation; keyboard seek with arrow keys
- Shareable results PNG screenshot (640×360)
- Input/output device selection (Chrome 110+)

## Requirements

- **Browser:** Chrome 110+ (recommended for full feature set), Firefox 115+
- **Microphone:** connected and accessible to the browser
- **MIDI file:** a `.mid` file containing the vocal melody (any channel; all channels are parsed)
- **Audio file(s):** WAV (most reliable) or MP3 — one or more files are mixed together

## Quick Start

1. Serve the project folder over HTTP:
   ```
   python -m http.server 8080
   ```
2. Open `http://localhost:8080` in Chrome 110+ and grant microphone permission when prompted.
3. Drop your audio file(s) onto the **Аудио-фонограмма** zone and your MIDI file onto the **MIDI вокальная партия** zone, then click **▶ Начать тренировку**.

> The app cannot run from `file://` — AudioWorklet requires HTTP or HTTPS.

## How It Works

### Input Processing

The MIDI file is parsed as raw binary: the app reads all tracks, builds a tempo map from `0xFF 0x51` meta-events, converts delta-ticks to seconds using piecewise tempo interpolation, and merges consecutive same-pitch notes separated by ≤ 0.15 s. Audio files are decoded with `AudioContext.decodeAudioData()` and played simultaneously through a shared `GainNode`.

### Real-Time Pitch Detection

Microphone audio is routed to `pitch-processor.js`, an `AudioWorkletProcessor` that accumulates samples in a 2048-sample ring buffer and runs the YIN algorithm every ~40 ms. YIN computes the cumulative mean normalised difference function, finds the first minimum below threshold 0.10, and applies parabolic interpolation for sub-sample accuracy. The detected frequency is converted to a MIDI note number and compared against the active MIDI note (after applying any transpose offset).

### Visual Feedback

The note-roll canvas redraws every animation frame. Notes are positioned using `tileX = hitLineX + (note.startSec - currentTime) × SCROLL_SPEED` and `tileY = noteToY(midiNote)` — a linear mapping from pitch range to canvas height. The pitch ball's Y position exponentially lerps toward the target at factor 0.12 each frame. The overview strip maps the entire song's duration to the strip width.

### Scoring

Each note accumulates pitch samples while it is active. When `currentTime > note.endSec`, the final verdict is computed:

```
if total_samples == 0          → miss
if hit_samples / total >= 0.4  → hit   (+100 pts)
if (hit+near) / total >= 0.4   → near  (+50 pts)
else                           → miss  (+0 pts)
```

Final accuracy: `(hits + nears × 0.5) / totalNotes × 100 %`

Star thresholds: ★★★ ≥ 85%, ★★ ≥ 60%, ★ ≥ 30%.

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `Space` | Play / Pause |
| `R` | Restart (resets score and replays from start) |
| `M` | Metronome on / off |
| `L` | Show / hide lyrics panel |
| `T` | Transpose +1 semitone (wraps from +6 to −6) |
| `?` or `/` | Show keyboard shortcut help |
| `Esc` | Close any open dialog or modal |
| `←` / `→` | Seek −5 s / +5 s |
| Click on overview strip | Seek to clicked position |

## File Format Notes

### MIDI

- Any MIDI format (0, 1, 2) is accepted; all tracks are parsed and merged.
- All channels are included — there is no channel filter. Supply a MIDI file with only the vocal melody.
- SMPTE time division is not supported (throws an error).
- NoteOn with velocity 0 is treated as NoteOff.
- Notes shorter than 0.02 s are discarded. Consecutive same-pitch notes with a gap ≤ 0.15 s are merged.

### Audio

- WAV files are recommended (most reliable cross-browser decoding).
- MP3 files are supported but decode failures are per-file and non-fatal.
- Multiple audio files are decoded separately and played simultaneously through the same `GainNode` — useful for separate instrumental tracks.

## Project Structure

```
index.html           — Complete application (HTML + CSS + JavaScript in a single IIFE)
pitch-processor.js   — AudioWorkletProcessor for YIN pitch detection (loaded at runtime)
```

Optional files to add:
```
screenshot.png       — App screenshot shown in README
*.vkt                — Saved training session files (JSON + base64 assets)
```

## Extending the Project

- **Add song transposition UI presets** — store preset semitone offsets in an array and map them to buttons that call `ui.transposeSlider.dispatchEvent(new InputEvent('input'))` after setting `ui.transposeSlider.value`.
- **Add polyphonic/chord display** — `parseMidi()` currently keeps overlapping notes; the renderer renders them at their respective `noteToY()` positions. To highlight chords, group `NoteEvent[]` by overlapping time windows before passing to the renderer.
- **Add pitch deviation history trail** — push detected MIDI note + timestamp into a circular buffer in `renderFrame()` and draw a fading polyline before the pitch ball.
- **Add a practice loop** — store `loopStart` and `loopEnd` in `state`, check them in `renderFrame()`, and call `startAudioPlayback(loopStart / state.playbackRate)` when `currentSec >= loopEnd`.
- **Improve lyrics distribution** — replace the whitespace-split in `buildLyricsFromText()` with a syllable splitter for better per-note alignment.
- **Add MIDI channel filter** — add a `MIDI_CHANNEL` constant to `CONFIG` and filter events in `parseMidi()` before building `pendingNotes`.

## License

MIT — see `LICENSE` file.
