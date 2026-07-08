"""
CLIVoiceMixin — extracted from cli.py.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class CLIVoiceMixin:
    """CLIVoiceMixin — mixed into HermesCLI."""

    def _voice_record_key_label(self) -> str:
        """Return the configured voice push-to-talk key formatted for UI.

        Shared helper so every voice-facing status line / placeholder /
        recording hint advertises the SAME label as the registered
        prompt_toolkit binding.

        Cached at startup (see ``set_voice_record_key_cache``) rather
        than re-read per render. Two reasons (Copilot round-13 on
        #19835):

        * The prompt_toolkit binding is registered once at session
          start via ``@kb.add(_voice_key)``; re-reading config per
          render meant the status bar could advertise a new shortcut
          after a config edit while the actual binding was still the
          startup chord — exactly the display/binding drift this PR
          is trying to eliminate.
        * The label is on the hot render path (status bar + composer
          placeholder invalidated every 150ms during recording), so
          reading config on every call added avoidable UI overhead.
        """
        return getattr(self, "_voice_record_key_display_cache", None) or "Ctrl+B"

    def _voice_start_recording(self):
        """Start capturing audio from the microphone."""
        if getattr(self, '_should_exit', False):
            return
        from tools.voice_mode import create_audio_recorder, check_voice_requirements

        reqs = check_voice_requirements()
        if not reqs["audio_available"]:
            if _is_termux_environment():
                details = reqs.get("details", "")
                if "Termux:API Android app is not installed" in details:
                    raise RuntimeError(
                        "Termux:API command package detected, but the Android app is missing.\n"
                        "Install/update the Termux:API Android app, then retry /voice on.\n"
                        "Fallback: pkg install python-numpy portaudio && python -m pip install sounddevice"
                    )
                raise RuntimeError(
                    "Voice mode requires either Termux:API microphone access or Python audio libraries.\n"
                    "Option 1: pkg install termux-api and install the Termux:API Android app\n"
                    "Option 2: pkg install python-numpy portaudio && python -m pip install sounddevice"
                )
            raise RuntimeError(
                "Voice mode requires sounddevice and numpy.\n"
                f"Install with: {sys.executable} -m pip install sounddevice numpy"
            )
        if not reqs.get("stt_available", reqs.get("stt_key_set")):
            raise RuntimeError(
                "Voice mode requires an STT provider for transcription.\n"
                "Option 1: uv pip install faster-whisper  "
                "(free, local; `pip install faster-whisper` also works if pip is on PATH)\n"
                "Option 2: Set GROQ_API_KEY (free tier)\n"
                "Option 3: Set VOICE_TOOLS_OPENAI_KEY (paid)"
            )

        # Prevent double-start from concurrent threads (atomic check-and-set)
        with self._voice_lock:
            if self._voice_recording:
                return
            self._voice_recording = True

        # Load silence detection params from config. Shape-safe: a
        # hand-edited ``voice: true`` / ``voice: cmd+b`` leaves
        # ``load_config()['voice']`` as a non-dict; coerce to {} so
        # continuous recording falls back to the documented defaults
        # instead of crashing on ``.get()``.
        voice_cfg: dict = {}
        try:
            from hermes_cli.config import load_config
            _cfg = load_config().get("voice")
            voice_cfg = _cfg if isinstance(_cfg, dict) else {}
        except Exception:
            pass

        # Recorder creation can fail (no input device, PortAudio init error).
        # Reset the flag on failure or _voice_recording stays True forever and
        # every future voice start is silently skipped by the guard above.
        if self._voice_recorder is None:
            try:
                self._voice_recorder = create_audio_recorder()
            except Exception:
                with self._voice_lock:
                    self._voice_recording = False
                raise

        # Apply config-driven silence params (numeric-guarded so YAML
        # scalar corruption doesn't break recording start-up).
        #
        # ``bool`` is explicitly excluded from the numeric check — in
        # Python bool is a subclass of int, so a hand-edited
        # ``silence_threshold: true`` would otherwise be forwarded as
        # ``1`` instead of falling back to the 200 default (Copilot
        # round-12 on #19835).
        _threshold = voice_cfg.get("silence_threshold")
        _duration = voice_cfg.get("silence_duration")
        self._voice_recorder._silence_threshold = (
            _threshold if isinstance(_threshold, (int, float)) and not isinstance(_threshold, bool) else 200
        )
        self._voice_recorder._silence_duration = (
            _duration if isinstance(_duration, (int, float)) and not isinstance(_duration, bool) else 3.0
        )

        def _on_silence():
            """Called by AudioRecorder when silence is detected after speech."""
            with self._voice_lock:
                if not self._voice_recording:
                    return
            _cprint(f"\n{_DIM}Silence detected, auto-stopping...{_RST}")
            if hasattr(self, '_app') and self._app:
                self._app.invalidate()
            self._voice_stop_and_transcribe()

        # Audio cue: single beep BEFORE starting stream (avoid CoreAudio conflict)
        if self._voice_beeps_enabled():
            try:
                from tools.voice_mode import play_beep
                play_beep(frequency=880, count=1)
            except Exception:
                pass

        try:
            self._voice_recorder.start(on_silence_stop=_on_silence)
        except Exception:
            with self._voice_lock:
                self._voice_recording = False
            raise
        _label = self._voice_record_key_label()
        if getattr(self._voice_recorder, "supports_silence_autostop", True):
            _recording_hint = f"auto-stops on silence | {_label} to stop & exit continuous"
        elif _is_termux_environment():
            _recording_hint = f"Termux:API capture | {_label} to stop"
        else:
            _recording_hint = f"{_label} to stop"
        _cprint(f"\n{_ACCENT}● Recording...{_RST} {_DIM}({_recording_hint}){_RST}")

        # Periodically refresh prompt to update audio level indicator
        def _refresh_level():
            while True:
                with self._voice_lock:
                    still_recording = self._voice_recording
                if not still_recording:
                    break
                if hasattr(self, '_app') and self._app:
                    self._app.invalidate()
                time.sleep(0.15)
        threading.Thread(target=_refresh_level, daemon=True).start()

    def _voice_stop_and_transcribe(self):
        """Stop recording, transcribe via STT, and queue the transcript as input."""
        # Atomic guard: only one thread can enter stop-and-transcribe.
        # Set _voice_processing immediately so concurrent Ctrl+B presses
        # don't race into the START path while recorder.stop() holds its lock.
        with self._voice_lock:
            if not self._voice_recording:
                return
            self._voice_recording = False
            self._voice_processing = True

        submitted = False
        transcription_failed = False
        wav_path = None
        try:
            if self._voice_recorder is None:
                return

            wav_path = self._voice_recorder.stop()

            # Audio cue: double beep after stream stopped (no CoreAudio conflict)
            if self._voice_beeps_enabled():
                try:
                    from tools.voice_mode import play_beep
                    play_beep(frequency=660, count=2)
                except Exception:
                    pass

            if wav_path is None:
                _cprint(f"{_DIM}No speech detected.{_RST}")
                return

            # _voice_processing is already True (set atomically above)
            if hasattr(self, '_app') and self._app:
                self._app.invalidate()
            _cprint(f"{_DIM}Transcribing...{_RST}")

            # Get STT model from config
            stt_model = None
            try:
                from hermes_cli.config import load_config
                stt_config = load_config().get("stt", {})
                stt_model = stt_config.get("model")
            except Exception:
                pass

            from tools.voice_mode import transcribe_recording
            result = transcribe_recording(wav_path, model=stt_model)

            if result.get("success") and result.get("transcript", "").strip():
                transcript = result["transcript"].strip()
                self._attached_images.clear()
                if hasattr(self, '_app') and self._app:
                    self._app.invalidate()
                self._pending_input.put(transcript)
                submitted = True
            elif result.get("success"):
                _cprint(f"{_DIM}No speech detected.{_RST}")
            else:
                error = result.get("error", "Unknown error")
                _cprint(f"\n{_DIM}Transcription failed: {error}{_RST}")
                transcription_failed = True

        except Exception as e:
            _cprint(f"\n{_DIM}Voice processing error: {e}{_RST}")
            transcription_failed = wav_path is not None
        finally:
            with self._voice_lock:
                self._voice_processing = False
            if hasattr(self, '_app') and self._app:
                self._app.invalidate()
            # Clean up temp file unless transcription failed. On failure, keep
            # the source recording so long dictation is not lost.
            try:
                if wav_path and os.path.isfile(wav_path):
                    if transcription_failed:
                        _cprint(f"{_DIM}Recording preserved at: {wav_path}{_RST}")
                    else:
                        os.unlink(wav_path)
            except Exception:
                pass

            # Track consecutive no-speech cycles to avoid infinite restart loops.
            if not submitted:
                self._no_speech_count = getattr(self, '_no_speech_count', 0) + 1
                if self._no_speech_count >= 3:
                    self._voice_continuous = False
                    self._no_speech_count = 0
                    _cprint(f"{_DIM}No speech detected 3 times, continuous mode stopped.{_RST}")
                    return
            else:
                self._no_speech_count = 0

            # If no transcript was submitted but continuous mode is active,
            # restart recording so the user can keep talking.
            # (When transcript IS submitted, process_loop handles restart
            # after chat() completes.)
            if self._voice_continuous and not submitted and not self._voice_recording:
                def _restart_recording():
                    try:
                        self._voice_start_recording()
                        if hasattr(self, '_app') and self._app:
                            self._app.invalidate()
                    except Exception as e:
                        _cprint(f"{_DIM}Voice auto-restart failed: {e}{_RST}")
                threading.Thread(target=_restart_recording, daemon=True).start()

    def _voice_speak_response_async(self, text: str) -> None:
        """Schedule TTS and mark it pending before continuous recording can restart."""
        if not self._voice_tts or not text:
            return
        self._voice_tts_done.clear()
        threading.Thread(
            target=self._voice_speak_response,
            args=(text,),
            daemon=True,
        ).start()

    def _voice_speak_response(self, text: str):
        """Speak the agent's response aloud using TTS (runs in background thread)."""
        if not self._voice_tts:
            return
        self._voice_tts_done.clear()
        try:
            from tools.tts_tool import text_to_speech_tool
            from tools.voice_mode import play_audio_file

            # Strip markdown and non-speech content for cleaner TTS
            tts_text = text[:4000] if len(text) > 4000 else text
            tts_text = re.sub(r'```[\s\S]*?```', ' ', tts_text)   # fenced code blocks
            tts_text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', tts_text)  # [text](url) -> text
            tts_text = re.sub(r'https?://\S+', '', tts_text)      # URLs
            tts_text = re.sub(r'\*\*(.+?)\*\*', r'\1', tts_text)  # bold
            tts_text = re.sub(r'\*(.+?)\*', r'\1', tts_text)      # italic
            tts_text = re.sub(r'`(.+?)`', r'\1', tts_text)        # inline code
            tts_text = re.sub(r'^#+\s*', '', tts_text, flags=re.MULTILINE)  # headers
            tts_text = re.sub(r'^\s*[-*]\s+', '', tts_text, flags=re.MULTILINE)  # list items
            tts_text = re.sub(r'---+', '', tts_text)              # horizontal rules
            tts_text = re.sub(r'\n{3,}', '\n\n', tts_text)        # excessive newlines
            tts_text = tts_text.strip()
            if not tts_text:
                return

            # Use MP3 output for CLI playback (afplay doesn't handle OGG well).
            # The TTS tool may auto-convert MP3->OGG, but the original MP3 remains.
            os.makedirs(os.path.join(tempfile.gettempdir(), "hermes_voice"), exist_ok=True)
            mp3_path = os.path.join(
                tempfile.gettempdir(), "hermes_voice",
                f"tts_{time.strftime('%Y%m%d_%H%M%S')}.mp3",
            )

            text_to_speech_tool(text=tts_text, output_path=mp3_path)

            # Play the MP3 directly (the TTS tool returns OGG path but MP3 still exists)
            if os.path.isfile(mp3_path) and os.path.getsize(mp3_path) > 0:
                play_audio_file(mp3_path)
                # Clean up
                try:
                    os.unlink(mp3_path)
                    ogg_path = mp3_path.rsplit(".", 1)[0] + ".ogg"
                    if os.path.isfile(ogg_path):
                        os.unlink(ogg_path)
                except OSError:
                    pass
        except Exception as e:
            logger.warning("Voice TTS playback failed: %s", e)
            _cprint(f"{_DIM}TTS playback failed: {e}{_RST}")
        finally:
            self._voice_tts_done.set()

    def _voice_beeps_enabled(self) -> bool:
        """Return whether CLI voice mode should play record start/stop beeps."""
        try:
            from hermes_cli.config import load_config
            voice_cfg = load_config().get("voice", {})
            if isinstance(voice_cfg, dict):
                return bool(voice_cfg.get("beep_enabled", True))
        except Exception:
            pass
        return True

    def _enable_voice_mode(self):
        """Enable voice mode after checking requirements."""
        if self._voice_mode:
            _cprint(f"{_DIM}Voice mode is already enabled.{_RST}")
            return

        from tools.voice_mode import check_voice_requirements, detect_audio_environment

        # Environment detection -- warn and block in incompatible environments
        env_check = detect_audio_environment()
        if not env_check["available"]:
            _cprint(f"\n{_ACCENT}Voice mode unavailable in this environment:{_RST}")
            for warning in env_check["warnings"]:
                _cprint(f"  {_DIM}{warning}{_RST}")
            return

        reqs = check_voice_requirements()
        if not reqs["available"]:
            _cprint(f"\n{_ACCENT}Voice mode requirements not met:{_RST}")
            for line in reqs["details"].split("\n"):
                _cprint(f"  {_DIM}{line}{_RST}")
            if reqs["missing_packages"]:
                if _is_termux_environment():
                    _cprint(f"\n  {_BOLD}Option 1: pkg install termux-api{_RST}")
                    _cprint(f"  {_DIM}Then install/update the Termux:API Android app for microphone capture{_RST}")
                    _cprint(f"  {_BOLD}Option 2: pkg install python-numpy portaudio && python -m pip install sounddevice{_RST}")
                else:
                    _cprint(f"\n  {_BOLD}Install: {sys.executable} -m pip install {' '.join(reqs['missing_packages'])}{_RST}")
            return

        with self._voice_lock:
            self._voice_mode = True

        # Check config for auto_tts (shape-safe — malformed ``voice:`` YAML
        # leaves ``voice_config`` as a non-dict, so guard before .get()).
        try:
            from hermes_cli.config import load_config
            _raw_voice = load_config().get("voice")
            voice_config = _raw_voice if isinstance(_raw_voice, dict) else {}
            if voice_config.get("auto_tts", False):
                with self._voice_lock:
                    self._voice_tts = True
        except Exception:
            pass

        # Voice mode instruction is injected as a user message prefix (not a
        # system prompt change) to avoid invalidating the prompt cache.  See
        # _voice_message_prefix property and its usage in _process_message().

        tts_status = " (TTS enabled)" if self._voice_tts else ""
        # Use the startup-pinned cache so the advertised shortcut always
        # matches the live prompt_toolkit binding — reading live config
        # here would drift after a mid-session config edit (Copilot
        # round-14 on #19835, same class as round-13).
        _ptt_display = self._voice_record_key_label()
        _cprint(f"\n{_ACCENT}Voice mode enabled{tts_status}{_RST}")
        _cprint(f"  {_DIM}{_ptt_display} to start/stop recording{_RST}")
        _cprint(f"  {_DIM}/voice tts  to toggle speech output{_RST}")
        _cprint(f"  {_DIM}/voice off  to disable voice mode{_RST}")

    def _disable_voice_mode(self):
        """Disable voice mode, cancel any active recording, and stop TTS."""
        recorder = None
        with self._voice_lock:
            if self._voice_recording and self._voice_recorder:
                self._voice_recorder.cancel()
                self._voice_recording = False
            recorder = self._voice_recorder
            self._voice_mode = False
            self._voice_tts = False
            self._voice_continuous = False

        # Shut down the persistent audio stream in background
        if recorder is not None:
            def _bg_shutdown(rec=recorder):
                try:
                    rec.shutdown()
                except Exception:
                    pass
            threading.Thread(target=_bg_shutdown, daemon=True).start()
            self._voice_recorder = None

        # Stop any active TTS playback
        try:
            from tools.voice_mode import stop_playback
            stop_playback()
        except Exception:
            pass
        self._voice_tts_done.set()

        _cprint(f"\n{_DIM}Voice mode disabled.{_RST}")

    def _toggle_voice_tts(self):
        """Toggle TTS output for voice mode."""
        if not self._voice_mode:
            _cprint(f"{_DIM}Enable voice mode first: /voice on{_RST}")
            return

        with self._voice_lock:
            self._voice_tts = not self._voice_tts
        status = "enabled" if self._voice_tts else "disabled"

        if self._voice_tts:
            from tools.tts_tool import check_tts_requirements
            if not check_tts_requirements():
                _cprint(f"{_DIM}Warning: No TTS provider available. Install edge-tts or set API keys.{_RST}")

        _cprint(f"{_ACCENT}Voice TTS {status}.{_RST}")

    def _show_voice_status(self):
        """Show current voice mode status."""
        from tools.voice_mode import check_voice_requirements

        reqs = check_voice_requirements()

        _cprint(f"\n{_BOLD}Voice Mode Status{_RST}")
        _cprint(f"  Mode:      {'ON' if self._voice_mode else 'OFF'}")
        _cprint(f"  TTS:       {'ON' if self._voice_tts else 'OFF'}")
        _cprint(f"  Recording: {'YES' if self._voice_recording else 'no'}")
        # Display the startup-pinned label so /voice status always
        # matches the live prompt_toolkit binding (Copilot round-14 on
        # #19835, same class as round-13). Reading live config here
        # would drift after a mid-session config edit.
        _cprint(f"  Record key: {self._voice_record_key_label()}")
        _cprint(f"\n  {_BOLD}Requirements:{_RST}")
        for line in reqs["details"].split("\n"):
            _cprint(f"    {line}")

