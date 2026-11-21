import base64
import hashlib
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import requests

from config import save_config

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = " ".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-library-read",
    "user-library-modify",
    "playlist-read-private",
    "playlist-read-collaborative",
])
API_BASE = "https://api.spotify.com/v1"


@dataclass
class TrackItem:
    """Represents a Spotify track or playlist entry inside the Now Bar drawer."""
    uri: str
    title: str
    artist: str
    duration_ms: int = 0
    is_playlist: bool = False
    playlist_id: str = ""
    track_count: int = 0


@dataclass
class SpotifyExtraState:
    """Snapshot of playback metadata fetched from the Spotify Web API."""
    connected: bool = False
    track_uri: str = ""
    is_liked: bool = False
    shuffle: bool = False
    repeat: str = "off"          # "off" | "context" | "track"
    volume: int = -1
    progress_ms: int = -1
    duration_ms: int = -1
    next_up: str = ""
    queue_items: List[TrackItem] = field(default_factory=list)
    cover_bytes: Optional[bytes] = None


def _parse_track(obj: dict) -> Optional[TrackItem]:
    """Converts a raw Spotify track JSON dict into a TrackItem."""
    if not isinstance(obj, dict):
        return None
    uri = obj.get("uri", "")
    name = (obj.get("name") or "").strip()
    if not uri or not name:
        return None
    artists = ", ".join(
        a.get("name", "") for a in (obj.get("artists") or []) if a.get("name")
    )
    dur = int(obj.get("duration_ms") or 0)
    return TrackItem(
        uri=uri,
        title=name,
        artist=artists or "Unknown Artist",
        duration_ms=dur,
    )


