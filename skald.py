"""Skald - push-to-talk speech-to-text with auto-paste.

DEFAULT: LIVE mode. TAP the hotkey to start, a floating overlay shows the level
and words as you pause, tap again to stop and paste instantly.
Classic hold-to-talk (no window) is still available:  python skald.py --classic
Use Settings in the overlay, or skald.toml, to change your writing and controls.
"""

import base64
import io
import json
import math
import os
import queue
import re
import sys
import time
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
from pathlib import Path
from copy import deepcopy
from skald_writing import Settings, MODES, data_directory
from skald_history import History
from skald_session import WritingSession, Run
from skald_desktop import (foreground_window, clipboard_is_plain_text,
                           clipboard_sequence, insert_unicode)

SETTINGS = Settings()
SETTINGS_PATH = data_directory() / "skald.toml"
HISTORY = History(data_directory() / "history.sqlite3")


def hotkey_from_name(name):
    name = name.strip()
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)
    try:
        return keyboard.Key[name.lower()]
    except KeyError:
        raise ValueError("Use a single character or a key name such as ctrl_r or f8") from None


def apply_settings(settings):
    global SETTINGS
    key = hotkey_from_name(settings.hotkey)
    settings.validate()
    SETTINGS = settings
    device = settings.device or None
    if device and device.lstrip("-").isdigit():
        device = int(device)
    CONFIG.update(HOTKEY=key, DEVICE=device,
                  MODEL_SIZE=settings.model, LIVE_MODEL=settings.model,
                  LANGUAGE=settings.language.lower() or None, CHIME=settings.chime,
                  ENABLE_COMMANDS=settings.enable_app_commands,
                  SAVE_TRANSCRIPTS=settings.save_history,
                  TRANSCRIPT_RETENTION_DAYS=settings.retention_days)

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
    "OVERLAY_WIDTH": 460,
    "OVERLAY_HEIGHT": 320,
    # Overlay waveform accent. Toggle "ice" (cool blue voice-energy, the default) or
    # "amber" (molten). Runic text, dividers, and the close control stay gold either way.
    "THEME_ACCENT": "gold",

    # Incremental SQLite recovery (v1.4); Settings controls new writes. Older daily
    # Markdown archives stay at TRANSCRIPT_DIR and are not changed or migrated.
    "SAVE_TRANSCRIPTS": True,
    "TRANSCRIPT_DIR": os.path.join(os.path.expanduser("~"), "Documents",
                                   "Skald Transcripts"),
    # Retention applies to unpinned recovery entries. 0 = keep forever.
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
    "ENABLE_COMMANDS": False,
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
_classic_target = None
_classic_busy = threading.Event()
_classic_closing = threading.Event()
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
        if CONFIG["LANGUAGE"] != "en":
            pick = pick.removesuffix(".en")
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
    mic, the Whisper model cache dir, and non-mutating clipboard access. Exits 0 only if
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

    # 5. Read without replacing rich text, images, copied files, or newer content.
    try:
        import pyperclip as _pc
        check("clipboard text read (no clipboard changes)", isinstance(_pc.paste(), str))
    except Exception as exc:
        check("clipboard text read (no clipboard changes)", False, str(exc)[:60])

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
    body = json.dumps({"audio_data": _wav_b64(audio),
                       "langcode": CONFIG["LANGUAGE"] or ""}).encode()
    req = urllib.request.Request(CONFIG["ASR_SERVER_URL"] + "/api/extra/transcribe",
                                 data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return (json.load(r).get("text") or "").strip()


def _get_fallback_model():
    global _fallback_model
    with _fallback_lock:
        if _fallback_model is None:
            size = pick_cpu_model(os.cpu_count() or 4)
            if CONFIG["LANGUAGE"] != "en":
                size = size.removesuffix(".en")
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


def _paste_text(text, target=None, allowed=lambda: True):
    """Deliver into the window captured at record start. Preserve the clipboard.

    Rich clipboard contents use Unicode input rather than blocking dictation.
    Restoration is conditional so a newer user clipboard change always wins.
    """
    if not text:
        return False
    if not allowed():
        return False
    if target is not None and foreground_window() != target:
        return False
    preserve = SETTINGS.preserve_clipboard
    if preserve and not clipboard_is_plain_text():
        return insert_unicode(text, target=target, allowed=allowed)
    previous = pyperclip.paste() if preserve else None
    pyperclip.copy(text)
    sequence = clipboard_sequence()
    try:
        time.sleep(0.15)
        if not allowed():
            return False
        if target is not None and foreground_window() != target:
            return False
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.6)  # allow the receiving app to consume the clipboard
        return True
    finally:
        if (preserve and clipboard_sequence() == sequence
                and pyperclip.paste() == text):
            pyperclip.copy(previous)


