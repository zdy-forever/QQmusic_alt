#!/usr/bin/env python3
"""Local web version of the QQ Music client.

The browser UI talks to this local server. The server reuses the PC client's
QQ Music API implementation so browser CORS and login-cookie restrictions do
not leak into the frontend.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
PC_DIR = REPO_ROOT / "qqmusic_for_pc"
STATIC_DIR = ROOT / "static"

os.environ["QQMUSIC_AUTH_FILE"] = str(ROOT / ".qqmusic_auth.json")
os.environ["QQMUSIC_SETTINGS_FILE"] = str(ROOT / ".qqmusic_settings.json")

sys.path.insert(0, str(PC_DIR))

from qqmusic_client import (  # noqa: E402
    DEFAULT_TIMEOUT,
    QQMusicAPI,
    QQMusicError,
    QRLoginSession,
    WXLoginSession,
    clear_auth,
    format_seconds,
    load_auth,
    normalize_song,
    parse_lrc_lines,
    save_auth,
)

WEB_DEFAULT_SETTINGS: dict[str, Any] = {
    "quality": "320",
    "play_mode": "顺序播放",
    "initial_page_size": 50,
    "background_page_size": 500,
    "auto_sync_playlists": True,
}


def settings_path() -> Path:
    return ROOT / ".qqmusic_settings.json"


def load_web_settings() -> dict[str, Any]:
    settings = dict(WEB_DEFAULT_SETTINGS)
    if not settings_path().exists():
        with settings_path().open("w", encoding="utf-8") as file:
            json.dump(settings, file, ensure_ascii=False, indent=2)
        return settings
    try:
        with settings_path().open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        with settings_path().open("w", encoding="utf-8") as file:
            json.dump(settings, file, ensure_ascii=False, indent=2)
        return settings
    if isinstance(data, dict):
        settings.update({key: data[key] for key in WEB_DEFAULT_SETTINGS if key in data})
    return normalize_web_settings(settings)


def normalize_web_settings(settings: dict[str, Any]) -> dict[str, Any]:
    def bounded_int(key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(settings.get(key) or default)
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    quality = str(settings.get("quality") or "320")
    if quality not in {"128", "320", "flac"}:
        quality = "320"
    play_mode = str(settings.get("play_mode") or "顺序播放")
    if play_mode not in {"顺序播放", "随机播放", "单曲循环"}:
        play_mode = "顺序播放"
    return {
        "quality": quality,
        "play_mode": play_mode,
        "initial_page_size": bounded_int("initial_page_size", 50, 1, 500),
        "background_page_size": bounded_int("background_page_size", 500, 50, 1000),
        "auto_sync_playlists": bool(settings.get("auto_sync_playlists", True)),
    }


def save_web_settings(settings: dict[str, Any]) -> dict[str, Any]:
    current = load_web_settings()
    current.update({key: value for key, value in settings.items() if key in WEB_DEFAULT_SETTINGS})
    current = normalize_web_settings(current)
    with settings_path().open("w", encoding="utf-8") as file:
        json.dump(current, file, ensure_ascii=False, indent=2)
    return current


api = QQMusicAPI(timeout=int(os.environ.get("QQMUSIC_TIMEOUT", DEFAULT_TIMEOUT)))
login_sessions: dict[str, tuple[str, QRLoginSession | WXLoginSession]] = {}
login_lock = threading.Lock()


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def song_to_dict(song: Any) -> dict[str, Any]:
    raw = song.raw if isinstance(getattr(song, "raw", None), dict) else {}
    album = raw.get("album") if isinstance(raw.get("album"), dict) else {}
    album_mid = str(album.get("mid") or album.get("pmid") or "")
    cover = f"https://y.qq.com/music/photo_new/T002R300x300M000{album_mid}.jpg?max_age=2592000" if album_mid else ""
    return {
        "title": song.title,
        "mid": song.mid,
        "song_id": song.song_id,
        "song_type": song.song_type,
        "singers": song.singers,
        "album": song.album,
        "duration": song.duration,
        "duration_text": format_seconds(song.duration) if song.duration else "",
        "media_mid": song.media_mid,
        "local_path": getattr(song, "local_path", ""),
        "cover": cover,
    }


def playlist_to_dict(playlist: Any) -> dict[str, Any]:
    cover = playlist.cover
    if cover.startswith("//"):
        cover = f"https:{cover}"
    elif cover and not cover.startswith(("http://", "https://")):
        cover = ""
    return {
        "id": playlist.id,
        "name": playlist.name,
        "dirid": playlist.dirid,
        "song_count": playlist.song_count,
        "cover": cover,
        "display": playlist.display,
        "builtin": playlist.id == "__downloads__" or playlist.dirid == "201" or playlist.name == "我喜欢",
    }


def require_cookie() -> str:
    cookie = load_auth().get("cookie", "")
    if not cookie:
        raise QQMusicError("请先登录")
    return cookie


def require_account() -> tuple[str, str]:
    auth = load_auth()
    qq_number = auth.get("qq_number", "")
    cookie = auth.get("cookie", "")
    if not qq_number or not cookie:
        raise QQMusicError("请先登录")
    return qq_number, cookie


def safe_filename(value: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", value).strip(" .")
    return name or "qqmusic"


class Handler(SimpleHTTPRequestHandler):
    server_version = "QQMusicWeb/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if os.environ.get("QQMUSIC_WEB_LOG"):
            super().log_message(format, *args)

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, exc: Exception, status: int = 400) -> None:
        self.send_json({"ok": False, "error": str(exc)}, status)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}

    def parsed_url(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        return urllib.parse.unquote(parsed.path), urllib.parse.parse_qs(parsed.query)

    def first_query(self, query: dict[str, list[str]], key: str, default: str = "") -> str:
        values = query.get(key)
        return values[0] if values else default

    def do_GET(self) -> None:  # noqa: N802
        path, query = self.parsed_url()
        try:
            if path.startswith("/api/"):
                self.handle_api_get(path, query)
                return
            self.serve_static(path)
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(exc, 500)

    def do_POST(self) -> None:  # noqa: N802
        path, _query = self.parsed_url()
        try:
            self.handle_api_write("POST", path, self.read_json())
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(exc, 400)

    def do_PUT(self) -> None:  # noqa: N802
        path, _query = self.parsed_url()
        try:
            self.handle_api_write("PUT", path, self.read_json())
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(exc, 400)

    def do_DELETE(self) -> None:  # noqa: N802
        path, _query = self.parsed_url()
        try:
            self.handle_api_write("DELETE", path, self.read_json())
        except Exception as exc:  # noqa: BLE001
            self.send_error_json(exc, 400)

    def serve_static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/state":
            auth = load_auth()
            self.send_json({"ok": True, "logged_in": bool(auth.get("qq_number")), "account": auth.get("qq_number", "")})
            return

        if path == "/api/settings":
            self.send_json(
                {
                    "ok": True,
                    "settings": load_web_settings(),
                    "files": {
                        "auth": str(Path(os.environ["QQMUSIC_AUTH_FILE"])),
                        "settings": str(settings_path()),
                    },
                }
            )
            return

        if path == "/api/login/poll":
            session_id = self.first_query(query, "id")
            with login_lock:
                entry = login_sessions.get(session_id)
            if not entry:
                raise QQMusicError("登录会话不存在或已过期")
            provider, session = entry
            state, result = api.poll_wx_qr_login(session) if provider == "wechat" else api.poll_qr_login(session)
            if result:
                save_auth(result.qq_number, result.cookie)
                with login_lock:
                    login_sessions.pop(session_id, None)
                self.send_json({"ok": True, "state": "done", "account": result.qq_number, "nickname": result.nickname})
            else:
                self.send_json({"ok": True, "state": state})
            return

        if path == "/api/playlists":
            qq_number, cookie = require_account()
            playlists = [playlist_to_dict(item) for item in api.user_playlists(qq_number, cookie)]
            self.send_json({"ok": True, "playlists": playlists})
            return

        if path.startswith("/api/playlists/") and path.endswith("/songs"):
            playlist_id = path.removeprefix("/api/playlists/").removesuffix("/songs").strip("/")
            cookie = require_cookie()
            begin = int(self.first_query(query, "begin", "0") or "0")
            count = int(self.first_query(query, "count", "50") or "50")
            name, songs, total = api.playlist_songs_page(playlist_id, cookie, begin, count)
            self.send_json({"ok": True, "name": name, "songs": [song_to_dict(song) for song in songs], "total": total})
            return

        if path == "/api/search":
            cookie = require_cookie()
            keyword = self.first_query(query, "q").strip()
            if not keyword:
                raise QQMusicError("请输入搜索关键词")
            count = int(self.first_query(query, "count", "40") or "40")
            songs = api.search(keyword, count=count, cookie=cookie)
            self.send_json({"ok": True, "songs": [song_to_dict(song) for song in songs]})
            return

        if path == "/api/song-url":
            cookie = require_cookie()
            mid = self.first_query(query, "mid")
            quality = self.first_query(query, "quality", "320")
            media_mid = self.first_query(query, "media_mid")
            url = api.song_url(mid, quality, cookie, media_mid)
            self.send_json({"ok": True, "url": url})
            return

        if path == "/api/lyric":
            cookie = load_auth().get("cookie", "")
            mid = self.first_query(query, "mid")
            text = api.lyric(mid, cookie)
            lines = [{"time_ms": time_ms, "text": lyric} for time_ms, lyric in parse_lrc_lines(text)]
            self.send_json({"ok": True, "lyric": text, "lines": lines})
            return

        if path == "/api/download":
            self.stream_download(query)
            return

        raise QQMusicError(f"未知接口: {path}")

    def handle_api_write(self, method: str, path: str, payload: dict[str, Any]) -> None:
        if path == "/api/login/start" and method == "POST":
            provider = str(payload.get("provider") or "qq")
            if provider not in {"qq", "wechat"}:
                raise QQMusicError("登录方式无效")
            session = api.start_wx_qr_login() if provider == "wechat" else api.start_qr_login()
            session_id = uuid.uuid4().hex
            with login_lock:
                login_sessions[session_id] = (provider, session)
            image = "data:image/png;base64," + base64.b64encode(session.image).decode("ascii")
            self.send_json({"ok": True, "id": session_id, "image": image, "provider": provider})
            return

        if path == "/api/logout" and method == "POST":
            clear_auth()
            self.send_json({"ok": True})
            return

        if path == "/api/settings" and method == "PUT":
            self.send_json({"ok": True, "settings": save_web_settings(payload)})
            return

        if path == "/api/playlists" and method == "POST":
            cookie = require_cookie()
            name = str(payload.get("name") or "").strip()
            if not name:
                raise QQMusicError("歌单名称不能为空")
            playlist_id = api.create_playlist(name, cookie)
            self.send_json({"ok": True, "id": playlist_id})
            return

        if path.startswith("/api/playlists/"):
            parts = path.strip("/").split("/")
            if len(parts) >= 3:
                playlist_id = parts[2]
                cookie = require_cookie()
                if len(parts) == 3 and method == "PUT":
                    name = str(payload.get("name") or "").strip()
                    if not name:
                        raise QQMusicError("歌单名称不能为空")
                    api.rename_playlist(playlist_id, name, cookie)
                    self.send_json({"ok": True})
                    return
                if len(parts) == 3 and method == "DELETE":
                    api.delete_playlist(playlist_id, cookie)
                    self.send_json({"ok": True})
                    return
                if len(parts) == 4 and parts[3] == "songs" and method == "POST":
                    song = payload.get("song") if isinstance(payload.get("song"), dict) else payload
                    api.add_song_to_playlist(
                        str(song.get("song_id") or ""),
                        int(song.get("song_type") or 0),
                        playlist_id,
                        cookie,
                        str(song.get("mid") or ""),
                        str(payload.get("playlist_id") or ""),
                    )
                    self.send_json({"ok": True})
                    return
                if len(parts) == 4 and parts[3] == "songs" and method == "DELETE":
                    api.remove_song_from_playlist(
                        str(payload.get("song_id") or ""),
                        int(payload.get("song_type") or 0),
                        playlist_id,
                        cookie,
                    )
                    self.send_json({"ok": True})
                    return

        raise QQMusicError(f"未知接口: {method} {path}")

    def stream_download(self, query: dict[str, list[str]]) -> None:
        cookie = require_cookie()
        mid = self.first_query(query, "mid")
        quality = self.first_query(query, "quality", "320")
        media_mid = self.first_query(query, "media_mid")
        title = safe_filename(self.first_query(query, "title", mid or "qqmusic"))
        url = api.song_url(mid, quality, cookie, media_mid)
        extension = ".flac" if quality == "flac" else ".mp3"
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"})
        with urllib.request.urlopen(request, timeout=api.timeout) as response:
            self.send_response(200)
            self.send_header("Content-Type", response.headers.get("Content-Type", "audio/mpeg"))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(title + extension)}")
            length = response.headers.get("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            self.end_headers()
            while True:
                chunk = response.read(1024 * 128)
                if not chunk:
                    break
                self.wfile.write(chunk)


def main() -> int:
    host = os.environ.get("QQMUSIC_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("QQMUSIC_WEB_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"QQ Music Web Browser: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
