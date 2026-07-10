"""Skald - push-to-talk speech-to-text with auto-paste.

DEFAULT: LIVE mode. TAP the hotkey to start, a floating overlay shows the level
and words as you pause, tap again to stop and paste instantly.
Classic hold-to-talk (no window) is still available:  python skald.py --classic
Edit the CONFIG block below to change model, hotkey, and audio settings.
"""

import base64
import collections
import datetime
import io
import json
import os
import re
import sys
import time
import queue
import threading
import subprocess
import urllib.request
import wave
import webbrowser
from urllib.parse import quote_plus
import numpy as np
import sounddevice as sd
import pyperclip
import pyautogui
from pynput import keyboard
from faster_whisper import WhisperModel

# ---------------------------------------------------------------------------
# CONFIG - edit these to customise behaviour
# ---------------------------------------------------------------------------
CONFIG = {
    # Whisper model. On CPU (no GPU here), model size is THE latency lever.
    # RE-BENCHED 2026-07-02 on a real 20.6s clip (cpu, int8, beam=1, lang=en, vad on):
    #   tiny.en 0.32s (WER 8.5%) · base.en 0.88s · distil-small.en 1.17s ·
    #   small.en 1.63s (those three tied at 4.3% WER - identical real-word accuracy)
    #   · distil-medium.en 2.66s (10.6% - WORSE than small, skip it) ·
    #   medium.en 34.4s (0% WER but 0.6x REALTIME - SLOWER THAN THE SPEECH ITSELF;
    #   the 2026-06-28 "~2-4s" estimate was wildly wrong on this box).
    # Verdict: medium.en is unusable for push-to-talk here. small.en is the
    # accuracy-proven sweet spot (the pre-6/28 daily driver); base.en is the
    # fast lane if small ever feels slow. English-only ".en" models are faster
    # AND more accurate for English. "auto" (default since 1.3.0) reads YOUR CPU
    # and picks the rung for it: 8+ cores -> small.en, 4-7 -> base.en, else
    # tiny.en - so a weak laptop stays snappy without knowing what a Whisper
    # model is. Override per run with  --model <name>.
    "MODEL_SIZE": "auto",

    # Pynput key to hold for recording. Examples:
    #   keyboard.Key.ctrl_r   - Right Ctrl (default)
    #   keyboard.Key.alt_r    - Right Alt
    #   keyboard.KeyCode.from_char('`')  - backtick
    "HOTKEY": keyboard.Key.ctrl_r,

    # Audio sample rate in Hz - Whisper expects 16000
    "SAMPLE_RATE": 16000,

    # Mono mic input
    "CHANNELS": 1,

    # RMS amplitude below this value is considered silence - skip paste.
    # LOWERED 0.01 -> 0.004 on 2026-06-28: a headset noise-gate (JBL QuantumENGINE)
    # was delivering real speech at rms ~0.005–0.009, BELOW the old 0.01 gate, so
    # every word got thrown away as "too quiet." AUTO_GAIN then boosts what survives.
    "SILENCE_THRESHOLD": 0.004,

    # Maximum recording length in seconds. NOT a timer that stops the mic - you can
    # hold and talk as long as you like; this only TRIMS the clip after you release,
    # so it's really a runaway-safety cap (e.g. a stuck key). Bumped 60 -> 600 on
    # 2026-06-28 so a long, uninterrupted idea isn't chopped at one minute. Override
    # per run with  --max-seconds N. Heads-up: a very long clip means a longer wait
    # after you release (medium.en transcribes ~real-time-ish on CPU).
    "MAX_RECORD_SECONDS": 600,

    # Sounddevice input device - index (int) OR a case-insensitive name substring
    # (e.g. "JBL", "Quantum"). None = AUTO-PICK: take the Windows default input but
    # prefer its WASAPI version over the legacy MME one. The MME default (the bare
    # "Headset Microphone (...)" index) is the flaky path on this box - WASAPI is
    # the modern, lower-latency, more reliable shared-mode API.
    # List devices:  python skald.py --list
    "DEVICE": None,

    # Preferred host APIs, in priority order, when auto-picking or name-matching a
    # device. WASAPI first. (Order is a preference, not a hard filter.)
    "HOST_API_PREFERENCE": ["Windows WASAPI", "Windows DirectSound", "MME"],

    # Capture sample rate. None = the device's NATIVE rate, then resample to 16 kHz
    # in-app (recommended: WASAPI shared mode won't open at an arbitrary rate). Set
    # to 16000 to force the old "let the driver resample" path (works on MME/DSound).
    "CAPTURE_SAMPLERATE": None,

    # Software auto-gain: if a clip is real but quiet (e.g. a headset noise-gate only
    # cracked partway), scale it up so Whisper sees a healthy level instead of a
    # mumble. Normalizes RMS toward TARGET_RMS, capped at MAX_GAIN + the clip point.
    # This is a band-aid for a gated mic - fix the gate in QuantumENGINE for the cure.
    "AUTO_GAIN": True,
    # Lift quiet speech toward this loudness. Keyed on RMS (the speech body), NOT
    # peak - otherwise a lone click / XRun spike sets the peak high and cancels the
    # boost, the exact case AUTO_GAIN exists to rescue. Capped at MAX_GAIN and at
    # the clipping point.
    "TARGET_RMS": 0.08,
    "MAX_GAIN": 25.0,

    # At/below this RMS the capture is treated as DIGITAL SILENCE (the device handed
    # us zeros - another app owns the mic, or the gate was fully shut) rather than
    # merely "too quiet." The two cases get different, actionable messages.
    "DIGITAL_SILENCE_RMS": 0.0006,

    # Compute type for faster-whisper: "int8" (CPU-friendly), "float16" (GPU)
    "COMPUTE_TYPE": "int8",

    # Device for inference: "cpu" or "cuda"
    "INFERENCE_DEVICE": "cpu",

    # ---- ASR backend (the Vulkan experiment, ADOPTED 2026-07-02) ----
    # "auto"           -> use the local GPU server when reachable (autostarts it if the
    #                     exe+model exist), FALLING BACK to in-process faster-whisper (CPU).
    # "server"         -> GPU server only (fails loudly if it can't run).
    # "faster-whisper" -> CPU-only, the pre-Vulkan behavior.
    # The server = koboldcpp-nocuda.exe (embeds whisper.cpp's Vulkan backend) running
    # ggml-large-v3-turbo on the RX 7900 GRE. Re-bench 2026-07-02, same 20.6s clip:
    #   GPU: small.en 0.29s · medium-q5 0.58s · LARGE-v3-turbo-q5 0.50s (41x realtime)
    #   CPU: small.en 1.63s · medium.en 34.4s
    # Big-model accuracy at instant speed - the size/latency tradeoff is gone on GPU.
    # The server is spawned DETACHED: it outlives this app, so the next launch reuses
    # it with the model already hot (~6s first boot, instant after).
    "ASR_BACKEND": "auto",
    "ASR_SERVER_URL": "http://127.0.0.1:5075",
    "ASR_SERVER_EXE": os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "engines", "koboldcpp-nocuda.exe"),
    "ASR_SERVER_MODEL": os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "engines", "ggml-large-v3-turbo-q5_0.bin"),
    "ASR_SERVER_BOOT_TIMEOUT": 90,

    # ---- Speed tuning ----
    # Decoding strategy. beam_size=1 is greedy (fastest); 5 is slower but slightly
    # more accurate. For short push-to-talk dictation, 1 is plenty and much faster.
    "BEAM_SIZE": 1,

    # Pin the spoken language to skip Whisper's auto-detection pass (saves time on
    # every clip). Set to None to auto-detect if you dictate in multiple languages.
    "LANGUAGE": "en",

    # CPU threads for inference. 0 = CTranslate2 default (~4). On a many-core box,
    # bumping this speeds up a single transcription. Diminishing returns past ~8.
    "CPU_THREADS": 8,

    # ---- Live (streaming) mode  (python skald.py --stream) ----
    # Tap the hotkey to start, watch a floating overlay show it hearing you + the
    # words as you pause, tap again to stop and paste. Opt-in; classic hold-to-talk
    # is unchanged and stays the default.
    # Model that drives the SNAPPY live text. "auto" (default since 1.3.0) picks by
    # CPU cores, same ladder as MODEL_SIZE - live streaming on a 2-core laptop with
    # small.en would lag hopelessly behind your voice.
    "LIVE_MODEL": "auto",
    # Model for the final paste when you tap off. None = paste the streamed live text
    # THE INSTANT you tap off (Rath's 2026-07-02 call - closer-to-instant typing; the
    # live text is what you watched appear anyway). Set "small.en" to add a fast
    # (~1s) seam-fixing full-clip pass, or "medium.en" only if you can stomach
    # ~1.7x-the-clip-length waits (34s on a 20s clip - the re-bench that demoted it).
    "FINAL_MODEL": None,
    # A phrase is "done" after this much trailing silence (ms) following speech.
    "PHRASE_SILENCE_MS": 700,
    # Ignore blips with less than this much actual speech (ms) - kills phantom phrases.
    "PHRASE_MIN_MS": 250,
    # Force-flush a phrase after this many seconds even with no pause, so an unbroken
    # monologue still streams text instead of waiting for you to breathe.
    "PHRASE_MAX_SECONDS": 12,
    # RMS above this counts as speech for pause detection (eyeball it with --test).
    "STREAM_SPEECH_RMS": 0.006,
    # Floating overlay look.
    "OVERLAY_OPACITY": 0.93,
    "OVERLAY_WIDTH": 380,
    "OVERLAY_HEIGHT": 150,
    # Overlay waveform accent. Toggle "ice" (cool blue voice-energy, the default) or
    # "amber" (molten). Runic text, dividers, and the close control stay gold either way.
    "THEME_ACCENT": "gold",

    # ---- Never-lose-a-brainstorm capture (2026-07-07, Rath's call) ----
    # Every transcribed phrase is appended to a dated file THE MOMENT it's heard, so a
    # misclick (paste landed in the wrong window), a crash, or an accidental close can
    # never lose a dictation. One rolling file per day; each recording gets a timestamp
    # header. Independent of the paste target - the file always has the words.
    "SAVE_TRANSCRIPTS": True,               # False (or --no-save) to never write to disk
    "TRANSCRIPT_DIR": os.path.join(os.path.expanduser("~"), "Documents",
                                   "Skald Transcripts"),
    # Housekeeping: on launch, delete transcript files older than this many days so the
    # folder can't balloon over months of daily brainstorming. 0 = keep forever.
    "TRANSCRIPT_RETENTION_DAYS": 30,

    # Audio cue on record toggle: a rising two-note when you START listening, a falling
    # two-note when you STOP - so your ears confirm the state even when your eyes are
    # elsewhere. Plays a synthesized tone through your DEFAULT OUTPUT device (your
    # headset) via sounddevice - winsound.Beep routed to the legacy system beep and was
    # inaudible here. Default ON (Rath wants it); --no-chime for one silent run.
    "CHIME": True,
    "CHIME_VOLUME": 0.22,                    # 0.0–1.0 tone amplitude (0.3 was a blast)

    # ---- Command routing ----
    # When True, a transcript that STARTS WITH the prefix is parsed as a command
    # instead of being pasted. Anything else is dictated (pasted) as normal.
    "ENABLE_COMMANDS": True,
    # The wake/prefix word that turns dictation into a command. Say it first:
    # "skald open chrome", "skald search ...", "skald send ...".
    # A prefix (vs. bare verbs) prevents false triggers like "open the door".
    "COMMAND_PREFIX": "skald",
    # Default search engine for "search <query>".
    "SEARCH_URL": "https://www.google.com/search?q=",
    # name (lowercase) -> shell command to launch. Extend freely.
    "APP_MAP": {
        "notepad": "notepad",
        "calculator": "calc",
        "calc": "calc",
        "explorer": "explorer",
        "file explorer": "explorer",
        "chrome": 'start "" chrome',
        "edge": 'start "" msedge',
        "browser": 'start "" msedge',
        "terminal": 'start "" wt',
        "cmd": 'start "" cmd',
        "code": 'start "" code',
        "vs code": 'start "" code',
        "vscode": 'start "" code',
        "discord": 'start "" discord',  # may need full path if not in App Paths
    },

}
# ---------------------------------------------------------------------------