def _send_text(text, target=None, allowed=lambda: True):
    """Paste text then press Enter (e.g. send a Discord/chat message)."""
    text = re.sub(r"^message\s+", "", text, flags=re.IGNORECASE).strip()
    if not text:
        print("❓ Send what?")
        return
    print(f"📤 Sending: {text}")
    if not _paste_text(text, target=target, allowed=allowed):
        print("Message held; choose its destination and copy it from Review.")
        return
    time.sleep(0.1)
    if allowed() and (target is None or foreground_window() == target):
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
    print("Unknown app. Add a trusted entry to APP_MAP to enable it.")


def _print_help():
    print("🗣️  Voice commands (say the prefix first, e.g. 'skald open chrome'):")
    print("   • open <app or site>   'open chrome' · 'open github.com'")
    print("   • search <query>       'search kentucky llc fees'")
    print("   • send <text>          paste + press Enter (send in a chat window)")
    print("   • type <text>          force-paste text")
    print("   • cancel / never mind  discard")
    print("   Anything WITHOUT the prefix is dictated (pasted) as normal.")


def route_command(full_text, target=None, allowed=lambda: True):
    """Parse and execute a command. Returns True if handled (don't paste),
    False if the transcript should fall through to normal dictation."""
    parsed = parse_command(full_text)
    if parsed is None:
        return False
    if not allowed() or (target is not None and foreground_window() != target):
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
        _paste_text(arg, target=target, allowed=allowed)
    elif verb == "send":
        _send_text(arg, target=target, allowed=allowed)
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

        print(f"Transcribed in {elapsed:.2f}s.")
        session = LiveSession(model, model, 16000)
        session.paste = lambda text, **kwargs: _paste_text(
            text, target=kwargs.get("target"),
            allowed=lambda: not _classic_closing.is_set())
        run = Run(deepcopy(SETTINGS), _classic_target)
        run.draft.accept(full_text)
        run.cancelled = run.draft.cancelled
        # Classic has no review window. Respect review-only and console shutdown.
        if run.settings.review_before_paste or _classic_closing.is_set():
            run.deliver = False
        session.closing = _classic_closing.is_set()
        session._finalize(run, [])
        print(run.error or f"Draft {run.status}. Open Skald History to recover it.")
        if run.output and run.status != "pasted" and not run.settings.save_history:
            # Classic has no review UI. Keep an unsaved held draft recoverable in
            # its console rather than overwriting a clipboard we promised to keep.
            print("Unsaved draft held for manual copy:\n" + run.output)

    except Exception as exc:
        print(f"❌ Transcription error: {exc}")


def on_press(key, model):
    """Begin buffering audio on hotkey press (stream is already running)."""
    global _recording, _buffer, _record_start, _classic_target

    if key != CONFIG["HOTKEY"] or _recording:
        return
    if _classic_busy.is_set() or _classic_closing.is_set():
        print("Finishing the previous dictation; wait before recording again.")
        return

    with _buffer_lock:
        _buffer = []
    _classic_target = foreground_window()
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
    _classic_busy.set()

    # Run transcription in a background thread so the hotkey listener
    # stays responsive during the (potentially slow) inference.
    def _transcribe():
        global _silent_streak
        try:
            audio, rms, secs = prepare_audio(frames_snapshot)
            if audio is None or rms < CONFIG["SILENCE_THRESHOLD"]:
                _silent_streak += 1
                _report_silence(rms, secs)
                return
            _silent_streak = 0
            audio = apply_auto_gain(audio, rms)
            transcribe_and_paste(model, audio)
        finally:
            _classic_busy.clear()

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
        if CONFIG.get("ASR_BACKEND") == "server":
            sys.exit("The GPU server is unavailable. Start it or choose backend auto.")
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
        _classic_closing.set()
        if _recording:
            on_release(CONFIG["HOTKEY"], model)
        try:
            _stream.stop()
            _stream.close()
        except Exception:
            pass
        if _classic_busy.is_set():
            print("Saving the pending dictation before closing...")
            # Event.wait() on the busy flag returns immediately while set, so poll
            # the flag here. The console stays alive until recovery is written.
            while _classic_busy.is_set():
                time.sleep(0.1)
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


