"""Recording lifecycle, local draft editing, and recoverable delivery."""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import collections
import os
import queue
import threading
import time
from uuid import uuid4

import numpy as np

from skald_desktop import foreground_process, foreground_window
from skald_writing import Draft


@dataclass
class Run:
    settings: object
    target: object
    ident: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    chunks: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=300))
    cancelled: bool = False
    deliver: bool = True
    overflow: bool = False
    error: str = ""
    output: str = ""
    status: str = "draft"

    def __post_init__(self):
        self.draft = Draft(self.settings)


class WritingSession:
    WAVE_BARS = 64

    def __init__(self, live_model, final_model, capture_sr, *, settings, config,
                 history, transcribe, resample, gain, refine_available, paste,
                 chime, route_command):
        self.live_model, self.final_model = live_model, final_model
        self.capture_sr, self.settings, self.config = capture_sr, settings, config
        self.history = history
        self.transcribe, self.resample, self.gain = transcribe, resample, gain
        self.refine_available, self.paste = refine_available, paste
        self.chime, self.route_command = chime, route_command
        self.ui_q = queue.Queue()
        self.rms_history = collections.deque([0.0] * self.WAVE_BARS, maxlen=self.WAVE_BARS)
        self.level = 0.0
        self.recording = self.paused = self.key_armed = self.closing = False
        self.dialog_open = False
        self.record_start = 0.0
        self.active_q = self.chunk_q = self.worker = self.run = self.last_run = None
        self.engine_lock = threading.Lock()
        self.state_lock = threading.RLock()

    @property
    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def status(self, text, state="idle"):
        self.ui_q.put(("status", (state, text)))

    def audio_cb(self, indata, frames, time_info, status):
        mono = indata.astype(np.float32).flatten()
        self.level = float(np.sqrt(np.mean(mono ** 2))) if mono.size else 0.0
        for part in np.array_split(mono, 4):
            if part.size:
                self.rms_history.append(float(np.sqrt(np.mean(part ** 2))))
        # The lock orders the final callback before the stop sentinel. A tail chunk
        # can neither arrive after finalize nor leak into the next recording.
        with self.state_lock:
            if self.active_q is not None:
                if status:
                    self.run.error = "Microphone reported dropped audio; review the draft"
                try:
                    self.active_q.put_nowait(indata.copy())
                except queue.Full:
                    self.run.overflow = True

    def on_press(self, key):
        if self.dialog_open:
            return
        if key != self.config["HOTKEY"] or self.key_armed:
            return
        self.key_armed = True
        self.toggle()

    def on_release(self, key):
        if key == self.config["HOTKEY"]:
            self.key_armed = False

    def toggle(self):
        with self.state_lock:
            self._stop() if self.recording else self._start()

    def _start(self):
        with self.state_lock:
            if self.closing or self.dialog_open:
                return
            if self.busy:
                self.status("Finishing your previous dictation", "finalizing")
                return
            settings = deepcopy(self.settings)
            process = foreground_process()
            settings.mode = next((v for k, v in settings.profiles.items()
                                  if k.casefold() == process.casefold()), settings.mode)
            self.run = run = Run(settings, foreground_window())
            self.chunk_q = run.chunks
            self.record_start = time.perf_counter()
            self.paused = False
            self.recording = True
            self.active_q = run.chunks
            self.ui_q.put(("reset", None))
            self.status("Listening", "listening")
            self.worker = threading.Thread(target=self._run_worker, args=(run,), daemon=True)
            self.worker.start()
            self.chime("start")

    def _stop(self):
        with self.state_lock:
            if not self.recording:
                return
            self.active_q = None
            self.recording = self.paused = False
            self.status("Finishing your draft", "finalizing")
            # A full queue will be consumed by the worker before it needs a sentinel.
            # Use an event flag via recording state, rather than blocking audio/UI.
            try:
                self.chunk_q.put_nowait(None)
            except queue.Full:
                pass
            self.chime("stop")

    def pause(self):
        with self.state_lock:
            if not self.recording:
                return
            self.paused = not self.paused
            self.active_q = None if self.paused else self.chunk_q
            self.status("Paused; click Resume when ready" if self.paused else "Listening",
                        "paused" if self.paused else "listening")

    def cancel(self):
        with self.state_lock:
            if self.run is not None and self.busy:
                self.run.cancelled = True
                self.run.deliver = False
                self._stop()
                self.status("Cancelling", "finalizing")

    def shutdown(self):
        with self.state_lock:
            self.closing = True
            if self.run is not None:
                self.run.deliver = False
            self._stop()

    def _save(self, run, status="draft"):
        run.status = status
        if not run.settings.save_history or not self.config.get("SAVE_TRANSCRIPTS", True):
            return True
        if not run.draft.original:
            return True
        try:
            self.history.save(run.ident, run.created_at, "\n".join(run.draft.original),
                              run.draft.output if status == "draft" else run.output,
                              run.settings.mode, status)
            return True
        except Exception:
            run.error = "History could not be saved; copy your draft before closing"
            return False

    def _transcribe(self, model, audio):
        with self.engine_lock:
            return self.transcribe(model, audio)

    def _emit_phrase(self, run, chunks):
        if run.cancelled:
            return
        audio = np.concatenate(chunks, axis=0).flatten().astype(np.float32)
        audio = self.gain(self.resample(audio, self.capture_sr))
        try:
            text = self._transcribe(self.live_model, audio)
        except Exception:
            run.error = "Part of this recording could not be transcribed; review captured text"
            return
        if run.cancelled:
            return
        if text:
            message = run.draft.accept(text)
            self._save(run)
            self.ui_q.put(("partial", run.draft.output))
            if message:
                self.status(message, "listening")
            if run.draft.cancelled:
                run.cancelled = True
                run.deliver = False
                self._stop()

    def _run_worker(self, run):
        config = self.config
        full, full_n = [], 0
        max_full = int(config["MAX_RECORD_SECONDS"] * self.capture_sr)
        phrase, sil_ms, sp_ms = [], 0.0, 0.0
        try:
            while not run.cancelled:
                try:
                    chunk = run.chunks.get(timeout=0.25)
                except queue.Empty:
                    if not self.recording:
                        break
                    continue
                if chunk is None:
                    break
                if full_n + len(chunk) <= max_full:
                    full.append(chunk)
                full_n += len(chunk)
                phrase.append(chunk)
                dur_ms = len(chunk) / self.capture_sr * 1000.0
                rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
                if rms >= config["STREAM_SPEECH_RMS"]:
                    sp_ms += dur_ms
                    sil_ms = 0.0
                else:
                    sil_ms += dur_ms
                if ((sp_ms >= config["PHRASE_MIN_MS"] and sil_ms >= config["PHRASE_SILENCE_MS"])
                        or sum(len(c) for c in phrase) / self.capture_sr >= config["PHRASE_MAX_SECONDS"]):
                    if sp_ms >= config["PHRASE_MIN_MS"]:
                        self._emit_phrase(run, phrase)
                    phrase, sp_ms = [], 0.0
                if run.overflow:
                    run.error = "Audio processing fell behind; recording stopped to protect your draft"
                    self._stop()
                silence_limit = run.settings.auto_stop_silence
                if silence_limit and sil_ms >= silence_limit * 1000:
                    self._stop()
            if phrase and sp_ms >= config["PHRASE_MIN_MS"] and not run.cancelled:
                self._emit_phrase(run, phrase)
            self._finalize(run, full, full_n <= max_full)
        except Exception:
            run.error = "Dictation stopped unexpectedly; review the recovered draft"
            self._stop()
            run.output = run.draft.output
            self._save(run, "error")
            self.last_run = run
            self.ui_q.put(("final", run.output))
            self.status(run.error, "error")

    def _finalize(self, run, full_chunks, complete=True):
        # Commands edit the streamed draft. A full ASR pass would resurrect undone
        # words and erase paragraph breaks, so refine only an unedited complete run.
        if (not run.cancelled and not run.draft.edited and run.draft.original
                and not run.error and complete and full_chunks
                and (self.config["FINAL_MODEL"] or self.refine_available())):
            try:
                audio = np.concatenate(full_chunks, axis=0).flatten().astype(np.float32)
                audio = self.gain(self.resample(audio, self.capture_sr))
                refined = self._transcribe(self.final_model, audio)
                if refined and not run.cancelled:
                    from skald_writing import apply_vocabulary
                    run.draft.text = apply_vocabulary(refined, run.settings.vocabulary)
            except Exception:
                run.error = "Final accuracy pass failed; your streamed draft is preserved"
        run.output = "" if run.cancelled else run.draft.output
        self.last_run = run
        status = "cancelled" if run.cancelled else "ready"
        self._save(run, status)       # durable final draft BEFORE any external paste
        if run.output and run.deliver and not self.closing and not run.error:
            allowed = lambda: not run.cancelled and not self.closing
            if run.settings.review_before_paste:
                status = "review"
                self.ui_q.put(("review", run))
            elif not run.target:
                status = "held"
            elif (run.settings.enable_app_commands and
                  self.route_command(run.output, target=run.target, allowed=allowed)):
                status = "command"
            else:
                try:
                    status = "pasted" if self.paste(run.output, target=run.target,
                                                    allowed=allowed) else "held"
                    if status == "held" and run.cancelled:
                        status = "cancelled"
                        run.output = ""
                except Exception:
                    run.error = "Paste failed; your draft is available in Review"
                    status = "held"
        self._save(run, status)
        self.ui_q.put(("final", run.output))
        messages = {"pasted": "Pasted; tap to talk", "review": "Draft ready for review",
                    "held": "Draft ready; click Review or Copy to choose its destination",
                    "ready": "Draft saved", "cancelled": "Cancelled; nothing pasted",
                    "command": "Command completed"}
        self.status(run.error or (messages[status] if run.output or run.cancelled else "Nothing heard; tap to talk"),
                    "error" if run.error else "idle")