# Global state
_recording = False
_buffer = []                       # audio chunks captured while _recording is True
_buffer_lock = threading.Lock()
_record_start = 0.0                # perf_counter() when the current hold began
_stream = None                     # the single, always-open input stream
_capture_sr = 16000                # actual rate the stream opened at (set in main)
_silent_streak = 0                 # consecutive silent/quiet captures (for escalation)

def load_model():
    """Load the Whisper model. Called once at startup."""
    print(f"⏳ Loading Whisper model '{CONFIG['MODEL_SIZE']}' "
          f"on {CONFIG['INFERENCE_DEVICE']} ({CONFIG['COMPUTE_TYPE']})...")
    print("   (First run will download the model - this may take a minute.)")
    model = WhisperModel(
        CONFIG["MODEL_SIZE"],
        device=CONFIG["INFERENCE_DEVICE"],
        compute_type=CONFIG["COMPUTE_TYPE"],
        cpu_threads=CONFIG["CPU_THREADS"],
    )

    # Warm up: the very first transcription pays a one-time cost to allocate
    # buffers and JIT the compute kernels. Run a dummy pass now so the first
    # real utterance is fast instead of slow.
    print("🔥 Warming up model...")
    warmup = np.zeros(CONFIG["SAMPLE_RATE"], dtype=np.float32)  # 1s of silence
    list(model.transcribe(warmup, beam_size=1, language=CONFIG["LANGUAGE"])[0])

    print(f"✅ Model loaded.")
    return model


def audio_callback(indata, frames, time_info, status):
    """Always-on input callback for the single, session-long stream.

    Buffers audio ONLY while _recording is True. Using one long-lived stream
    instead of opening/closing a fresh one per key-press is the key robustness
    fix: on several Windows/PortAudio drivers a re-opened device silently
    delivers zeros after the first use - the root cause of "first clip works,
    every clip after says 'No speech detected'".
    """
    if status:
        # XRuns / overflows happen occasionally - surface but don't crash.
        print(f"⚠️  audio status: {status}", file=sys.stderr)
    # Gate + append under ONE lock: otherwise a fast release-then-repress can clear
    # the buffer and flip _recording (in on_press) between our read of _recording and
    # the append here, bleeding a stale tail chunk into the start of the next clip.
    with _buffer_lock:
        if _recording:
            _buffer.append(indata.copy())


def prepare_audio(frames):
    """Flatten captured chunks into a 16kHz mono float32 array for Whisper.

    faster-whisper accepts the array directly (no temp-WAV round-trip). Returns
    (audio, rms, seconds). audio is None only when no frames were captured. The
    caller applies the silence gate so a skip can be reported with real numbers.
    """
    if not frames:
        return None, 0.0, 0.0

    audio = np.concatenate(frames, axis=0).flatten().astype(np.float32)

    # The stream may have opened at the device's native rate (e.g. 48 kHz on
    # WASAPI). Whisper wants 16 kHz - resample here so RMS, trimming and inference
    # all work on the same 16 kHz signal.
    audio = resample_to_16k(audio, _capture_sr)

    max_samples = int(CONFIG["MAX_RECORD_SECONDS"] * CONFIG["SAMPLE_RATE"])
    if audio.size > max_samples:
        audio = audio[:max_samples]
        print(f"⚠️  Trimmed to max {CONFIG['MAX_RECORD_SECONDS']}s.")

    rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
    seconds = audio.size / CONFIG["SAMPLE_RATE"]
    return audio, rms, seconds


# ---------------------------------------------------------------------------
# Audio DSP helpers - resampling, gain, device selection, live meter
# ---------------------------------------------------------------------------
def _lowpass(audio, cutoff_hz, sr, taps=101):
    """Windowed-sinc FIR low-pass (anti-alias before downsampling). Dependency-free
    so we don't pull in scipy. `cutoff_hz` is the passband edge in Hz."""
    if audio.size < taps:
        return audio
    f = max(min(cutoff_hz / sr, 0.5), 1e-3)            # cycles/sample, 0..0.5
    n = np.arange(taps) - (taps - 1) / 2.0
    h = 2 * f * np.sinc(2 * f * n) * np.hamming(taps)  # ideal LPF * window
    h = (h / h.sum()).astype(np.float32)
    return np.convolve(audio, h, mode="same").astype(np.float32)


def resample_to_16k(audio, src_sr, dst_sr=16000):
    """Resample mono float32 to 16 kHz. Anti-alias low-pass when downsampling, then
    linear interpolation to the target length. Good enough for speech into Whisper."""
    if audio.size == 0 or src_sr == dst_sr:
        return audio
    if src_sr > dst_sr:
        audio = _lowpass(audio, cutoff_hz=dst_sr * 0.475, sr=src_sr)  # ~7.6 kHz @48k
    n_dst = int(round(audio.size * dst_sr / src_sr))
    if n_dst <= 1:
        return audio[:0]
    x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_dst, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def apply_auto_gain(audio, rms=None):
    """Lift a quiet-but-real clip toward TARGET_RMS so a half-gated mic still feeds
    Whisper a usable level. Keys off RMS (the speech body), not peak - a lone click
    or XRun spike must not suppress the boost. Capped at MAX_GAIN and at the point of
    clipping. No-op on healthy/empty audio."""
    if not CONFIG["AUTO_GAIN"] or audio.size == 0:
        return audio
    if rms is None:
        rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms < 1e-6:
        return audio
    peak = float(np.max(np.abs(audio)))
    headroom = 0.99 / (peak + 1e-9)  # don't push the loudest sample past full scale
    gain = min(CONFIG["TARGET_RMS"] / rms, CONFIG["MAX_GAIN"], headroom)
    if gain <= 1.0:
        return audio
    return np.clip(audio * gain, -1.0, 1.0).astype(np.float32)


def _host_api_name(dev_index):
    devs = sd.query_devices()
    return sd.query_hostapis(devs[dev_index]["hostapi"])["name"]


def _ha_rank(name):
    pref = CONFIG["HOST_API_PREFERENCE"]
    return pref.index(name) if name in pref else len(pref) + 1