class LiveSession(WritingSession):
    def __init__(self, live_model, final_model, capture_sr, settings=None, history=None):
        super().__init__(live_model, final_model, capture_sr,
                         settings=settings or SETTINGS, config=CONFIG,
                         history=history or HISTORY,
                         transcribe=lambda model, audio: transcribe_any(model, audio),
                         resample=resample_to_16k, gain=apply_auto_gain,
                         refine_available=lambda: gpu_asr_active(),
                         paste=lambda text, **kwargs: _paste_text(text, **kwargs),
                         chime=_chime, route_command=route_command)


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

    MIN_W, MIN_H = 440, 300

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

        self._dialogs = set()

        def button(parent, title, action):
            seal = {"Record": "ᚱ", "Pause": "ᛁ", "Cancel": "ᛉ", "Review": "ᛗ",
                    "History": "ᛟ", "Settings": "ᚷ"}.get(title, "ᛊ")
            edge = tk.Frame(parent, bg=self.BORDER, padx=1, pady=1)
            edge.pack(side="left", fill="x", expand=True, padx=(0, 5))
            control = tk.Button(edge, text=f"{seal}  {title.upper()}", command=action,
                                takefocus=False, cursor="hand2", bg="#2D2B28",
                                fg=self.GOLD, activebackground="#51432A",
                                activeforeground=self.GOLD_HI, relief="flat", bd=0,
                                highlightthickness=0, padx=7, pady=6,
                                font=("Palatino Linotype", 9, "bold"))
            control.pack(fill="x")
            control.bind("<Enter>", lambda e: (edge.configure(bg=self.GOLD_HI),
                                               control.configure(bg="#3C3427", fg=self.GOLD_HI)))
            control.bind("<Leave>", lambda e: (edge.configure(bg=self.BORDER),
                                               control.configure(bg="#2D2B28", fg=self.GOLD)))
            return control

        actions = tk.Frame(frame, bg=self.BG)
        actions.pack(fill="x", padx=12, pady=(4, 6))
        self.record_btn = button(actions, "Record", self.sess.toggle)
        self.pause_btn = button(actions, "Pause", self.sess.pause)
        button(actions, "Cancel", self.sess.cancel)
        button(actions, "Review", self._review)

        options = tk.Frame(frame, bg=self.BG)
        options.pack(fill="x", padx=12, pady=(0, 6))
        self.mode_btn = button(options, self.sess.settings.mode.title(), self._cycle_mode)
        button(options, "History", self._history)
        button(options, "Settings", self._settings)
        self.saving_lbl = tk.Label(options, bg=self.BG, fg=self.SUB,
                                   font=("Segoe UI", 9))
        self.saving_lbl.pack(side="right")
        self._refresh_controls()

        # ---- status row (listening state + live timer) ----
        status_row = tk.Frame(frame, bg=self.BG)
        status_row.pack(fill="x", padx=12, pady=(0, 2))
        self.status = tk.Label(status_row, text="Tap to talk", bg=self.BG, fg=self.ACC,
                               font=("Segoe UI", 10, "bold"), anchor="w",
                               justify="left", wraplength=410, height=2)
        self.status.pack(side="left")

        # ---- live waveform meter (the centerpiece) ----
        self.canvas = tk.Canvas(frame, height=64, bg=self.BG, highlightthickness=0)
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

    def _refresh_controls(self):
        self.mode_btn.configure(text=f"ᛊ  {self.sess.settings.mode.upper()}")
        self.saving_lbl.configure(text="Saving on" if self.sess.settings.save_history else "Saving off")

    def _track_dialog(self, win):
        self._dialogs.add(win)
        self.sess.dialog_open = True

        def closed(event):
            if event.widget is win:
                self._dialogs.discard(win)
                self.sess.dialog_open = bool(self._dialogs)
        win.bind("<Destroy>", closed, add="+")
        return win

    def _save_settings(self, updated):
        hotkey_from_name(updated.hotkey)
        updated.save(SETTINGS_PATH)
        apply_settings(updated)
        self.sess.settings = updated
        self._refresh_controls()
        self.sess.status("Settings saved; restart for microphone, model, or language changes")

    def _cycle_mode(self):
        if self.sess.busy:
            self.sess.status("Finish this dictation before changing modes", "listening")
            return
        from dataclasses import replace
        mode = MODES[(MODES.index(self.sess.settings.mode) + 1) % len(MODES)]
        try:
            self._save_settings(replace(self.sess.settings, mode=mode))
        except (ValueError, OSError) as exc:
            self.sess.status(f"Could not save settings: {exc}", "error")

    def _settings(self):
        if self.sess.busy:
            self.sess.status("Finish this dictation before opening Settings", "listening")
            return
        from skald_ui import show_settings
        try:
            devices = sorted({device["name"] for device in sd.query_devices()
                              if device["max_input_channels"] > 0})
        except Exception:
            devices = ()
        self.sess.dialog_open = True
        try:
            self._track_dialog(show_settings(self.root, self.sess.settings,
                                            self._save_settings, devices))
        finally:
            self.sess.dialog_open = bool(self._dialogs)

    def _review(self, run=None):
        if self.sess.busy and run is None:
            self.sess.status("Finish this dictation before reviewing", "listening")
            return
        run = run or self.sess.last_run
        if run is None or not run.draft.original:
            self.sess.status("No draft yet; record something first")
            return
        from skald_ui import show_review

        def save(text):
            run.output = text
            self.last_text = text
            if not self.sess._save(run, "reviewed"):
                raise OSError("History unavailable; the draft is still in memory")
        self._track_dialog(show_review(self.root, "\n".join(run.draft.original),
                                       run.output, run.settings.mode, pyperclip.copy, save,
                                       learn=self._learn_spelling))

    def _learn_spelling(self, heard, preferred):
        from dataclasses import replace
        vocabulary = {k: v for k, v in self.sess.settings.vocabulary.items()
                      if k.casefold() != heard.casefold()}
        vocabulary[heard] = preferred
        self._save_settings(replace(self.sess.settings, vocabulary=vocabulary))

    def _history(self):
        if self.sess.busy:
            self.sess.status("Finish this dictation before opening History", "listening")
            return
        from skald_ui import show_history, show_review

        def review(row):
            def save(text):
                HISTORY.save(row["id"], row["created_at"], row["original"], text,
                             row["mode"], "reviewed")
            self._track_dialog(show_review(self.root, row["original"], row["output"],
                                           row["mode"], pyperclip.copy, save,
                                           learn=self._learn_spelling))
        self._track_dialog(show_history(self.root, HISTORY, review))

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
            self.status.configure(wraplength=max(200, self.root.winfo_width() - 24))
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
        return str(SETTINGS_PATH.parent / "overlay_state.json")

    @staticmethod
    def _load_state():
        import json
        try:
            path = _Overlay._state_path()
            if not os.path.exists(path):
                path = os.path.join(os.path.dirname(__file__), "overlay_state.json")
            with open(path, encoding="utf-8") as f:
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
                os.makedirs(os.path.dirname(self._state_path()), exist_ok=True)
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
        if self.on_quit:
            self.on_quit()
        else:
            self._alive = False
            try:
                self.root.destroy()
            except Exception:
                pass

    def _tick(self):
        import tkinter as tk
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
                    elif kind == "review" and not self.sess.closing:
                        self._review(payload)
            except queue.Empty:
                pass
            # While recording, show a live elapsed timer in the status line (highlight as
            # it nears the MAX_RECORD_SECONDS trim cap). Runs after the queue drain so it
            # wins over the static "Listening" message set at tap-on.
            self.record_btn.configure(text="ᛏ  FINISH" if self.sess.recording else "ᚱ  RECORD")
            self.pause_btn.configure(text="ᚹ  RESUME" if self.sess.paused else "ᛁ  PAUSE")
            if self.sess.recording and not self.sess.paused and self.sess.record_start:
                el = int(time.perf_counter() - self.sess.record_start)
                m, s = divmod(el, 60)
                near_cap = el >= CONFIG["MAX_RECORD_SECONDS"] - 30
                self.status.config(text=f"● Listening  {m}:{s:02d}",
                                   fg=self.WARN if near_cap else self.ACC)
        except tk.TclError:
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
        from skald_wave import RibbonFrames
        hd = Path(__file__).resolve().parent / "assets" / "wave-hd"
        if (hd / "manifest.json").exists():
            try:
                return {"hd": RibbonFrames(hd)}
            except (OSError, ValueError, KeyError) as exc:
                print(f"Blue ribbon artwork unavailable; using original sprites: {exc}")
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
        """Blue knotwork ribbons glide right; a separate gold string follows speech.

        Pre-sized antialiased artwork keeps the filaments sharp without runtime
        imaging dependencies. All drawing stays on the Tk thread.
        """
        c = self.canvas
        vals = list(self.sess.rms_history)
        raw = min(max((max(vals[-6:]) if vals else 0.0) / 0.025, 0.0), 1.0)
        if not self.sess.recording or self.sess.paused:
            raw = 0.0
        # Smooth so the wave swells and settles instead of twitching.
        self._level = 0.82 * getattr(self, "_level", 0.0) + 0.18 * raw
        lv = self._level
        w = c.winfo_width() or (CONFIG["OVERLAY_WIDTH"] - 24)
        ch = c.winfo_height() or 40
        mid = ch // 2
        sp = self._sprites
        c.delete("all")
        now = time.perf_counter()
        dt = min(0.05, max(0.0, now - getattr(self, "_rune_clock", now - 0.033)))
        self._rune_clock = now
        if "hd" in sp:
            source_x = sp["hd"].draw(c, w, ch, lv, dt, self.BG)
        else:
            source_x = self._draw_legacy_blue_stream(w, ch, mid, lv)
        self._draw_rune_stream(w, mid, source_x, dt)

    def _draw_legacy_blue_stream(self, w, ch, mid, lv):
        """Keep the original bank as a fallback when HD artwork is absent."""
        c, sp = self.canvas, self._sprites
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
        return float(sp["star_w"] * sy + 4)

    def _draw_rune_stream(self, w, mid, source_x, dt):
        import random
        c = self.canvas
        # One evenly spaced string, emitted directly from microphone activity.
        # Advance every glyph at the same speed; louder syllables must not pile
        # multiple runes onto the source or make older glyphs bunch together.
        runes = getattr(self, "_runes", None)
        if runes is None:
            runes = self._runes = []
        spacing = 28.0
        for r in runes:
            r["x"] += 125.0 * dt
        speaking = (self.sess.recording and not self.sess.paused
                    and self.sess.level >= CONFIG["STREAM_SPEECH_RMS"])
        if speaking and (not runes or runes[-1]["x"] - source_x >= spacing):
            runes.append({"x": source_x, "g": random.choice(self.RUNES),
                          "age": 0.0, "size": 17})
        keep = []
        for r in runes:
            r["age"] += dt
            r["y"] = mid + 3 * math.sin((r["x"] - source_x) / 55.0)
            if r["x"] < w:
                edge = min(1.0, max(0.0, (w - r["x"]) / 70.0))
                col = self.GOLD_HI if edge > 0.6 else (self.GOLD if edge > 0.25 else "#9A7D33")
                c.create_text(r["x"] + 1, r["y"] + 1, text=r["g"], fill="#5E481F",
                              font=("Palatino Linotype", r.get("size", 12) + 2, "bold"),
                              anchor="w")
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
        if CONFIG.get("ASR_BACKEND") == "server":
            sys.exit("The GPU server is unavailable. Start it or choose backend auto.")
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
        if sess.closing:
            return
        overlay._save_state()
        sess.shutdown()                  # flush pending speech; never paste on close
        try:
            listener.stop()
        except Exception:
            pass
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        def finish_close():
            if sess.busy:
                sess.status("Saving pending speech before closing", "finalizing")
                root.after(100, finish_close)
                return
            overlay._alive = False
            root.destroy()
        finish_close()

    overlay.on_quit = on_quit
    root.protocol("WM_DELETE_WINDOW", on_quit)
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
        if overlay._alive:
            root.mainloop()             # allow the pending save/close callback to run
    print("\n👋 Live Mode stopped.")
    sys.exit(0)


