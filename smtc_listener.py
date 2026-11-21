import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions


@dataclass
class TrackState:
    active: bool = False
    title: str = ""
    artist: str = ""
    album: str = ""
    is_playing: bool = False
    position_ms: int = 0
    duration_ms: int = 0
    updated_at: float = field(default_factory=time.monotonic)
    thumbnail_bytes: Optional[bytes] = None

    @property
    def track_key(self) -> str:
        return f"{self.title}::{self.artist}"


def _timespan_to_ms(ts) -> int:
    if ts is None:
        return 0
    if hasattr(ts, "total_seconds"):
        return max(0, int(ts.total_seconds() * 1000))
    if hasattr(ts, "duration"):
        return max(0, int(ts.duration // 10_000))
    return 0


class SpotifySMTCListener:
    """Listens exclusively to Spotify via Windows SMTC on a background asyncio thread."""

    def __init__(self, on_update: Callable[[TrackState], None]):
        self._on_update = on_update
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._session = None
        self._manager = None
        self._tokens = []
        self._last_thumb_key = ""
        self._cached_thumb: Optional[bytes] = None
        self._state = TrackState()
        self._pending_refresh = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main_loop())
        except Exception:
            pass
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def _find_spotify_session(self):
        if not self._manager:
            return None
        try:
            for session in self._manager.get_sessions():
                app_id = (session.source_app_user_model_id or "").lower()
                if "spotify" in app_id:
                    return session
        except Exception:
            pass
        return None

    def _detach_session(self) -> None:
        if self._session:
            for remover, token in self._tokens:
                try:
                    remover(token)
                except Exception:
                    pass
        self._tokens.clear()
        self._session = None

    def _attach_session(self, session) -> None:
        self._detach_session()
        self._session = session
        if not session:
            return

        def schedule_refresh(*_args):
            if self._loop and not self._pending_refresh:
                self._pending_refresh = True
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self._debounced_refresh())
                )

        try:
            t1 = session.add_media_properties_changed(schedule_refresh)
            t2 = session.add_playback_info_changed(schedule_refresh)
            t3 = session.add_timeline_properties_changed(schedule_refresh)
            self._tokens = [
                (session.remove_media_properties_changed, t1),
                (session.remove_playback_info_changed, t2),
                (session.remove_timeline_properties_changed, t3),
            ]
        except Exception:
            pass

    async def _debounced_refresh(self) -> None:
        await asyncio.sleep(0.08)
        self._pending_refresh = False
        await self._read_session_state()

    async def _read_thumbnail(self, thumb_ref) -> Optional[bytes]:
        if not thumb_ref:
            return None
        try:
            stream = await thumb_ref.open_read_async()
            size = int(stream.size)
            if size <= 0 or size > 5_000_000:
                return None
            buf = Buffer(size)
            await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
            try:
                return bytes(buf)
            except Exception:
                reader = DataReader.from_buffer(buf)
                arr = bytearray(size)
                reader.read_bytes(arr)
                return bytes(arr)
        except Exception:
            return None

    async def _read_session_state(self) -> None:
        if not self._session:
            if self._state.active:
                self._state = TrackState(active=False)
                self._on_update(self._state)
            return

        try:
            props = await self._session.try_get_media_properties_async()
            pb_info = self._session.get_playback_info()
            timeline = self._session.get_timeline_properties()

            title = (props.title or "").strip() if props else ""
            artist = (props.artist or "").strip() if props else ""
            album = (props.album_title or "").strip() if props else ""
            is_playing = (
                pb_info is not None
                and pb_info.playback_status == PlaybackStatus.PLAYING
            )
            pos_ms = _timespan_to_ms(timeline.position) if timeline else 0
            dur_ms = _timespan_to_ms(timeline.end_time) if timeline else 0

            key = f"{title}::{artist}"
            thumb = self._cached_thumb
            if key != self._last_thumb_key or thumb is None:
                thumb = await self._read_thumbnail(props.thumbnail if props else None)
                self._last_thumb_key = key
                self._cached_thumb = thumb

            self._state = TrackState(
                active=bool(title),
                title=title,
                artist=artist,
                album=album,
                is_playing=is_playing,
                position_ms=pos_ms,
                duration_ms=dur_ms,
                updated_at=time.monotonic(),
                thumbnail_bytes=thumb,
            )
            self._on_update(self._state)
        except Exception:
            self._detach_session()
            if self._state.active:
                self._state = TrackState(active=False)
                self._on_update(self._state)

    async def _main_loop(self) -> None:
        self._manager = await MediaManager.request_async()
        while not self._stop_event.is_set():
            found = self._find_spotify_session()
            if found is None and self._session is not None:
                self._detach_session()
                self._state = TrackState(active=False)
                self._on_update(self._state)
            elif found is not None and self._session is None:
                self._attach_session(found)
                await self._read_session_state()
            elif found is not None and self._session is not None:
                # Confirm session hasn't been replaced by a Spotify restart
                try:
                    _ = self._session.get_playback_info()
                except Exception:
                    self._attach_session(found)
                    await self._read_session_state()

            await asyncio.sleep(1.5)

    # --- Playback Commands (instant local SMTC control) ---

    def _dispatch(self, coro_fn):
        if self._loop and self._loop.is_running() and self._session:
            asyncio.run_coroutine_threadsafe(coro_fn(), self._loop)

    def toggle_play_pause(self) -> None:
        async def _cmd():
            if self._session:
                await self._session.try_toggle_play_pause_async()
                await asyncio.sleep(0.12)
                await self._read_session_state()
        self._dispatch(_cmd)

    def next_track(self) -> None:
        async def _cmd():
            if self._session:
                await self._session.try_skip_next_async()
        self._dispatch(_cmd)

    def previous_track(self) -> None:
        async def _cmd():
            if self._session:
                await self._session.try_skip_previous_async()
        self._dispatch(_cmd)

    def seek_ms(self, target_ms: int) -> None:
        async def _cmd():
            if self._session:
                # WinRT expects 100-nanosecond ticks
                await self._session.try_change_playback_position_async(int(target_ms * 10_000))
                await asyncio.sleep(0.1)
                await self._read_session_state()
        self._dispatch(_cmd)