class SpotifyWebAPI:
    """Thread-safe OAuth 2.0 PKCE client for Spotify's Web API."""

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._tls = threading.local()
        self._last_cover_url = ""
        self._cached_cover: Optional[bytes] = None
        self.last_error = ""

    def _get_session(self) -> requests.Session:
        """Returns a thread-local Session so concurrent calls never collide on sockets."""
        sess = getattr(self._tls, "session", None)
        if sess is None:
            sess = requests.Session()
            self._tls.session = sess
        return sess

    def _reset_session(self) -> None:
        """Closes and recreates the current thread's HTTP session after socket errors."""
        sess = getattr(self._tls, "session", None)
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass
        self._tls.session = requests.Session()

    @property
    def is_connected(self) -> bool:
        return bool(self._cfg.get("client_id") and self._cfg.get("refresh_token"))

    def start_pkce_auth(
        self, client_id: str, on_done: Callable[[bool, str], None]
    ) -> None:
        """Runs OAuth 2.0 PKCE authorization on a background thread via 127.0.0.1:8888."""
        client_id = client_id.strip()
        if not client_id:
            on_done(False, "Client ID cannot be empty.")
            return

        def _worker():
            verifier = secrets.token_urlsafe(64)[:96]
            digest = hashlib.sha256(verifier.encode("ascii")).digest()
            challenge = (
                base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
            )
            state_token = secrets.token_urlsafe(16)
            auth_result = {"code": None, "error": None}

            class CallbackHandler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    parsed = urllib.parse.urlparse(self.path)
                    if parsed.path != "/callback":
                        self.send_response(404)
                        self.end_headers()
                        return
                    qs = urllib.parse.parse_qs(parsed.query)
                    if qs.get("state", [""])[0] != state_token:
                        auth_result["error"] = "State mismatch."
                    elif "error" in qs:
                        auth_result["error"] = qs["error"][0]
                    elif "code" in qs:
                        auth_result["code"] = qs["code"][0]

                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    msg = (
                        "<h2>Spotify Now Bar Connected!</h2><p>You can close this tab now.</p>"
                        if auth_result["code"]
                        else f"<h2>Authorization Failed</h2><p>{auth_result['error']}</p>"
                    )
                    self.wfile.write(
                        f"<html><body style='font-family:Segoe UI;background:#111;color:#fff;"
                        f"display:grid;place-items:center;height:90vh;text-align:center'>{msg}</body></html>"
                        .encode("utf-8")
                    )

                def log_message(self, *_args):
                    pass

            try:
                server = http.server.HTTPServer(
                    ("127.0.0.1", 8888), CallbackHandler
                )
                server.timeout = 120
            except OSError as e:
                on_done(False, f"Port 8888 busy: {e}")
                return

            params = {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "state": state_token,
                "scope": SCOPES,
            }
            url = (
                "https://accounts.spotify.com/authorize?"
                + urllib.parse.urlencode(params)
            )
            webbrowser.open(url)

            server.handle_request()
            server.server_close()

            if not auth_result["code"]:
                on_done(
                    False,
                    auth_result["error"]
                    or "Authorization timed out or cancelled.",
                )
                return

            try:
                resp = self._get_session().post(
                    "https://accounts.spotify.com/api/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": auth_result["code"],
                        "redirect_uri": REDIRECT_URI,
                        "client_id": client_id,
                        "code_verifier": verifier,
                    },
                    timeout=10,
                )
                if resp.status_code != 200:
                    on_done(
                        False,
                        f"Token exchange failed ({resp.status_code}): {resp.text[:160]}",
                    )
                    return
                tok = resp.json()
                with self._lock:
                    self._cfg["client_id"] = client_id
                    self._cfg["access_token"] = tok["access_token"]
                    self._cfg["refresh_token"] = tok.get("refresh_token", "")
                    self._cfg["expires_at"] = time.time() + int(
                        tok.get("expires_in", 3600)
                    )
                    save_config(self._cfg)
                on_done(True, "Connected to Spotify!")
            except Exception as e:
                on_done(False, f"Network error: {e}")

        threading.Thread(target=_worker, daemon=True).start()

    def _ensure_token(self, force: bool = False) -> Optional[str]:
        """Returns a valid access token, refreshing automatically when needed."""
        with self._lock:
            if not self._cfg.get("client_id") or not self._cfg.get("refresh_token"):
                self.last_error = "Not linked to Spotify"
                return None
            if (
                not force
                and time.time() < float(self._cfg.get("expires_at", 0)) - 60
                and self._cfg.get("access_token")
            ):
                return self._cfg["access_token"]
            client_id = self._cfg["client_id"]
            refresh_token = self._cfg["refresh_token"]

        for attempt in range(2):
            try:
                resp = self._get_session().post(
                    "https://accounts.spotify.com/api/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": client_id,
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    tok = resp.json()
                    with self._lock:
                        self._cfg["access_token"] = tok["access_token"]
                        if tok.get("refresh_token"):
                            self._cfg["refresh_token"] = tok["refresh_token"]
                        self._cfg["expires_at"] = time.time() + int(
                            tok.get("expires_in", 3600)
                        )
                        save_config(self._cfg)
                        self.last_error = ""
                        return self._cfg["access_token"]
                self.last_error = (
                    f"Auth expired ({resp.status_code}) — Re-link Spotify"
                )
                return None
            except requests.RequestException:
                self._reset_session()
                if attempt == 1:
                    self.last_error = "Could not reach Spotify — check connection"
        return None

    def _req(
        self,
        method: str,
        path: str,
        params: dict = None,
        json_body: dict = None,
    ) -> Optional[requests.Response]:
        """Executes an authenticated Spotify API request with auto-retry on stale sockets/401."""
        token = self._ensure_token()
        if not token:
            return None

        for attempt in range(2):
            try:
                res = self._get_session().request(
                    method,
                    f"{API_BASE}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                    json=json_body,
                    timeout=10,
                )
                if res.status_code == 401 and attempt == 0:
                    new_token = self._ensure_token(force=True)
                    if new_token:
                        token = new_token
                        continue
                self.last_error = ""
                return res
            except requests.RequestException as e:
                self._reset_session()
                if attempt == 1:
                    self.last_error = f"Connection failed ({type(e).__name__})"
                    return None
        return None

    def fetch_extra_state(self, include_queue: bool = True) -> SpotifyExtraState:
        """Fetches active playback state, liked status, cover art, and optionally queue."""
        if not self.is_connected:
            return SpotifyExtraState(connected=False)

        res = self._req("GET", "/me/player")
        if not res or res.status_code != 200:
            return SpotifyExtraState(connected=True)

        data = res.json()
        item = data.get("item") or {}
        track_uri = item.get("uri", "")
        shuffle = bool(data.get("shuffle_state", False))
        repeat = data.get("repeat_state", "off")
        device = data.get("device") or {}
        volume = (
            int(device.get("volume_percent", -1))
            if device.get("volume_percent") is not None
            else -1
        )
        progress_ms = (
            int(data.get("progress_ms", -1))
            if data.get("progress_ms") is not None
            else -1
        )
        duration_ms = (
            int(item.get("duration_ms", -1))
            if item.get("duration_ms") is not None
            else -1
        )

        # Unified 2026 library check endpoint
        is_liked = False
        if track_uri:
            chk = self._req(
                "GET", "/me/library/contains", params={"uris": track_uri}
            )
            if chk and chk.status_code == 200:
                arr = chk.json()
                if isinstance(arr, list) and arr:
                    is_liked = bool(arr[0])

        # Medium-res album art (~300px) cached by URL
        cover_bytes = self._cached_cover
        images = (item.get("album") or {}).get("images") or []
        if images:
            chosen = images[1] if len(images) > 1 else images[0]
            url = chosen.get("url", "")
            if url and url != self._last_cover_url:
                try:
                    img_r = self._get_session().get(url, timeout=6)
                    if img_r.status_code == 200:
                        self._last_cover_url = url
                        self._cached_cover = img_r.content
                        cover_bytes = self._cached_cover
                except Exception:
                    pass

        queue_items: List[TrackItem] = []
        next_up = ""
        if include_queue:
            queue_items = self.fetch_queue()
            if queue_items:
                next_up = f"{queue_items[0].title} — {queue_items[0].artist}"

        return SpotifyExtraState(
            connected=True,
            track_uri=track_uri,
            is_liked=is_liked,
            shuffle=shuffle,
            repeat=repeat,
            volume=volume,
            progress_ms=progress_ms,
            duration_ms=duration_ms,
            next_up=next_up,
            queue_items=queue_items,
            cover_bytes=cover_bytes,
        )

    def fetch_queue(self) -> List[TrackItem]:
        res = self._req("GET", "/me/player/queue")
        if not res or res.status_code != 200:
            return []
        raw_q = res.json().get("queue") or []
        items: List[TrackItem] = []
        for entry in raw_q[:20]:
            t = _parse_track(entry)
            if t:
                items.append(t)
        return items

    def fetch_liked_songs(
        self, limit: int = 30, offset: int = 0
    ) -> List[TrackItem]:
        res = self._req(
            "GET", "/me/tracks", params={"limit": limit, "offset": offset}
        )
        if not res or res.status_code != 200:
            return []
        out: List[TrackItem] = []
        for row in res.json().get("items") or []:
            t = _parse_track(row.get("track") or row.get("item") or {})
            if t:
                out.append(t)
        return out

    def fetch_playlists(self, limit: int = 25) -> Tuple[List[TrackItem], str]:
        res = self._req("GET", "/me/playlists", params={"limit": limit})
        if not res:
            return [], self.last_error or "Connection failed — tap Retry"
        if res.status_code in (401, 403):
            return [], "Playlist permission needed — tap Re-link Spotify below"
        if res.status_code != 200:
            return [], f"Spotify API error ({res.status_code})"
        out: List[TrackItem] = []
        for pl in res.json().get("items") or []:
            if not pl:
                continue
            pl_id = pl.get("id", "")
            uri = pl.get("uri", "")
            name = (pl.get("name") or "Untitled Playlist").strip()
            owner = (pl.get("owner") or {}).get("display_name") or "Spotify"
            cnt = int(
                ((pl.get("items") or pl.get("tracks") or {}).get("total")) or 0
            )
            out.append(
                TrackItem(
                    uri=uri,
                    title=name,
                    artist=f"{owner} • {cnt} tracks" if cnt else owner,
                    is_playlist=True,
                    playlist_id=pl_id,
                    track_count=cnt,
                )
            )
        return out, ""

    def fetch_playlist_items(
        self, playlist_id: str, limit: int = 30
    ) -> Tuple[List[TrackItem], str]:
        # Spotify 2026 endpoint: GET /playlists/{id}/items
        res = self._req(
            "GET", f"/playlists/{playlist_id}/items", params={"limit": limit}
        )
        if not res:
            return [], self.last_error or "Connection failed — tap Retry"
        if res.status_code == 403:
            return (
                [],
                "Spotify API only lists tracks for playlists you own — tap Play All above",
            )
        if res.status_code != 200:
            return [], f"Unable to load tracks ({res.status_code})"
        out: List[TrackItem] = []
        for row in res.json().get("items") or []:
            obj = row.get("item") or row.get("track") or {}
            t = _parse_track(obj)
            if t:
                out.append(t)
        if not out:
            return [], "No tracks returned — tap Play All above to listen"
        return out, ""

    def search_tracks(self, query: str) -> List[TrackItem]:
        q = query.strip()
        if not q:
            return []
        # Spotify 2026 Dev Mode cap: limit <= 10
        res = self._req(
            "GET", "/search", params={"q": q, "type": "track", "limit": 10}
        )
        if not res or res.status_code != 200:
            return []
        raw = ((res.json().get("tracks") or {}).get("items")) or []
        out: List[TrackItem] = []
        for obj in raw:
            t = _parse_track(obj)
            if t:
                out.append(t)
        return out

    # --- Playback & Library Actions ---

    def play_track(self, uri: str) -> bool:
        res = self._req("PUT", "/me/player/play", json_body={"uris": [uri]})
        return bool(res and res.status_code in (200, 202, 204))

    def play_context(self, context_uri: str) -> bool:
        res = self._req(
            "PUT", "/me/player/play", json_body={"context_uri": context_uri}
        )
        return bool(res and res.status_code in (200, 202, 204))

    def add_to_queue(self, uri: str) -> bool:
        res = self._req("POST", "/me/player/queue", params={"uri": uri})
        return bool(res and res.status_code in (200, 202, 204))

    def toggle_like(self, track_uri: str, currently_liked: bool) -> bool:
        if not track_uri:
            return currently_liked
        method = "DELETE" if currently_liked else "PUT"
        res = self._req(method, "/me/library", params={"uris": track_uri})
        if res and res.status_code in (200, 204):
            return not currently_liked
        return currently_liked

    def save_to_library(self, track_uri: str) -> bool:
        res = self._req("PUT", "/me/library", params={"uris": track_uri})
        return bool(res and res.status_code in (200, 204))

    def remove_from_library(self, track_uri: str) -> bool:
        res = self._req("DELETE", "/me/library", params={"uris": track_uri})
        return bool(res and res.status_code in (200, 204))

    def set_shuffle(self, state: bool) -> bool:
        res = self._req(
            "PUT",
            "/me/player/shuffle",
            params={"state": "true" if state else "false"},
        )
        return bool(res and res.status_code in (200, 204))

    def set_repeat(self, mode: str) -> bool:
        res = self._req("PUT", "/me/player/repeat", params={"state": mode})
        return bool(res and res.status_code in (200, 204))

    def set_volume(self, percent: int) -> bool:
        vol = max(0, min(100, int(percent)))
        res = self._req(
            "PUT", "/me/player/volume", params={"volume_percent": vol}
        )
        return bool(res and res.status_code in (200, 204))

    def seek_ms(self, pos_ms: int) -> bool:
        res = self._req(
            "PUT", "/me/player/seek", params={"position_ms": max(0, int(pos_ms))}
        )
        return bool(res and res.status_code in (200, 204))