def _input_indices():
    return [i for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def resolve_device(spec):
    """Resolve a device spec to (index, name, host_api_name).

    spec: None  -> auto: the default input, but prefer its WASAPI sibling.
          int   -> that exact device index.
          str   -> case-insensitive name substring, best host API wins.
    """
    devs = sd.query_devices()
    inputs = _input_indices()
    if not inputs:
        raise SystemExit("❌ No input (microphone) devices found. Plug in a mic, "
                         "then run:  python skald.py --list")

    if isinstance(spec, int):
        if spec not in inputs:
            raise SystemExit(f"❌ Device {spec} is not a valid input device. "
                             f"Run --list to see the input indices.")
        return spec, devs[spec]["name"], _host_api_name(spec)

    if isinstance(spec, str) and spec.strip():
        s = spec.lower().strip()
        matches = [i for i in inputs if s in devs[i]["name"].lower()]
        matches.sort(key=lambda i: _ha_rank(_host_api_name(i)))
        if matches:
            i = matches[0]
            return i, devs[i]["name"], _host_api_name(i)
        raise SystemExit(f"❌ No input device matches '{spec}'. Run --list.")

    # auto: follow the OS default input, but upgrade it to the best host API
    default_in = sd.default.device[0]
    if default_in is None or default_in < 0 or default_in not in inputs:
        default_in = inputs[0]
    key = devs[default_in]["name"][:20].lower()
    same = [i for i in inputs if devs[i]["name"][:20].lower() == key]
    same.sort(key=lambda i: _ha_rank(_host_api_name(i)))
    pick = same[0] if same else default_in
    return pick, devs[pick]["name"], _host_api_name(pick)


def cmd_list():
    """Print all input devices (★ = current default) with host API + a warning on
    voice-chat/DSP virtual endpoints that are prone to noise-gating."""
    devs = sd.query_devices()
    default_in = sd.default.device[0]
    print("Input devices  (★ = current Windows default input):\n")
    for i, d in enumerate(devs):
        if d["max_input_channels"] <= 0:
            continue
        ha = sd.query_hostapis(d["hostapi"])["name"]
        star = "★" if i == default_in else " "
        nl = d["name"].lower()
        flag = ""
        if "chat" in nl or "communications" in nl:
            flag = "   ⚠ voice-chat/DSP endpoint - noise-gated by headset software"
        print(f"  {star} [{i:>2}] {d['name'][:44]:<44} {ha:<20} "
              f"{int(d['default_samplerate'])}Hz{flag}")
    print("\nPin one:  set CONFIG['DEVICE'] = <index or name>, "
          "or run with  --device <index|name>")


# ---------------------------------------------------------------------------
# Hardware auto-detect - "what machine is this, and which engine fits it best?"
# Not everyone knows what's in their box (a gifted PC, a two-year-old budget
# build). Skald reads the GPU + CPU itself and says, in plain words, which
# engine it will use and whether a faster one is one download away.
# ---------------------------------------------------------------------------
def _gpu_names():
    """Best-effort GPU name list, no extra deps. Failure of any probe -> []."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_VideoController).Name"],
                capture_output=True, text=True, timeout=12,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        if sys.platform == "darwin":
            out = subprocess.run(["system_profiler", "SPDisplaysDataType"],
                                 capture_output=True, text=True, timeout=12)
            return [ln.split(":", 1)[1].strip() for ln in out.stdout.splitlines()
                    if "Chipset Model" in ln]
        out = subprocess.run(["sh", "-c", "lspci | grep -iE 'vga|3d controller'"],
                             capture_output=True, text=True, timeout=12)
        return [ln.split(":", 2)[-1].strip() for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        return []


def classify_gpu(name):
    """One GPU name -> 'nvidia' | 'amd' | 'intel-arc' | 'apple' | 'igpu' | 'unknown'.
    iGPU markers are checked BEFORE the generic vendor words so an 'AMD Radeon(TM)
    Graphics' APU or 'Intel(R) Iris Xe' never masquerades as a discrete card."""
    low = name.lower()
    igpu = ("radeon(tm) graphics", "vega 8", "vega 7", "vega 6", "vega 3",
            "uhd graphics", "iris xe", "iris(r)", "hd graphics",
            "microsoft basic", "virtual", "parsec", "displaylink")
    if any(k in low for k in igpu):
        return "igpu"
    if any(k in low for k in ("geforce", "rtx", "gtx", "quadro", "nvidia")):
        return "nvidia"
    if "apple m" in low:
        return "apple"
    if any(k in low for k in ("arc a", "arc b", "intel arc")):
        return "intel-arc"
    if any(k in low for k in ("radeon", "amd", "firepro")):
        return "amd"
    if "intel" in low:
        return "igpu"
    return "unknown"


_GPU_RANK = {"nvidia": 0, "amd": 1, "intel-arc": 2, "apple": 3, "igpu": 4,
             "unknown": 5, "none": 9}


def detect_hardware():
    """{'gpus': [(name, class)], 'best_gpu': name, 'gpu_class': class, 'cpu_cores': n}"""
    gpus = [(n, classify_gpu(n)) for n in _gpu_names()]
    best = min(gpus, key=lambda g: _GPU_RANK[g[1]]) if gpus else ("", "none")
    return {"gpus": gpus, "best_gpu": best[0], "gpu_class": best[1],
            "cpu_cores": os.cpu_count() or 4}


def pick_cpu_model(cores):
    """CPU-ladder pick when no GPU server carries the load. Bench-anchored
    (2026-07-02): small.en is the accuracy sweet spot but needs a real CPU;
    weaker boxes get base.en/tiny.en so push-to-talk stays snappy."""
    if cores >= 8:
        return "small.en"
    if cores >= 4:
        return "base.en"
    return "tiny.en"


def recommend_engine(hw, server_ready):
    """(verdict, plain-English reason). verdict: 'gpu-server' (use/start it),
    'gpu-available' (hardware could, assets missing - tell them the one download),
    'cpu' (this box's honest best is the CPU ladder)."""
    vulkan_capable = hw["gpu_class"] in ("nvidia", "amd", "intel-arc")
    if vulkan_capable and server_ready:
        return ("gpu-server",
                f"{hw['best_gpu']}: big-model accuracy at GPU speed (whisper.cpp Vulkan)")
    if vulkan_capable:
        return ("gpu-available",
                f"{hw['best_gpu']} can run Whisper 30-70x realtime with the free "
                f"Vulkan server - one download, works on NVIDIA, AMD and Intel Arc "
                f"alike (README > 'GPU acceleration')")
    return ("cpu",
            f"no discrete GPU found - CPU engine, model {pick_cpu_model(hw['cpu_cores'])} "
            f"({hw['cpu_cores']} cores)")


def resolve_auto_models(hw=None):
    """MODEL_SIZE / LIVE_MODEL set to 'auto' resolve to the CPU-ladder pick for this
    machine. Called once at startup and by --doctor; explicit names pass through."""
    if "auto" in (CONFIG.get("MODEL_SIZE"), CONFIG.get("LIVE_MODEL")):
        hw = hw or detect_hardware()
        pick = pick_cpu_model(hw["cpu_cores"])
        if CONFIG.get("MODEL_SIZE") == "auto":
            CONFIG["MODEL_SIZE"] = pick
        if CONFIG.get("LIVE_MODEL") == "auto":
            CONFIG["LIVE_MODEL"] = pick
        print(f"🧭 Auto model: {pick} ({hw['cpu_cores']} CPU cores)")


def _print_gpu_hint():
    """Called right before the CPU engine loads: resolve any 'auto' model picks for
    THIS machine, and if the box actually has a Vulkan-capable GPU, say (once, in
    plain words) that a much faster engine is one download away."""
    hw = detect_hardware()
    resolve_auto_models(hw)
    verdict, reason = recommend_engine(hw, server_ready=False)
    if verdict == "gpu-available":
        print(f"💡 {reason}")


def cmd_doctor():
    """Self-diagnostic: print PASS/FAIL for the runtime, dependencies, a default
    mic, the Whisper model cache dir, and a clipboard round-trip. Exits 0 only if
    every check passes, so it works cleanly as a setup gate."""
    print("Skald self-diagnostic")
    print("-" * 44)
    results = []

    def check(label, ok, detail=""):
        tag = "PASS" if ok else "FAIL"
        line = f"[{tag}] {label}"
        if detail:
            line += f"  ({detail})"
        print(line)
        results.append(bool(ok))

    # 1. Python version
    v = sys.version_info
    check(f"Python >= 3.11", v >= (3, 11), f"found {v.major}.{v.minor}.{v.micro}")

    # 2. Each dependency imports
    for mod in ("numpy", "sounddevice", "pyperclip", "pyautogui", "pynput",
                "faster_whisper"):
        try:
            __import__(mod)
            check(f"import {mod}", True)
        except Exception as exc:
            check(f"import {mod}", False, str(exc)[:60])

    # 3. A default input (microphone) device exists
    try:
        import sounddevice as _sd
        inputs = [d for d in _sd.query_devices() if d["max_input_channels"] > 0]
        check("input (microphone) device present", bool(inputs),
              f"{len(inputs)} found" if inputs else "none found")
    except Exception as exc:
        check("input (microphone) device present", False, str(exc)[:60])

    # 4. The Whisper model cache dir is writable
    cache = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
    try:
        os.makedirs(cache, exist_ok=True)
        probe = os.path.join(cache, ".skald_write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        check("whisper model cache dir writable", True, cache)
    except Exception as exc:
        check("whisper model cache dir writable", False, str(exc)[:60])

    # 5. Clipboard round-trip (set then get), restoring any prior contents
    try:
        import pyperclip as _pc
        try:
            prev = _pc.paste()
        except Exception:
            prev = None
        marker = "skald-doctor-clipboard-check"
        _pc.copy(marker)
        got = _pc.paste()
        if prev is not None:
            try:
                _pc.copy(prev)
            except Exception:
                pass
        check("clipboard round-trip (pyperclip)", got == marker)
    except Exception as exc:
        check("clipboard round-trip (pyperclip)", False, str(exc)[:60])

    # 6. Hardware report: what this machine actually is, in plain words. You
    #    shouldn't need to know whether your GPU wants CUDA or Vulkan - Skald reads
    #    the box and says which engine fits it.
    hw = detect_hardware()
    if hw["gpus"]:
        for name, cls in hw["gpus"]:
            label = {"nvidia": "discrete NVIDIA - Vulkan server capable",
                     "amd": "discrete AMD - Vulkan server capable",
                     "intel-arc": "discrete Intel Arc - Vulkan server capable",
                     "apple": "Apple silicon",
                     "igpu": "integrated graphics (CPU-class for Whisper)",
                     "unknown": "unrecognized"}[cls]
            print(f"[INFO] GPU: {name}  ({label})")
    else:
        print("[INFO] GPU: none detected")
    print(f"[INFO] CPU: {hw['cpu_cores']} cores"
          f"  (CPU-ladder model pick: {pick_cpu_model(hw['cpu_cores'])})")

    # 7. ASR engine report: which backend will actually carry your voice.
    #    The GPU server is optional, so these lines are INFO, not pass/fail,
    #    except when CONFIG demands a server that cannot be reached.
    backend = CONFIG.get("ASR_BACKEND", "auto")
    server_url = CONFIG.get("ASR_SERVER_URL", "")
    exe = CONFIG.get("ASR_SERVER_EXE", "")
    mdl = CONFIG.get("ASR_SERVER_MODEL", "")
    server_live = False
    try:
        import urllib.request as _ur
        _ur.urlopen(server_url + "/api/v1/info/version", timeout=2)
        server_live = True
    except Exception:
        server_live = False
    if server_live:
        check("GPU ASR server reachable", True, server_url + " (whisper.cpp Vulkan)")
        print("[INFO] engine in use: GPU server, large-model accuracy at GPU speed")
    elif os.path.isfile(exe) and os.path.isfile(mdl):
        check("GPU ASR server auto-start available", True,
              os.path.basename(exe) + " + " + os.path.basename(mdl))
        print("[INFO] engine in use: GPU server (Skald will start it on launch)")
    elif backend == "server":
        check("GPU ASR server reachable", False,
              "ASR_BACKEND is 'server' but nothing answers at " + server_url)
    else:
        resolve_auto_models(hw)
        verdict, reason = recommend_engine(hw, server_ready=False)
        model_size = CONFIG.get("MODEL_SIZE", "base.en")
        print(f"[INFO] engine in use: faster-whisper on CPU (model {model_size})")
        if verdict == "gpu-available":
            print(f"[INFO] 💡 faster engine available: {reason}")
        else:
            print(f"[INFO] {reason} - this is the right engine for this machine")

    print("-" * 44)
    ok = all(results)
    print("All checks passed." if ok else "Some checks FAILED (see above).")
    sys.exit(0 if ok else 1)


def run_meter(device, seconds=20):
    """Live RMS meter so you can SEE your mic level and tune the noise gate. Speak
    normally and watch the bar; a healthy mic peaks well past 0.05."""
    info = sd.query_devices(device, "input")
    sr = int(info["default_samplerate"])
    ha = sd.query_hostapis(info["hostapi"])["name"]
    print(f"🎚️  Live meter: [{device}] {info['name']} ({ha}) @ {sr}Hz for {seconds}s.")
    print("    Speak normally. Ctrl+C to stop early.\n")
    peak = [0.0]

    def cb(indata, frames, t, status):
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        peak[0] = max(peak[0], rms)
        bars = int(min(rms / 0.1, 1.0) * 40)
        print(f"\r  rms={rms:0.4f}  peak={peak[0]:0.4f} "
              f"|{'█' * bars}{' ' * (40 - bars)}|", end="", flush=True)

    try:
        with sd.InputStream(samplerate=sr, channels=1, dtype="float32",
                            device=device, blocksize=int(sr * 0.1), callback=cb):
            sd.sleep(int(seconds * 1000))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"\n❌ Couldn't open this device for the meter: {exc}")
        print("   It may be held in exclusive mode, or your main Skald")
        print("   instance is already using it. Close that and retry, or try another")
        print("   --device from --list.")
        return
    print("\n")
    p = peak[0]
    if p > 0.05:
        print(f"✅ Peak RMS {p:.4f} - healthy. The mic itself is fine.")
    elif p > CONFIG["DIGITAL_SILENCE_RMS"]:
        print(f"⚠️  Peak RMS only {p:.4f} - REAL but heavily gated/low. "
              f"Open the gate / raise mic level in JBL QuantumENGINE + Windows Sound.")
    else:
        print(f"🔇 Peak RMS {p:.4f} - DIGITAL SILENCE. This endpoint is delivering "
              f"zeros: another app owns the mic, the gate is shut, or wrong device. "
              f"Try a different --device from --list.")


# ---------------------------------------------------------------------------
# GPU ASR server (whisper.cpp Vulkan inside koboldcpp) - adopted 2026-07-02
# ---------------------------------------------------------------------------
_fallback_model = None            # lazy CPU model, loaded only if the GPU path dies
_fallback_lock = threading.Lock()


def _asr_port_alive(timeout=1.5):
    try:
        urllib.request.urlopen(CONFIG["ASR_SERVER_URL"] + "/api/v1/info/version",
                               timeout=timeout)
        return True
    except Exception:
        return False


def ensure_asr_server(verbose=True):
    """True if the GPU ASR server is reachable - starting it (detached) if we can."""
    if _asr_port_alive():
        return True
    exe, mdl = CONFIG["ASR_SERVER_EXE"], CONFIG["ASR_SERVER_MODEL"]
    if not (os.path.exists(exe) and os.path.exists(mdl)):
        return False
    port = CONFIG["ASR_SERVER_URL"].rsplit(":", 1)[-1]
    if verbose:
        print(f"⏳ Starting GPU ASR server (Vulkan · {os.path.basename(mdl)}) on :{port} ...")
    try:
        log = open(os.path.join(os.path.dirname(exe), "server.log"), "a",
                   encoding="utf-8", errors="replace")
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW - the server
        # deliberately OUTLIVES this app so the model stays hot across launches.
        proc = subprocess.Popen(
            [exe, "--nomodel", "--skiplauncher", "--usevulkan",
             "--whispermodel", mdl, "--port", port],
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=0x08000208, cwd=os.path.dirname(exe))
    except Exception as exc:
        if verbose:
            print(f"⚠️  Couldn't start the GPU ASR server ({str(exc)[:80]}).")
        return False
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < CONFIG["ASR_SERVER_BOOT_TIMEOUT"]:
        if _asr_port_alive():
            if verbose:
                print("✅ GPU ASR server ready.")
            return True
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    if verbose:
        print("⚠️  GPU ASR server didn't come up - see engines/server.log.")
    return False


def _wav_b64(audio):
    """float32 mono 16k -> base64 WAV (int16) for the server."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(CONFIG["SAMPLE_RATE"])
        w.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
    return base64.b64encode(buf.getvalue()).decode()


def transcribe_gpu(audio):
    """One clip through the Vulkan server. Raises on failure (caller decides fallback)."""
    body = json.dumps({"audio_data": _wav_b64(audio), "langcode": "en"}).encode()
    req = urllib.request.Request(CONFIG["ASR_SERVER_URL"] + "/api/extra/transcribe",
                                 data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return (json.load(r).get("text") or "").strip()


def _get_fallback_model():
    global _fallback_model
    with _fallback_lock:
        if _fallback_model is None:
            size = pick_cpu_model(os.cpu_count() or 4)
            print(f"⏳ Loading CPU fallback model ({size})...")
            _fallback_model = WhisperModel(size, device="cpu", compute_type="int8",
                                           cpu_threads=CONFIG["CPU_THREADS"])
        return _fallback_model


def gpu_asr_active():
    return CONFIG.get("ASR_BACKEND") in ("auto", "server") and _asr_port_alive(timeout=0.8)


def transcribe_any(model, audio):
    """THE transcription chokepoint (classic + live both route here): GPU server when
    allowed and alive, else local faster-whisper (the passed model, or a lazily loaded
    small.en if this session skipped loading one because the GPU was up)."""
    backend = CONFIG.get("ASR_BACKEND", "faster-whisper")
    if backend in ("auto", "server") and _asr_port_alive(timeout=0.8):
        try:
            return transcribe_gpu(audio)
        except Exception as exc:
            if backend == "server":
                raise
            print(f"⚠️  GPU ASR failed ({str(exc)[:70]}) - CPU fallback.")
    if backend == "server":
        raise RuntimeError("ASR_BACKEND='server' but the GPU server is unreachable")
    m = model or _get_fallback_model()
    segments, _info = m.transcribe(
        audio, beam_size=CONFIG["BEAM_SIZE"], language=CONFIG["LANGUAGE"],
        condition_on_previous_text=False, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=300))
    return " ".join(s.text.strip() for s in segments).strip()


# ---------------------------------------------------------------------------
# Command routing
# ---------------------------------------------------------------------------
# Verbs recognized after the prefix. Longest phrases first so "send message"
# wins over "send". Synonyms normalized in parse_command().
_VERB_RE = re.compile(
    r"(open|launch|search|google|type|dictate|paste|send message|send)\b\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)
_CANCEL = {"cancel", "never mind", "nevermind", "scratch that", "stop"}
_HELP = {"help", "commands", "list commands"}


def parse_command(full_text):
    """Pure parser (no side effects). Returns (verb, arg) if the transcript is a
    command, else None. Kept side-effect-free so it can be unit-tested."""
    prefix = CONFIG["COMMAND_PREFIX"].lower().strip()
    cleaned = full_text.strip().rstrip(".!?").strip()
    low = cleaned.lower()
    if not (low == prefix or low.startswith(prefix + " ") or low.startswith(prefix + ",")):
        return None  # not a command -> dictate normally
    rest = cleaned[len(prefix):].lstrip(" ,").strip()
    if not rest:
        return ("help", "")
    low_rest = rest.lower()
    if low_rest in _CANCEL:
        return ("cancel", "")
    if low_rest in _HELP:
        return ("help", "")
    m = _VERB_RE.match(rest)
    if not m:
        return ("unknown", rest)
    verb = m.group(1).lower()
    arg = rest[m.start(2):].strip()
    if verb == "launch":
        verb = "open"
    elif verb == "google":
        verb = "search"
    elif verb in ("dictate", "paste"):
        verb = "type"
    elif verb == "send message":
        verb = "send"
    return (verb, arg)


def _paste_text(text):
    """Copy text to clipboard and paste into the active window."""
    if not text:
        return
    pyperclip.copy(text)
    time.sleep(0.15)  # let the original window regain focus
    pyautogui.hotkey("ctrl", "v")


def _send_text(text):
    """Paste text then press Enter (e.g. send a Discord/chat message)."""
    text = re.sub(r"^message\s+", "", text, flags=re.IGNORECASE).strip()
    if not text:
        print("❓ Send what?")
        return
    print(f"📤 Sending: {text}")
    _paste_text(text)
    time.sleep(0.1)
    pyautogui.press("enter")


def _web_search(query):
    if not query:
        print("❓ Search what?")
        return
    print(f"🔍 Searching: {query}")
    webbrowser.open(CONFIG["SEARCH_URL"] + quote_plus(query))


def _open_target(arg):
    if not arg:
        print("❓ Open what? e.g. 'skald open chrome'")
        return
    key = arg.lower().strip()
    target = CONFIG["APP_MAP"].get(key)
    if target:
        print(f"🚀 Opening {arg}")
        try:
            subprocess.Popen(target, shell=True)
        except Exception as exc:
            print(f"❌ Could not open {arg}: {exc}")
        return
    # Looks like a domain/URL?
    if "." in key and " " not in key:
        url = key if key.startswith("http") else "https://" + key
        print(f"🌐 Opening {url}")
        webbrowser.open(url)
        return
    # Last resort: let Windows try to resolve it.
    print(f"🚀 Trying to open '{arg}' via system...")
    try:
        subprocess.Popen(f'start "" "{arg}"', shell=True)
    except Exception as exc:
        print(f"❌ Don't know how to open '{arg}'. Add it to APP_MAP. ({exc})")


def _print_help():
    print("🗣️  Voice commands (say the prefix first, e.g. 'skald open chrome'):")
    print("   • open <app or site>   'open chrome' · 'open github.com'")
    print("   • search <query>       'search kentucky llc fees'")
    print("   • send <text>          paste + press Enter (send in a chat window)")
    print("   • type <text>          force-paste text")
    print("   • cancel / never mind  discard")
    print("   Anything WITHOUT the prefix is dictated (pasted) as normal.")


def route_command(full_text):
    """Parse and execute a command. Returns True if handled (don't paste),
    False if the transcript should fall through to normal dictation."""
    parsed = parse_command(full_text)
    if parsed is None:
        return False
    verb, arg = parsed
    print(f"🎯 Command: {verb}" + (f": {arg}" if arg else ""))
    if verb == "help":
        _print_help()
    elif verb == "cancel":
        print("🚫 Cancelled.")
    elif verb == "unknown":
        print(f"❓ Unrecognized command: '{arg}'. Say 'skald help'.")
    elif verb == "open":
        _open_target(arg)
    elif verb == "search":
        _web_search(arg)
    elif verb == "type":
        _paste_text(arg)
    elif verb == "send":
        _send_text(arg)
    return True


def transcribe_and_paste(model, audio):
    """Transcribe (GPU server or faster-whisper), then route as a command or paste."""
    try:
        t0 = time.perf_counter()
        full_text = transcribe_any(model, audio)
        elapsed = time.perf_counter() - t0

        if not full_text:
            print(f"🔇 No speech detected - audio had signal but Whisper/VAD "
                  f"returned nothing ({elapsed:.2f}s). Speak more clearly or relax "
                  f"vad_parameters.")
            return

        print(f"✅ Transcribed ({elapsed:.2f}s): {full_text}")

        # Command mode: if it starts with the prefix, act on it instead of pasting.
        if CONFIG.get("ENABLE_COMMANDS") and route_command(full_text):
            return

        # Otherwise dictate: paste into the active window.
        _paste_text(full_text)

    except Exception as exc:
        print(f"❌ Transcription error: {exc}")


def on_press(key, model):
    """Begin buffering audio on hotkey press (stream is already running)."""
    global _recording, _buffer, _record_start

    if key != CONFIG["HOTKEY"] or _recording:
        return

    with _buffer_lock:
        _buffer = []
    _record_start = time.perf_counter()
    _recording = True  # set last: the callback only buffers once the list is clear

    print("🎙️  Recording... (release key to stop)")


def on_release(key, model):
    """Stop buffering on hotkey release, then transcribe in the background."""
    global _recording

    if key != CONFIG["HOTKEY"] or not _recording:
        return

    _recording = False  # callback stops buffering immediately
    held = time.perf_counter() - _record_start

    with _buffer_lock:
        frames_snapshot = list(_buffer)

    if not frames_snapshot:
        print(f"🔇 No audio captured (held {held:.1f}s but the stream delivered "
              f"no frames - check the mic or set CONFIG['DEVICE']).")
        return

    print("⏳ Transcribing...")

    # Run transcription in a background thread so the hotkey listener
    # stays responsive during the (potentially slow) inference.
    def _transcribe():
        global _silent_streak
        audio, rms, secs = prepare_audio(frames_snapshot)
        if audio is None or rms < CONFIG["SILENCE_THRESHOLD"]:
            _silent_streak += 1
            _report_silence(rms, secs)
            return
        _silent_streak = 0
        audio = apply_auto_gain(audio, rms)
        transcribe_and_paste(model, audio)

    threading.Thread(target=_transcribe, daemon=True).start()


def _report_silence(rms, secs):
    """Distinguish DIGITAL SILENCE (device handed us zeros) from a REAL-but-quiet
    capture, and give the matching fix. Escalates after a few in a row."""
    if rms <= CONFIG["DIGITAL_SILENCE_RMS"]:
        print(f"🔇 DIGITAL SILENCE - the mic delivered ~zero signal "
              f"(rms={rms:.4f}; {secs:.1f}s). The device, not your voice, is the "
              f"problem: another app (Discord/Steam/game) may own the mic, the "
              f"headset noise-gate is shut, or it's the wrong endpoint.")
    else:
        print(f"🔇 Too quiet - REAL signal but below the gate "
              f"(rms={rms:.4f} < {CONFIG['SILENCE_THRESHOLD']:.3f}; {secs:.1f}s). "
              f"Open the gate / raise mic level in JBL QuantumENGINE + Windows Sound, "
              f"or lower SILENCE_THRESHOLD.")
    if _silent_streak >= 3:
        print("   ↳ 3+ silent captures in a row. Run  python skald.py "
              "--test  to see the live level, or  --list  to pick another device.")


def _open_stream(device, callback=None):
    """Open the single persistent input stream. Tries CAPTURE_SAMPLERATE (None =
    native) first, then falls back through common rates so a WASAPI/MME rate quirk
    can't leave us dead. `callback` defaults to the classic-mode audio_callback;
    --stream passes its own. Returns (stream, actual_samplerate)."""
    native = int(sd.query_devices(device, "input")["default_samplerate"])
    wanted = CONFIG["CAPTURE_SAMPLERATE"]
    candidates = ([wanted] if wanted else []) + [native, 48000, 44100, 16000]
    seen, last_exc = set(), None
    for sr in candidates:
        if sr in seen:
            continue
        seen.add(sr)
        try:
            st = sd.InputStream(
                samplerate=sr,
                channels=CONFIG["CHANNELS"],
                dtype="float32",
                device=device,
                blocksize=int(sr * 0.1),
                callback=callback or audio_callback,
            )
            st.start()
            return st, sr
        except Exception as exc:
            last_exc = exc
    raise last_exc


def main(device_spec=None):
    global _stream, _capture_sr
    model = None
    if CONFIG.get("ASR_BACKEND") in ("auto", "server") and ensure_asr_server():
        print(f"⚡ ASR: GPU Vulkan server · {os.path.basename(CONFIG['ASR_SERVER_MODEL'])}")
    else:
        _print_gpu_hint()
        model = load_model()

    spec = device_spec if device_spec is not None else CONFIG["DEVICE"]
    try:
        dev_index, dev_name, ha_name = resolve_device(spec)
    except SystemExit as exc:
        print(exc)
        sys.exit(1)
    except Exception as exc:
        print(f"❌ Could not resolve a microphone: {exc}")
        print("   Run:  python skald.py --list")
        sys.exit(1)

    # Open ONE input stream for the whole session and keep it running. The callback
    # buffers audio only while the hotkey is held. (Re-opening a fresh stream per
    # key-press was the OLD bug - left the device delivering zeros after the first
    # clip; see audio_callback.) We pin a specific endpoint + host API now, because
    # defaulting to the legacy MME "Chat" virtual mic was the NEW failure mode.
    try:
        _stream, _capture_sr = _open_stream(dev_index)
    except Exception as exc:
        print(f"❌ Could not open the microphone: {exc}")
        print("   List devices:  python skald.py --list")
        print("   Then set CONFIG['DEVICE'] or pass  --device <index|name>")
        sys.exit(1)

    mic_name = dev_name
    if "chat" in dev_name.lower() or "communications" in dev_name.lower():
        print("⚠️  Heads-up: this is a voice-chat/DSP endpoint - if a headset noise-")
        print("    gate (e.g. JBL QuantumENGINE) is on, it WILL gate your dictation.")

    hotkey_name = getattr(CONFIG["HOTKEY"], "name", str(CONFIG["HOTKEY"]))
    print()
    print("━" * 50)
    print(f"  ✅ Skald ready")
    print(f"  🎤 Mic: {mic_name}  [{ha_name} @ {_capture_sr}Hz]")
    print(f"  Hold [{hotkey_name}] to record, release to paste.")
    if CONFIG.get("ENABLE_COMMANDS"):
        pfx = CONFIG["COMMAND_PREFIX"]
        print(f"  Say '{pfx} ...' for commands (open / search / send / type).")
        print(f"  e.g. '{pfx} open chrome'  ·  '{pfx} help'")
    print(f"  Press Ctrl+C to quit.")
    print("━" * 50)
    print()

    try:
        with keyboard.Listener(
            on_press=lambda key: on_press(key, model),
            on_release=lambda key: on_release(key, model),
            suppress=False,  # don't block keypresses from reaching other apps
        ) as listener:
            listener.join()
    except KeyboardInterrupt:
        print("\n👋 Skald stopped.")
    finally:
        try:
            _stream.stop()
            _stream.close()
        except Exception:
            pass
    sys.exit(0)


# ---------------------------------------------------------------------------
# Live (streaming) mode - tap-to-toggle + floating overlay + as-you-go text
# ---------------------------------------------------------------------------
def _horn(kind, sr=44100):
    """Synthesize a short Viking war-horn call. A horn is a fundamental plus decaying
    harmonics, a slow breathy attack, and slight vibrato; start = a rising call (the
    horn summons your voice), stop = a shorter falling blast an octave-ish down. The
    two cues are deliberately UNMISTAKABLE from each other so the mic state is never
    in doubt.

    SOFTENED 2026-07-10 (Rath: the blast was too hard): darker harmonic mix, the
    brightness now DECAYS over the note like a real horn losing its edge, a slower
    breathier swell instead of a punch, and a longer natural tail."""
    if kind == "start":
        dur, f0, f1 = 0.62, 146.8, 196.0     # D3 rising to G3: the summons
    else:
        dur, f0, f1 = 0.42, 130.8, 98.0      # C3 falling to G2: the release
    n = int(sr * dur)
    t = np.linspace(0, dur, n, endpoint=False)
    # Pitch glides between the two notes; vibrato gives it lungs.
    glide = f0 + (f1 - f0) * (t / dur) ** 1.4
    vib = 1.0 + 0.005 * np.sin(2 * np.pi * 5.2 * t)
    phase = 2 * np.pi * np.cumsum(glide * vib) / sr
    wave = np.zeros(n, dtype=np.float64)
    # Darker mix than v1.2 (the 6-harmonic stack rasped); upper partials also fade
    # over the note (exp decay per harmonic) so the call mellows instead of blaring.
    for h, g in ((1, 1.00), (2, 0.42), (3, 0.16), (4, 0.06), (5, 0.02)):
        bright = np.exp(-(h - 1) * 1.8 * t) if h > 1 else 1.0
        wave += g * bright * np.sin(phase * h)
    # Slow breathy swell and a long natural tail instead of an electronic gate.
    atk = int(sr * (0.20 if kind == "start" else 0.09))
    env = np.ones(n)
    env[:atk] = np.linspace(0, 1, atk) ** 2.2
    rel = int(sr * 0.18 if kind == "start" else sr * 0.14)
    env[-rel:] *= np.linspace(1, 0, rel) ** 1.2
    wave *= env
    wave /= max(1e-9, np.abs(wave).max())
    return wave.astype(np.float32)


def _chime(kind="stop"):
    """War-horn cue on record toggle: a rising horn call = the mic is OPEN, a short
    falling blast = it is CLOSED. Plays through sounddevice's DEFAULT OUTPUT device
    (your headset), on a daemon thread, fully guarded so it never delays or breaks
    the record toggle. No-op unless CHIME."""
    if not CONFIG.get("CHIME"):
        return
    def _play():
        try:
            amp = float(CONFIG.get("CHIME_VOLUME", 0.3))
            sd.play(amp * _horn(kind), 44100)   # default output = your headset
        except Exception:
            pass
    threading.Thread(target=_play, daemon=True).start()


def _load_whisper(size):
    """Build + warm up a Whisper model of the given size (shares CONFIG settings)."""
    m = WhisperModel(size, device=CONFIG["INFERENCE_DEVICE"],
                     compute_type=CONFIG["COMPUTE_TYPE"], cpu_threads=CONFIG["CPU_THREADS"])
    list(m.transcribe(np.zeros(CONFIG["SAMPLE_RATE"], dtype=np.float32),
                      beam_size=1, language=CONFIG["LANGUAGE"])[0])  # warm the kernels
    return m


# (Phrase dividers were removed by owner feedback: the transcript flows as one passage.)


class LiveSession:
    """Owns --stream state across four threads, talking only via queues:
      • PortAudio callback (audio_cb) - push chunks while recording + the live level
      • pynput listener   (on_press)  - tap toggles recording on/off
      • worker thread     (_run_worker) - segment on pauses, transcribe, finalize+paste
      • main thread       (the _Overlay tkinter loop) - drains ui_q, draws the meter
    Each recording gets a FRESH chunk queue and the worker keeps its OWN audio +
    transcript locals, so a quick stop-then-start can't cross-contaminate runs.
    """

    WAVE_BARS = 64        # rolling RMS samples backing the live waveform meter

    def __init__(self, live_model, final_model, capture_sr):
        self.live_model = live_model
        self.final_model = final_model
        self.capture_sr = capture_sr
        self.recording = False
        self.key_armed = False
        self.level = 0.0
        # Rolling window of recent RMS values that drives the live waveform meter. The
        # PortAudio thread only APPENDS here (cheap, lock-free); the tkinter thread reads
        # a snapshot at ~30fps to draw. Nothing on the ASR/paste hot path touches it.
        self.rms_history = collections.deque([0.0] * self.WAVE_BARS,
                                             maxlen=self.WAVE_BARS)
        self.active_q = None             # audio_cb enqueues here ONLY while non-None
        self.chunk_q = None              # current run's queue (for the stop sentinel)
        self.ui_q = queue.Queue()
        self.worker = None
        self.record_start = 0.0          # perf_counter() at tap-on (drives the timer)
        self.capture_path = None         # lazily-opened dated transcript file
        self.capture_lock = threading.Lock()
        # Serializes ALL model.transcribe() + paste calls. A fast stop-then-start can
        # leave the old worker still finalizing while a new one streams; without this
        # they'd call transcribe() on the same CTranslate2 model concurrently (can
        # segfault) and interleave clipboard writes. The lock makes both safe.
        self.engine_lock = threading.Lock()

    # ---- PortAudio thread ----
    def audio_cb(self, indata, frames, time_info, status):
        mono = indata.astype(np.float32).flatten()
        self.level = float(np.sqrt(np.mean(mono ** 2))) if mono.size else 0.0
        # Feed the waveform: split this block into a few sub-windows and append each
        # one's RMS, so the meter dances at a livelier rate than the 10Hz block cadence.
        # Pure append onto a deque; the drawing all happens later on the tkinter thread.
        if mono.size:
            parts = np.array_split(mono, 4)
            for p in parts:
                if p.size:
                    self.rms_history.append(float(np.sqrt(np.mean(p ** 2))))
        q = self.active_q                # single atomic read snapshots the run's queue,
        if q is not None:                # so even if _start swaps in a new queue mid-call
            q.put(indata.copy())         # a stale tail chunk can't leak into the new run

    # ---- pynput thread ----
    def on_press(self, key):
        if key != CONFIG["HOTKEY"] or self.key_armed:
            return
        self.key_armed = True            # one tap = one toggle; ignore OS auto-repeat
        self.toggle()

    def on_release(self, key):
        if key == CONFIG["HOTKEY"]:
            self.key_armed = False

    def toggle(self):
        self._stop() if self.recording else self._start()

    def _start(self):
        q = queue.Queue()                # fresh queue isolates this run
        self.chunk_q = q
        self.record_start = time.perf_counter()
        self._capture(header=True)       # stamp a timestamp header for this recording
        self.worker = threading.Thread(target=self._run_worker, args=(q,), daemon=True)
        self.ui_q.put(("reset", None))
        self.ui_q.put(("status", ("listening", "● Listening")))
        self.worker.start()
        self.recording = True
        self.active_q = q                # set LAST: audio_cb enqueues only once ready
        _chime("start")                  # rising cue: now listening

    def _stop(self):
        self.active_q = None             # FIRST: audio_cb stops enqueuing immediately
        self.recording = False
        self.ui_q.put(("status", ("finalizing", "⏳ Finalizing")))
        if self.chunk_q is not None:
            self.chunk_q.put(None)       # sentinel unblocks the worker -> finalize
        _chime("stop")                   # falling cue: stopped listening

    def _capture(self, text="", header=False):
        """Append a phrase (or a per-recording timestamp header) to today's transcript
        file the moment it's heard. This is the never-lose-a-brainstorm guarantee: the
        words hit disk independent of where the paste lands, so a misclick / crash /
        accidental close can't lose them. Fully isolated - any failure is swallowed so
        capture can never break dictation."""
        if not CONFIG.get("SAVE_TRANSCRIPTS"):
            return
        if not header and not (text or "").strip():
            return
        try:
            with self.capture_lock:
                if self.capture_path is None:
                    d = CONFIG["TRANSCRIPT_DIR"]
                    os.makedirs(d, exist_ok=True)
                    self._prune_old(d)   # housekeeping, once per session
                    self.capture_path = os.path.join(
                        d, f"skald-{datetime.datetime.now():%Y-%m-%d}.md")
                with open(self.capture_path, "a", encoding="utf-8") as f:
                    if header:
                        f.write(f"\n\n## {datetime.datetime.now():%H:%M:%S}\n\n")
                    else:
                        f.write(text.strip() + "\n")
        except Exception:
            pass                         # capture must never break dictation

    def _prune_old(self, d):
        """Delete skald-*.md older than TRANSCRIPT_RETENTION_DAYS so months of daily
        brainstorming can't pile up unbounded. 0 = keep forever. Fully guarded."""
        days = CONFIG.get("TRANSCRIPT_RETENTION_DAYS", 0)
        if not days or days <= 0:
            return
        cutoff = time.time() - days * 86400
        try:
            for name in os.listdir(d):
                if name.startswith("skald-") and name.endswith(".md"):
                    p = os.path.join(d, name)
                    try:
                        if os.path.getmtime(p) < cutoff:
                            os.remove(p)
                    except Exception:
                        pass
        except Exception:
            pass

    # ---- worker thread (self-contained per run) ----
    def _run_worker(self, q):
        speech_rms = CONFIG["STREAM_SPEECH_RMS"]
        need_sil = CONFIG["PHRASE_SILENCE_MS"]
        min_sp = CONFIG["PHRASE_MIN_MS"]
        max_s = CONFIG["PHRASE_MAX_SECONDS"]
        max_full = int(CONFIG["MAX_RECORD_SECONDS"] * self.capture_sr)
        transcript, full, full_n = [], [], 0
        phrase, sil_ms, sp_ms = [], 0.0, 0.0
        while True:
            try:
                chunk = q.get(timeout=0.25)
            except queue.Empty:
                continue
            if chunk is None:
                break                    # stop sentinel
            if full_n < max_full:        # cap session audio so a long hold can't grow
                full.append(chunk)       # memory without bound (the final pass trims to
                full_n += len(chunk)     # the same first-N samples anyway)
            phrase.append(chunk)
            dur_ms = len(chunk) / self.capture_sr * 1000.0
            rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
            if rms >= speech_rms:
                sp_ms += dur_ms
                sil_ms = 0.0
            else:
                sil_ms += dur_ms
            phrase_s = sum(len(c) for c in phrase) / self.capture_sr
            # Flush on a real pause (needs some speech first), OR force-flush a long
            # phrase regardless of level - so a quiet/gated monologue still streams and
            # `phrase` can never grow unbounded waiting for speech that reads as silence.
            if (sp_ms >= min_sp and sil_ms >= need_sil) or phrase_s >= max_s:
                self._emit_phrase(phrase, transcript)
                phrase, sil_ms, sp_ms = [], 0.0, 0.0
        if phrase and sp_ms >= min_sp:   # trailing speech captured before the stop
            self._emit_phrase(phrase, transcript)
        self._finalize(full, transcript)

    def _transcribe(self, model, audio):
        if audio.size == 0:
            return ""
        with self.engine_lock:           # serializes model calls AND keeps pastes ordered
            return transcribe_any(model, audio)

    def _emit_phrase(self, phrase_chunks, transcript):
        audio = np.concatenate(phrase_chunks, axis=0).flatten().astype(np.float32)
        audio = apply_auto_gain(resample_to_16k(audio, self.capture_sr))
        text = self._transcribe(self.live_model, audio)
        if text:
            transcript.append(text)
            self._capture(text)          # persist THIS phrase now (crash/misclick-proof)
            # Display flows phrases together as one passage, exactly like the paste.
            display = " ".join(transcript)
            self.ui_q.put(("partial", display))

    def _finalize(self, full_chunks, transcript):
        final_text = " ".join(transcript).strip()
        # With the GPU server active, the full-clip accuracy pass costs ~0.5s even on a
        # 20s clip - so ALWAYS run it there (big-model quality, still feels instant).
        # On CPU it only runs if FINAL_MODEL is set (None = paste the live text as-is).
        if (CONFIG["FINAL_MODEL"] or gpu_asr_active()) and full_chunks:
            audio = np.concatenate(full_chunks, axis=0).flatten().astype(np.float32)
            audio = resample_to_16k(audio, self.capture_sr)
            maxs = int(CONFIG["MAX_RECORD_SECONDS"] * 16000)
            if audio.size > maxs:
                audio = audio[:maxs]
            # Gain off the VOICED part only - averaging RMS over the inter-phrase
            # silence would dilute it and over-boost vs. the per-phrase live passes.
            voiced = audio[np.abs(audio) > 0.005]
            rms = (float(np.sqrt(np.mean(voiced ** 2))) if voiced.size
                   else float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0)
            audio = apply_auto_gain(audio, rms)
            refined = self._transcribe(self.final_model, audio)
            if refined:
                final_text = refined
        if final_text:
            self.ui_q.put(("final", final_text))
            with self.engine_lock:       # atomic copy+paste - no interleave with a
                if not (CONFIG.get("ENABLE_COMMANDS") and route_command(final_text)):
                    _paste_text(final_text)   # concurrent run's paste
            self.ui_q.put(("status", ("idle", "✓ Pasted, tap to talk")))
        else:
            self.ui_q.put(("final", ""))
            self.ui_q.put(("status", ("idle", "nothing heard, tap to talk")))


class _Overlay:
    """Frameless, always-on-top, NON-focus-stealing status window. Shows a runic
    title row, a live waveform meter, and the running transcript. Runs on the main
    thread; pulls cross-thread updates off sess.ui_q each tick.

    Palette matched to the app icon: charcoal stone, muted-gold border, warm off-white
    body text, gold runic accents. The waveform picks its blue/amber glow stack from
    CONFIG['THEME_ACCENT'] (blue voice-energy by default); runes stay gold either way.
    """

    BG, FG, SUB, DIM = "#23252B", "#EDE5D3", "#9A96A2", "#34343C"
    BORDER = "#9A7D33"                       # thin gold window border line
    GOLD, GOLD_HI = "#E8C766", "#FFE28A"     # runes and chrome: true gold, not orange
    ACC, WARN = GOLD, GOLD_HI                # status/timer accent + near-cap highlight
    # Waveform glow stacks (dim -> mid -> bright core), drawn as three layered widths to
    # fake a glow since tkinter has no alpha. "gold" = molten gold (the default);
    # "ice" = blue voice-energy; "amber" = the older warm stack.
    WAVE = {"gold":  ("#8A6A1F", "#D4AF37", "#FFE082"),
            "ice":   ("#1E4A66", "#3E7FA8", "#8FD4F5"),
            "amber": ("#6B4E1F", "#B07E2E", "#F5C879")}
    SB_TRACK, SB_THUMB, SB_W = "#2E2A22", "#E8C766", 14   # scrollbar: track, gold thumb, width

    MIN_W, MIN_H = 280, 140

    def __init__(self, root, sess, subtitle):
        import tkinter as tk
        self.root, self.sess, self.on_quit = root, sess, None
        self._alive = True
        self._minimized = False          # True between ─ click and taskbar restore
        c = CONFIG
        root.title("Skald - Live")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        # Window icon: the carved Norse horn. Optional, so a missing file never blocks.
        try:
            root.iconbitmap(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "assets", "skald.ico"))
        except Exception:
            pass
        try:
            root.attributes("-alpha", c["OVERLAY_OPACITY"])
        except Exception:
            pass
        # Waveform glow stack for the selected accent (falls back to gold).
        self.wave_stack = self.WAVE.get(CONFIG.get("THEME_ACCENT", "gold"),
                                        self.WAVE["gold"])
        # Sprite-wave frame bank (Rath's soundwave art); None -> bar-meter fallback.
        self._sprites = self._load_wave_sprites()
        self._phase = 0.0                # drives the idle shimmer on the waveform
        # Geometry: last session's size/position wins (overlay_state.json beside the
        # script); CONFIG provides the first-run defaults. Clamped on-screen + to min.
        st = self._load_state()
        w = max(self.MIN_W, int(st.get("w", c["OVERLAY_WIDTH"])))
        h = max(self.MIN_H, int(st.get("h", c["OVERLAY_HEIGHT"])))
        x = st.get("x", root.winfo_screenwidth() - w - 40)
        y = st.get("y", root.winfo_screenheight() - h - 90)
        x = min(max(0, x), max(0, root.winfo_screenwidth() - self.MIN_W))
        y = min(max(0, y), max(0, root.winfo_screenheight() - self.MIN_H))
        root.geometry(f"{w}x{h}+{x}+{y}")

        frame = tk.Frame(root, bg=self.BG, highlightthickness=1,
                         highlightbackground=self.BORDER)
        frame.pack(fill="both", expand=True)

        # ---- title row: runic brand on the left, controls on the right ----
        top = tk.Frame(frame, bg=self.BG)
        top.pack(fill="x", padx=12, pady=(8, 2))
        # "ᛋᚲᚨᛚᛞ" is the word SKALD in Elder Futhark runes (Unicode text glyphs), gold.
        brand = tk.Label(top, text="ᛋᚲᚨᛚᛞ  SKALD", bg=self.BG, fg=self.GOLD,
                         font=("Segoe UI", 11, "bold"))
        brand.pack(side="left")
        # Close control: gold ✕ that brightens on hover.
        quit_lbl = tk.Label(top, text="✕", bg=self.BG, fg=self.GOLD,
                            font=("Segoe UI", 11, "bold"), cursor="hand2")
        quit_lbl.pack(side="right")
        quit_lbl.bind("<Button-1>", lambda e: self._quit())
        quit_lbl.bind("<Enter>", lambda e: quit_lbl.config(fg=self.GOLD_HI))
        quit_lbl.bind("<Leave>", lambda e: quit_lbl.config(fg=self.GOLD))
        # ─ minimize: parks the overlay as a normal taskbar button; click it to bring
        # the overlay back (frameless + no-focus styles re-apply on restore).
        min_lbl = tk.Label(top, text="─", bg=self.BG, fg=self.SUB,
                           font=("Segoe UI", 11, "bold"), cursor="hand2")
        min_lbl.pack(side="right", padx=(0, 10))
        min_lbl.bind("<Button-1>", lambda e: self._minimize())
        # Click-to-copy: drop the last transcript on the clipboard (no paste) so you
        # can click the right field yourself and Ctrl+V, a rescue for when it pasted
        # into the wrong app because focus was not where you thought.
        self.last_text = ""
        self.copy_lbl = tk.Label(top, text="⎘ copy", bg=self.BG, fg=self.SUB,
                                 font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.copy_lbl.pack(side="right", padx=(0, 12))
        self.copy_lbl.bind("<Button-1>", self._copy)

        # ---- status row (listening state + live timer) ----
        status_row = tk.Frame(frame, bg=self.BG)
        status_row.pack(fill="x", padx=12, pady=(0, 2))
        self.status = tk.Label(status_row, text="Tap to talk", bg=self.BG, fg=self.ACC,
                               font=("Segoe UI", 10, "bold"))
        self.status.pack(side="left")

        # ---- live waveform meter (the centerpiece) ----
        self.canvas = tk.Canvas(frame, height=40, bg=self.BG, highlightthickness=0)
        self.canvas.pack(fill="x", padx=12, pady=2)

        # Running transcript: a scrollable, read-only Text that shows the WHOLE
        # brainstorm and auto-follows the newest text unless you scroll up to re-read.
        # The wheel scrolls even without focus (the overlay never takes focus).
        txt_wrap = tk.Frame(frame, bg=self.BG)
        txt_wrap.pack(fill="both", expand=True, padx=12, pady=(4, 10))
        # Custom scrollbar: a bold gold thumb on a dark track drawn on a Canvas, wide
        # enough to click, with a minimum grab height. Drag it or wheel to scroll.
        self._sb_first, self._sb_last = 0.0, 1.0
        self.sb = tk.Canvas(txt_wrap, width=self.SB_W, bg=self.SB_TRACK,
                            highlightthickness=0, bd=0, cursor="hand2")
        self.sb.pack(side="right", fill="y")
        # Manuscript-flavored body face: Palatino ships with Windows and reads like a
        # printed saga; tkinter silently falls back to the default family if missing.
        self.text = tk.Text(txt_wrap, bg=self.BG, fg=self.FG, wrap="word",
                            font=("Palatino Linotype", 12), relief="flat", bd=0,
                            highlightthickness=0, padx=2, pady=2, cursor="arrow",
                            insertwidth=0, spacing2=2, spacing3=4,
                            yscrollcommand=self._sb_set)
        self.text.pack(side="left", fill="both", expand=True)
        self.text.insert("1.0", subtitle)
        self.text.config(state="disabled")
        self.text.bind("<MouseWheel>", self._wheel)
        self.sb.bind("<MouseWheel>", self._wheel)
        self.sb.bind("<Button-1>", self._sb_jump)
        self.sb.bind("<B1-Motion>", self._sb_jump)
        self.sb.bind("<Configure>", lambda e: self._sb_draw())

        self._dx = self._dy = 0
        # Drag from the chrome (frame/title/brand/status) to move; the transcript area
        # is left to the Text widget so its own scroll works.
        for wdg in (frame, top, brand, status_row, self.status):
            wdg.bind("<Button-1>", self._press)
            wdg.bind("<B1-Motion>", self._drag)
            wdg.bind("<ButtonRelease-1>", lambda e: self._save_state())

        # Resize grip (bottom-right): a small triangle of three short gold line segments
        # drawn on a tiny Canvas. A frameless window has no OS resize border, so the grip
        # IS the resize affordance. Size persists via overlay_state.json.
        self._gw = self._gh = self._gx = self._gy = 0
        grip = tk.Canvas(frame, width=16, height=16, bg=self.BG,
                         highlightthickness=0, bd=0, cursor="size_nw_se")
        grip.place(relx=1.0, rely=1.0, anchor="se")
        for off in (2, 7, 12):               # three stepped diagonals -> a corner wedge
            grip.create_line(14, off, off, 14, fill=self.GOLD, width=1)
        grip.bind("<Button-1>", self._grip_press)
        grip.bind("<B1-Motion>", self._grip_drag)
        grip.bind("<ButtonRelease-1>", lambda e: self._save_state())

        # Restore-from-taskbar is detected by POLLING root.state() (see _watch_restore),
        # a <Map> binding is a trap here: toggling overrideredirect remaps the window
        # mid-minimize and fires a premature restore.
        root.bind("<Configure>", self._on_configure)

        root.after(80, self._no_activate)
        root.after(50, self._tick)
        root.after(33, self._wave_loop)      # ~30fps waveform, independent of _tick

    def _apply_exstyle(self, noactivate, toolwindow):
        """Set/clear WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW on the CURRENT native window
        (re-queried each call - toggling overrideredirect recreates the hwnd). Returns
        the resulting style bits, or None if unavailable (non-Windows / no ctypes)."""
        try:
            import ctypes
            u = ctypes.windll.user32
            hwnd = self.root.winfo_id()
            parent = u.GetParent(hwnd)
            target = parent if parent else hwnd
            GWL_EXSTYLE, NOACT, TOOLWIN = -20, 0x08000000, 0x00000080
            cur = u.GetWindowLongW(target, GWL_EXSTYLE)
            new = (cur | NOACT) if noactivate else (cur & ~NOACT)
            new = (new | TOOLWIN) if toolwindow else (new & ~TOOLWIN)
            u.SetWindowLongW(target, GWL_EXSTYLE, new)
            # SWP_NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED - make the new style take effect.
            u.SetWindowPos(target, 0, 0, 0, 0, 0, 0x0027)
            return u.GetWindowLongW(target, GWL_EXSTYLE)
        except Exception:
            return None

    def _no_activate(self):
        """Windows: WS_EX_NOACTIVATE + TOOLWINDOW so showing/clicking the overlay
        never steals focus (the paste lands in your real app) and it stays off the
        taskbar while visible. Degrades silently on non-Windows."""
        got = self._apply_exstyle(noactivate=True, toolwindow=True)
        if got is None:
            print("⚠️  Overlay focus-guard unavailable; paste should still work - "
                  "click your target app if it doesn't.")
        elif not (got & 0x08000000):
            print("⚠️  Overlay couldn't claim no-focus mode - if a paste ever lands "
                  "in the overlay, click your target app first, then tap to talk.")

    # ---- minimize to taskbar / restore ----
    def _minimize(self):
        """─ button: park the overlay on the taskbar. A frameless (override-redirect)
        window can't iconify and a TOOLWINDOW has no taskbar button - so flip BOTH off
        for the minimized stretch, then <Map> (the taskbar-click restore) undoes it."""
        if self._minimized:
            return
        self._save_state()
        self._minimized = True
        try:
            self.root.overrideredirect(False)      # give Windows a real, iconifiable window
            self._apply_exstyle(noactivate=False, toolwindow=False)  # -> taskbar button
            self.root.iconify()
            self.root.after(200, self._watch_restore)
        except Exception:
            self._minimized = False                # couldn't minimize - stay visible

    def _watch_restore(self):
        """Poll while parked on the taskbar; the moment the user restores it
        (state leaves 'iconic'), rebuild the frameless no-focus overlay."""
        if not self._alive or not self._minimized:
            return
        try:
            if self.root.state() == "iconic":
                self.root.after(200, self._watch_restore)
                return
        except Exception:
            return
        self._restore()

    def _restore(self):
        """Back from the taskbar: re-frameless + re-apply the no-focus style -
        toggling overrideredirect gave us a NEW native window."""
        self._minimized = False
        st = self._load_state()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", CONFIG["OVERLAY_OPACITY"])
        except Exception:
            pass
        if st:                                     # decorated->frameless can shift origin
            self.root.geometry(f"{max(self.MIN_W, st['w'])}x{max(self.MIN_H, st['h'])}"
                               f"+{st['x']}+{st['y']}")
        self.root.after(60, self._no_activate)

    # ---- resize grip ----
    def _grip_press(self, e):
        self._gw, self._gh = self.root.winfo_width(), self.root.winfo_height()
        self._gx, self._gy = self.root.winfo_pointerx(), self.root.winfo_pointery()

    def _grip_drag(self, e):
        w = max(self.MIN_W, self._gw + (self.root.winfo_pointerx() - self._gx))
        h = max(self.MIN_H, self._gh + (self.root.winfo_pointery() - self._gy))
        self.root.geometry(f"{w}x{h}")

    def _on_configure(self, e=None):
        # The Text widget word-wraps to its own width automatically. The wave canvas,
        # though, grows with the window: ~22% of window height, floor 40, cap 160, so
        # a taller window means a thicker, more detailed current.
        try:
            wave_h = max(40, min(160, int(self.root.winfo_height() * 0.22)))
            if wave_h != getattr(self, "_wave_h", 40):
                self._wave_h = wave_h
                self.canvas.configure(height=wave_h)
        except Exception:
            pass

    def _wheel(self, e):
        """Mouse-wheel scroll the transcript even though the overlay never holds focus."""
        try:
            self.text.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass
        return "break"

    def _set_text(self, t):
        """Replace the transcript, preserving the reader's scroll position unless they
        were already at the bottom (then follow the newest text). Read-only: flip to
        normal to write, back to disabled so it can't be edited."""
        try:
            at_bottom = self.text.yview()[1] >= 0.999
        except Exception:
            at_bottom = True
        try:
            self.text.config(state="normal")
            self.text.delete("1.0", "end")
            if t:
                self.text.insert("1.0", t)
            self.text.config(state="disabled")
            if at_bottom:
                self.text.see("end")
        except Exception:
            pass

    def _sb_set(self, first, last):
        """yscrollcommand from the Text: remember the visible fraction and redraw."""
        self._sb_first, self._sb_last = float(first), float(last)
        self._sb_draw()

    def _sb_draw(self):
        """Draw the gold thumb over the range the Text currently shows. Hidden when
        everything fits; a minimum height keeps it grabbable with lots of text."""
        if not self._alive:
            return
        try:
            c = self.sb
            c.delete("all")
            h = c.winfo_height()
            w = self.SB_W
            if h <= 1 or (self._sb_first <= 0.0 and self._sb_last >= 1.0):
                return                       # nothing to scroll -> no thumb
            top, bot = self._sb_first * h, self._sb_last * h
            if bot - top < 26:               # enforce a grabbable minimum height
                mid = (top + bot) / 2
                top = max(0, min(mid - 13, h - 26))
                bot = top + 26
            pad = 2
            c.create_rectangle(pad, top + pad, w - pad, bot - pad,
                               fill=self.SB_THUMB, outline="")
        except Exception:
            pass

    def _sb_jump(self, e):
        """Click or drag the track: center the visible window on the cursor."""
        try:
            h = self.sb.winfo_height()
            if h <= 1:
                return
            span = max(0.0, self._sb_last - self._sb_first)
            self.text.yview_moveto(max(0.0, min(1.0, e.y / h - span / 2)))
        except Exception:
            pass

    # ---- geometry persistence (position + size survive restarts) ----
    @staticmethod
    def _state_path():
        import os
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "overlay_state.json")

    @staticmethod
    def _load_state():
        import json
        try:
            with open(_Overlay._state_path(), encoding="utf-8") as f:
                st = json.load(f)
            return {k: int(st[k]) for k in ("w", "h", "x", "y")}
        except Exception:
            return {}

    def _save_state(self):
        import json
        if self._minimized or not self._alive:
            return
        try:
            st = {"w": self.root.winfo_width(), "h": self.root.winfo_height(),
                  "x": self.root.winfo_x(), "y": self.root.winfo_y()}
            if st["w"] >= self.MIN_W and st["h"] >= self.MIN_H:
                with open(self._state_path(), "w", encoding="utf-8") as f:
                    json.dump(st, f)
        except Exception:
            pass

    def _press(self, e):
        # Offset of the cursor from the WINDOW origin (not the grabbed child widget),
        # so dragging from the transcript label doesn't teleport the overlay.
        self._dx = self.root.winfo_pointerx() - self.root.winfo_rootx()
        self._dy = self.root.winfo_pointery() - self.root.winfo_rooty()

    def _drag(self, e):
        self.root.geometry(f"+{self.root.winfo_pointerx() - self._dx}"
                           f"+{self.root.winfo_pointery() - self._dy}")

    def _copy(self, e=None):
        """Copy the last transcript to the clipboard (NO paste). Uses pyperclip so it
        survives the app closing; falls back to the Tk clipboard if that fails."""
        txt = self.last_text
        if not txt:
            self._flash_copy("nothing yet", self.SUB)
            return
        try:
            pyperclip.copy(txt)
        except Exception:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(txt)
            except Exception:
                self._flash_copy("copy failed", self.WARN)
                return
        self._flash_copy("✓ copied", self.ACC)

    def _flash_copy(self, text, color):
        self.copy_lbl.config(text=text, fg=color)
        self.root.after(1200, lambda: self._alive and self.copy_lbl.config(
            text="⎘ copy", fg=self.SUB))

    def _quit(self):
        self._save_state()               # remember size/position for next launch
        self._alive = False              # stop ticks from touching torn-down widgets
        if self.on_quit:
            self.on_quit()
        else:
            try:
                self.root.destroy()
            except Exception:
                pass

    def _tick(self):
        if not self._alive:
            return                       # quit in progress - don't re-arm or touch UI
        try:
            try:
                while True:
                    kind, payload = self.sess.ui_q.get_nowait()
                    if kind == "status":
                        state, txt = payload
                        color = {"listening": self.ACC, "finalizing": self.WARN,
                                 "idle": self.SUB}.get(state, self.FG)
                        self.status.config(text=txt, fg=color)
                    elif kind == "reset":
                        self._set_text("")
                    elif kind in ("partial", "final"):
                        t = payload or ""
                        if t:
                            self.last_text = t          # remember full text for ⎘ copy
                        self._set_text(t)               # full transcript, scrollable
            except queue.Empty:
                pass
            # While recording, show a live elapsed timer in the status line (highlight as
            # it nears the MAX_RECORD_SECONDS trim cap). Runs after the queue drain so it
            # wins over the static "Listening" message set at tap-on.
            if self.sess.recording and self.sess.record_start:
                el = int(time.perf_counter() - self.sess.record_start)
                m, s = divmod(el, 60)
                near_cap = el >= CONFIG["MAX_RECORD_SECONDS"] - 30
                self.status.config(text=f"● Listening  {m}:{s:02d}",
                                   fg=self.WARN if near_cap else self.ACC)
        except Exception:
            return                       # widgets torn down mid-tick, stop quietly
        self.root.after(50, self._tick)

    # ---- waveform meter (own ~30fps loop, off the ASR/paste hot path) ----
    def _wave_loop(self):
        """Redraw the waveform at ~30fps. Reads a snapshot of the session's RMS deque
        (filled by the audio thread) and does ALL canvas work here on the tk thread."""
        if not self._alive:
            return
        self._phase += 0.35
        try:
            if self._sprites:
                self._draw_wave_sprite()
            else:
                self._draw_waveform()
        except Exception:
            pass
        self.root.after(33, self._wave_loop)

    def _load_wave_sprites(self):
        """Load the pre-rendered sprite-wave frame bank (assets/wave). Returns None
        when the bank is missing so the overlay falls back to the bar meter."""
        import tkinter as tk
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "wave")
        try:
            star = [tk.PhotoImage(file=os.path.join(base, f"star_{i}.png"))
                    for i in range(3)]
            stream = {(li, p): tk.PhotoImage(
                          file=os.path.join(base, f"stream_L{li}_P{p}.png"))
                      for li in range(5) for p in range(8)}
            return {"star": star, "stream": stream,
                    "star_w": star[1].width(), "stream_w": stream[(0, 0)].width()}
        except Exception:
            return None

    # Elder Futhark glyphs the drift layer writes with (plain text items, gold ink).
    RUNES = "ᚠᚢᚦᚨᚱᚲᚷᚹᚾᛁᛃᛈᛊᛏᛒᛖᛗᛚᛜᛞᛟ"

    def _draw_wave_sprite(self):
        """Rath's soundwave art, alive: the starburst breathes at the left, the stream
        scrolls RIGHTWARD and swells with the voice (5 amplitude levels x 8 phases from
        the shipped frame bank), and gold runes spawn off the star and drift down the
        current when you speak. All work stays on the tk thread; the audio callback
        only feeds the RMS deque."""
        import random
        c = self.canvas
        vals = list(self.sess.rms_history)
        raw = min(max((max(vals[-6:]) if vals else 0.0) / 0.08, 0.0), 1.0)
        if not self.sess.recording:
            raw = 0.0
        # Smooth so the wave swells and settles instead of twitching.
        self._level = 0.82 * getattr(self, "_level", 0.0) + 0.18 * raw
        lv = self._level
        w = c.winfo_width() or (CONFIG["OVERLAY_WIDTH"] - 24)
        ch = c.winfo_height() or 40
        mid = ch // 2
        sp = self._sprites
        c.delete("all")
        # Vertical stretch: taller canvas -> integer vertical zoom of the frames, so
        # the ribbons genuinely thicken as the window grows. Zoomed frames are cached.
        sy = max(1, round(ch / 40))
        zc = getattr(self, "_zoom_cache", None)
        if zc is None:
            zc = self._zoom_cache = {}
        # Stream tiles scroll rightward; speaking speeds the current up.
        self._sprite_phase = (getattr(self, "_sprite_phase", 0.0)
                              + 0.55 + 2.6 * lv) % 8.0
        li = min(4, int(round(lv * 4)))
        key = ("s", li, int(self._sprite_phase), sy)
        frame = zc.get(key)
        if frame is None:
            frame = sp["stream"][(li, int(self._sprite_phase))]
            if sy > 1:
                frame = frame.zoom(1, sy)
            zc[key] = frame
        x = sp["star_w"] * sy - 12
        while x < w:
            c.create_image(x, mid, anchor="w", image=frame)
            x += sp["stream_w"]
        # The voice-source starburst pulses with level, drawn over the stream head.
        si = 0 if lv < 0.12 else (1 if lv < 0.55 else 2)
        skey = ("star", si, sy)
        star = zc.get(skey)
        if star is None:
            star = sp["star"][si]
            if sy > 1:
                star = star.zoom(sy, sy)
            zc[skey] = star
        c.create_image(0, mid, anchor="w", image=star)
        # Rune drift: new glyphs leave the star while you speak and ride the current.
        runes = getattr(self, "_runes", None)
        if runes is None:
            runes = self._runes = []
        # Louder voice = more runes, bigger runes. Full voice can gust two per tick.
        cap = 6 + int(lv * 14)
        spawns = (1 if random.random() < 0.06 + 0.55 * lv else 0) + \
                 (1 if lv > 0.7 and random.random() < 0.35 else 0)
        if self.sess.recording:
            spread = max(mid - 6, 1)
            for _ in range(spawns):
                if len(runes) >= cap:
                    break
                runes.append({"x": float(sp["star_w"] * sy + 4),
                              "y": mid + random.randint(-spread, spread),
                              "g": random.choice(self.RUNES), "age": 0,
                              "size": 10 + int(lv * 5)})
        keep = []
        for r in runes:
            r["x"] += 1.6 + 3.4 * lv
            r["age"] += 1
            if r["x"] < w and r["age"] < 90:
                col = self.GOLD_HI if r["age"] < 24 else (
                      self.GOLD if r["age"] < 60 else "#8A6A1F")
                c.create_text(r["x"], r["y"], text=r["g"], fill=col,
                              font=("Palatino Linotype", r.get("size", 10), "bold"),
                              anchor="w")
                keep.append(r)
        self._runes = keep

    def _draw_waveform(self):
        """A center-mirrored row of slim bars whose heights follow recent RMS. Idle is a
        calm low shimmer; speech pulses the bars tall. Each bar is drawn three times
        (dim, mid, bright core) at stepped widths to fake a glow, since tkinter has no
        alpha. Bars reflow to the canvas width, so a resize just respaces them."""
        if not self._alive:
            return
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        if w <= 1:
            w = CONFIG["OVERLAY_WIDTH"] - 24
        ch = c.winfo_height() or 40
        mid = ch / 2.0
        # Newest sample renders at the LEFT and history flows rightward, so speech
        # visibly builds to the right like a line of runes being written.
        vals = list(self.sess.rms_history)[::-1]    # snapshot; audio thread only appends
        n = len(vals) or 1
        gap = 2
        bw = max(2.0, (w - (n - 1) * gap) / n)
        dim, midc, core = self.wave_stack
        recording = self.sess.recording
        import math
        for i, v in enumerate(vals):
            # Idle shimmer: a low travelling ripple so silence still breathes; speech
            # overrides it as the real RMS climbs well past the shimmer floor.
            shimmer = 0.06 + 0.04 * (0.5 + 0.5 * math.sin(self._phase - i * 0.5))
            level = min(max(v / 0.08, 0.0), 1.0)
            amp = max(shimmer, level) if recording else shimmer * 0.6
            half = amp * (mid - 2)
            x0 = i * (bw + gap)
            xc = x0 + bw / 2.0
            # Three stepped layers: widest+dim, mid, then the bright core.
            for frac, col in ((1.0, dim), (0.62, midc), (0.30, core)):
                hw = max(0.6, (bw / 2.0) * frac)
                c.create_rectangle(xc - hw, mid - half, xc + hw, mid + half,
                                   fill=col, outline="")