def run_demo():
    """Visual QA with synthetic text, isolated settings, no mic and no model."""
    import tempfile
    import tkinter as tk
    from dataclasses import replace
    global SETTINGS_PATH, HISTORY
    qa = Path(__file__).resolve().parent / "qa"
    qa.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="demo-", dir=qa) as directory:
        SETTINGS_PATH = Path(directory) / "skald.toml"
        settings = Settings(vocabulary={"skull": "Skald"},
                            snippets={"follow up": "Thanks for your time.\nHere are the next steps."})
        settings.save(SETTINGS_PATH)
        apply_settings(settings)
        HISTORY = History(Path(directory) / "history.sqlite3")
        sess = LiveSession(None, None, 16000)
        sess.chime = lambda kind: None
        sess.transcribe = lambda model, audio: "Example dictation."
        sess.refine_available = lambda: False
        sess.paste = lambda text, **kwargs: False
        for mode, text in (("exact", "A thought worth keeping. Your original words stay available."),
                           ("clean", "Um, confirm the delivery date is Thursday, not Tuesday."),
                           ("bullets", "Review the draft.\nShare the next steps.\nKeep the original.")):
            run = Run(replace(settings, mode=mode), None)
            run.draft.accept(text)
            run.output = run.draft.output
            sess._save(run, "ready")
            sess.last_run = run
        root = tk.Tk()
        overlay = _Overlay(root, sess, "Speak freely. Shape your draft. Keep your words.")
        # Use a normal taskbar window for the demo so desktop capture tools can
        # inspect it. The real dictation overlay keeps its no-activation behavior.
        def show_demo_window():
            root.overrideredirect(False)
            overlay._apply_exstyle(noactivate=False, toolwindow=False)
            root.deiconify()
        root.after(150, show_demo_window)
        sess.ui_q.put(("final", "Confirm the delivery date is Thursday, not Tuesday.\n\nYour draft is ready for review."))
        sess.status("Demo: no microphone, model download, or automatic paste")

        def close():
            sess.shutdown()
            if sess.busy:
                root.after(50, close)
                return
            overlay._alive = False
            root.destroy()
        overlay.on_quit = close
        root.protocol("WM_DELETE_WINDOW", close)
        root.mainloop()


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
    ap.add_argument("--config", help="path to your skald.toml settings file")
    ap.add_argument("--mode", choices=MODES, help="writing mode for this run")
    ap.add_argument("--review", action="store_true", help="review each draft before pasting")
    ap.add_argument("--demo", action="store_true", help="preview with example text; no mic or model")
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
                    help="classic clip/live accuracy-pass audio limit (default 600); "
                         "live streaming continues beyond it")
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
                    help="don't save dictation history to disk this run")
    args = ap.parse_args()

    if args.demo:
        run_demo()
        sys.exit(0)
    if args.max_seconds is not None and args.max_seconds <= 0:
        ap.error("--max-seconds must be positive")
    if args.threshold is not None and (not math.isfinite(args.threshold)
                                       or not 0 < args.threshold <= 1):
        ap.error("--threshold must be greater than 0 and at most 1")
    try:
        if args.config:
            SETTINGS_PATH = Path(args.config).expanduser().resolve()
        settings = Settings.load(SETTINGS_PATH)
        hotkey_from_name(settings.hotkey)
        if not SETTINGS_PATH.exists():
            settings.save(SETTINGS_PATH)
        if args.mode:
            settings.mode = args.mode
        if args.review:
            settings.review_before_paste = True
        if args.no_save:
            settings.save_history = False
        if args.no_chime:
            settings.chime = False
        if args.chime and not args.no_chime:
            settings.chime = True
        if args.model:
            settings.model = args.model
        if args.device:
            settings.device = args.device
        apply_settings(settings)
        HISTORY = History(SETTINGS_PATH.parent / "history.sqlite3")
        if settings.save_history:
            HISTORY.prune(settings.retention_days)
    except (ValueError, TypeError, OSError) as exc:
        ap.error(f"Settings: {exc}")

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
        CONFIG["STREAM_SPEECH_RMS"] = args.threshold
    if args.model:
        CONFIG["MODEL_SIZE"] = args.model
        CONFIG["LIVE_MODEL"] = args.model
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