def run_stream_mode(device_spec=None):
    """Live mode entry point: tap to start/stop, floating overlay, as-you-go text."""
    try:
        import tkinter as tk
    except Exception as exc:
        print(f"❌ Live mode needs tkinter (bundled with standard Python): {exc}")
        sys.exit(1)

    spec = device_spec if device_spec is not None else CONFIG["DEVICE"]
    try:
        dev_index, dev_name, ha_name = resolve_device(spec)
    except SystemExit as exc:
        print(exc)
        sys.exit(1)
    except Exception as exc:
        print(f"❌ Could not resolve a microphone: {exc}")
        print("   Run:  python skald.py --list")
        sys.exit(1)

    live_model = final_model = None
    if CONFIG.get("ASR_BACKEND") in ("auto", "server") and ensure_asr_server():
        print(f"⚡ ASR: GPU Vulkan server · {os.path.basename(CONFIG['ASR_SERVER_MODEL'])} "
              f"(live phrases AND the final pass)")
    else:
        _print_gpu_hint()
        print(f"⏳ Live mode: loading CPU models (live={CONFIG['LIVE_MODEL']}, "
              f"final={CONFIG['FINAL_MODEL']}). First run downloads them once.")
        live_model = _load_whisper(CONFIG["LIVE_MODEL"])
        if CONFIG["FINAL_MODEL"] in (None, CONFIG["LIVE_MODEL"]):
            final_model = live_model
        else:
            final_model = _load_whisper(CONFIG["FINAL_MODEL"])
        print("✅ Models ready.")

    sess = LiveSession(live_model, final_model, capture_sr=16000)
    try:
        stream, sr = _open_stream(dev_index, callback=sess.audio_cb)
    except Exception as exc:
        print(f"❌ Could not open the microphone: {exc}")
        print("   Run:  python skald.py --list")
        sys.exit(1)
    sess.capture_sr = sr

    hotkey_name = getattr(CONFIG["HOTKEY"], "name", str(CONFIG["HOTKEY"]))
    root = tk.Tk()
    overlay = _Overlay(root, sess, subtitle=f"Tap [{hotkey_name}] to start, speak, "
                                            f"tap again to finish.")

    listener = keyboard.Listener(on_press=sess.on_press, on_release=sess.on_release,
                                 suppress=False)
    listener.start()

    print("\n" + "━" * 50)
    print("  ✅ Live Mode ready")
    print(f"  🎤 {dev_name}  [{ha_name} @ {sr}Hz]")
    print(f"  TAP [{hotkey_name}] to start/stop (no holding).")
    print(f"  ⎘ copy = put the last transcript on the clipboard (pasted in the wrong")
    print(f"     spot? click ⎘ copy, click the right field, then Ctrl+V).")
    print(f"  Drag the overlay to move it · click ✕ (or Ctrl+C here) to quit.")
    if "chat" in dev_name.lower():
        print("  ⚠  Gated 'Chat' endpoint. Open the headset mic gate if it is quiet.")
    print("━" * 50 + "\n")

    def on_quit():
        overlay._alive = False           # stop ticks touching dead widgets
        try:
            listener.stop()
        except Exception:
            pass
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    overlay.on_quit = on_quit
    # A no-op periodic callback keeps the interpreter ticking so Ctrl+C in the
    # console can still interrupt tkinter's mainloop on Windows.
    def _heartbeat():
        if not overlay._alive:
            return
        root.after(200, _heartbeat)
    root.after(200, _heartbeat)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        on_quit()
    print("\n👋 Live Mode stopped.")
    sys.exit(0)


if __name__ == "__main__":
    # Force UTF-8 stdout/stderr so the emoji + meter bars never crash on a legacy
    # cp1252 console (Python <3.15 defaults to the OEM codepage when piped). If the
    # stream can't be reconfigured (e.g. it's a StringIO under capture), wrap its
    # buffer so output degrades to replacement chars instead of dying mid-print.
    import io
    for _name in ("stdout", "stderr"):
        _s = getattr(sys, _name)
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                setattr(sys, _name, io.TextIOWrapper(
                    _s.buffer, encoding="utf-8", errors="replace", line_buffering=True))
            except Exception:
                pass

    import argparse

    ap = argparse.ArgumentParser(
        description="Skald - push-to-talk speech-to-text with auto-paste.")
    ap.add_argument("--list", action="store_true",
                    help="list input devices (with host API + gate warnings) and exit")
    ap.add_argument("--doctor", action="store_true",
                    help="run a self-diagnostic (deps, mic, model cache, clipboard) and exit")
    ap.add_argument("--test", action="store_true",
                    help="live mic level meter to tune your noise gate, then exit")
    ap.add_argument("--device",
                    help="input device: index (e.g. 18) or name substring (e.g. JBL)")
    ap.add_argument("--threshold", type=float,
                    help="override SILENCE_THRESHOLD for this run")
    ap.add_argument("--model",
                    help="Whisper model override, e.g. base.en (fastest sane) / small.en "
                         "(default) / medium.en (accurate but 0.6x realtime on this CPU)")
    ap.add_argument("--max-seconds", type=int, dest="max_seconds",
                    help="max recording length before a clip is trimmed (default 600)")
    ap.add_argument("--stream", action="store_true",
                    help="(now the DEFAULT) LIVE mode: tap to start/stop, floating overlay")
    ap.add_argument("--classic", action="store_true",
                    help="classic hold-to-talk mode: no window, hold the hotkey, release to paste")
    ap.add_argument("--backend", choices=["auto", "server", "faster-whisper"],
                    help="ASR backend override: auto (GPU server w/ CPU fallback, default) / "
                         "server (GPU only) / faster-whisper (CPU only)")
    ap.add_argument("--chime", action="store_true",
                    help="force the record-toggle chime on (it's on by default)")
    ap.add_argument("--no-chime", action="store_true", dest="no_chime",
                    help="silence the record-toggle chime for this run")
    ap.add_argument("--no-save", action="store_true", dest="no_save",
                    help="don't save transcripts to disk this run (default: save to "
                         "~/Documents/Skald Transcripts so a brainstorm is never lost)")
    args = ap.parse_args()

    if args.backend:
        CONFIG["ASR_BACKEND"] = args.backend
    if args.chime:
        CONFIG["CHIME"] = True
    if args.no_chime:
        CONFIG["CHIME"] = False
    if args.no_save:
        CONFIG["SAVE_TRANSCRIPTS"] = False

    if args.threshold is not None:
        CONFIG["SILENCE_THRESHOLD"] = args.threshold
    if args.model:
        CONFIG["MODEL_SIZE"] = args.model
    if args.max_seconds is not None:
        CONFIG["MAX_RECORD_SECONDS"] = args.max_seconds

    # A numeric arg (incl. negatives) is a device index; anything else is a name
    # substring. isdigit() would mis-handle "-1" and choke on exotic Unicode digits.
    dev_spec = args.device
    if dev_spec is not None:
        try:
            dev_spec = int(dev_spec)
        except (TypeError, ValueError):
            pass

    if args.doctor:
        cmd_doctor()      # exits with its own status code

    if args.list:
        cmd_list()
        sys.exit(0)

    if args.test:
        spec = dev_spec if dev_spec is not None else CONFIG["DEVICE"]
        try:
            idx, name, ha = resolve_device(spec)
        except SystemExit as exc:
            print(exc)
            sys.exit(1)
        except Exception as exc:
            print(f"❌ Bad device '{args.device}': {exc}")
            print("   Run:  python skald.py --list")
            sys.exit(1)
        run_meter(idx)
        sys.exit(0)

    # LIVE mode is the default (Rath, 2026-07-02): the desktop shortcut runs the bare
    # script, and the tap-toggle + overlay is the experience he expects "the app" to be.
    if args.classic:
        main(dev_spec)
    else:
        run_stream_mode(dev_spec)
