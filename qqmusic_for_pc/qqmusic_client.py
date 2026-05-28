#!/usr/bin/env python3
"""Small Music client for Linux.

This client uses metadata and playback-link APIs. It does not try to bypass
paid, region-locked, DRM-protected, or account-only content.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import binascii
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_API_BASE = "https://api.ygking.top"
DEFAULT_TIMEOUT = 15
DEFAULT_AUTH_FILE = Path(__file__).with_name(".qqmusic_auth.json")
DEFAULT_NETEASE_AUTH_FILE = Path(__file__).with_name(".netease_auth.json")
DEFAULT_SETTINGS_FILE = Path(__file__).with_name(".qqmusic_settings.json")
PLAYLIST_INITIAL_PAGE_SIZE = 50
PLAYLIST_BACKGROUND_PAGE_SIZE = 500
QQ_QR_APPID = "716027609"
QQ_QR_THIRD_APPID = "100497308"
WX_QR_APPID = "wx48db31d50e334801"
WX_QR_REDIRECT_URI = "https://y.qq.com/portal/wx_redirect.html?login_type=2&surl=https%3A%2F%2Fy.qq.com%2F"
QQ_QR_REDIRECT_URI = "https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https%3A%2F%2Fy.qq.com%2F"
NETEASE_BASE_URL = "https://music.163.com"
NETEASE_WEAPI_NONCE = "0CoJUm6Qyw8W8jud"
NETEASE_WEAPI_PUBKEY = "010001"
NETEASE_WEAPI_MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
    "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280"
    "104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee2559325"
    "75cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3"
    "ece0462db0a22b8e7"
)
NETEASE_WEB_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
)
NETEASE_DEVICE_XOR_KEY = "3go8&$8*3*3h0k(2)2"
PLATFORMS = {
    "qqmusic": "QQ 音乐",
    "netease": "网易云音乐",
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "platform": "qqmusic",
    "queue_show_singers": True,
    "queue_show_album": True,
    "queue_show_duration": True,
    "queue_show_mid": False,
    "queue_font_size": 11,
    "quality": "320",
    "play_mode": "顺序播放",
    "auto_sync_playlists": True,
    "download_dir": str(Path.home() / "音乐" / "Music"),
}


class QQMusicError(RuntimeError):
    pass


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass
class Song:
    title: str
    mid: str
    song_id: str = ""
    song_type: int = 0
    singers: str = ""
    album: str = ""
    duration: int | None = None
    media_mid: str = ""
    local_path: str = ""
    raw: dict[str, Any] | None = None

    @property
    def display(self) -> str:
        left = self.title
        if self.singers:
            left = f"{left} - {self.singers}"
        if self.album:
            left = f"{left}  [{self.album}]"
        if self.duration:
            left = f"{left}  {format_seconds(self.duration)}"
        return left


@dataclass
class Playlist:
    id: str
    name: str
    dirid: str = ""
    song_count: int | None = None
    cover: str = ""
    raw: dict[str, Any] | None = None

    @property
    def display(self) -> str:
        if self.song_count is None:
            return self.name
        return f"{self.name} ({self.song_count})"


@dataclass
class QRLoginSession:
    qrsig: str
    ptqrtoken: int
    image: bytes


@dataclass
class WXLoginSession:
    uuid: str
    image: bytes
    referer: str
    last_code: str = ""


@dataclass
class QRLoginResult:
    qq_number: str
    cookie: str
    nickname: str = ""


@dataclass
class LyricLine:
    time_ms: int
    text: str
    start_index: str = ""
    end_index: str = ""


def format_seconds(value: int) -> str:
    minutes, seconds = divmod(int(value), 60)
    return f"{minutes}:{seconds:02d}"


def format_milliseconds(value: int | float | None) -> str:
    if value is None:
        return "0:00"
    return format_seconds(max(0, int(value) // 1000))


def strip_lrc_timestamps(text: str) -> str:
    cleaned: list[str] = []
    for line in text.splitlines():
        while line.startswith("[") and "]" in line:
            line = line[line.index("]") + 1 :]
        if line.strip():
            cleaned.append(line)
    return "\n".join(cleaned)


def parse_lrc_lines(text: str) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    timestamp_pattern = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
    for raw_line in text.splitlines():
        matches = list(timestamp_pattern.finditer(raw_line))
        if not matches:
            continue
        lyric = timestamp_pattern.sub("", raw_line).strip()
        if not lyric:
            continue
        for match in matches:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            fraction = match.group(3) or "0"
            milliseconds = int(fraction.ljust(3, "0")[:3])
            entries.append(((minutes * 60 + seconds) * 1000 + milliseconds, lyric))
    return sorted(entries, key=lambda item: item[0])


def decode_possible_base64(text: str) -> str:
    try:
        return base64.b64decode(text).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return text


def parse_json_like_response(body: bytes) -> dict[str, Any]:
    texts = [
        body.decode("utf-8", "replace").strip(),
        body.decode("gb18030", "replace").strip(),
    ]
    for text in texts:
        candidates = [text]
        match = re.search(r"\((\{.*\})\)\s*;?$", text, flags=re.S)
        if match:
            candidates.append(match.group(1))
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidates.append(text[start : end + 1])
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    preview = texts[-1][:200] if texts else ""
    raise QQMusicError(f"接口返回的不是 JSON: {preview}")


def cookie_pairs(header_values: list[str]) -> list[str]:
    pairs: list[str] = []
    for header in header_values:
        pair = header.split(";", 1)[0].strip()
        if "=" in pair and not pair.endswith("="):
            pairs.append(pair)
    return pairs


def merge_cookie_pairs(existing: list[str], new_pairs: list[str]) -> list[str]:
    merged: dict[str, str] = {}
    for pair in [*existing, *new_pairs]:
        key, _, value = pair.partition("=")
        if key and value:
            merged[key] = value
    return [f"{key}={value}" for key, value in merged.items()]


def cookie_value(cookie: str, key: str) -> str:
    match = re.search(rf"(?:^|;\s*){re.escape(key)}=([^;]+)", cookie)
    return match.group(1) if match else ""


def hash33(value: str) -> int:
    result = 0
    for char in value:
        result += (result << 5) + ord(char)
    return result & 0x7FFFFFFF


def get_gtk(p_skey: str) -> int:
    result = 5381
    for char in p_skey:
        result += (result << 5) + ord(char)
    return result & 0x7FFFFFFF


def gtk_from_cookie(cookie: str) -> int:
    p_skey = cookie_value(cookie, "p_skey") or cookie_value(cookie, "skey")
    return get_gtk(p_skey) if p_skey else 5381


def musicu_login_fields(cookie: str) -> dict[str, str]:
    tme_login_type = cookie_value(cookie, "tmeLoginType")
    if tme_login_type:
        return {"tmeAppID": "qqmusic", "tmeLoginType": tme_login_type}
    if cookie_value(cookie, "login_type") == "2" or cookie_value(cookie, "wxuin"):
        return {"tmeAppID": "qqmusic", "tmeLoginType": "1"}
    return {}


def platform_display_name(platform: str) -> str:
    return PLATFORMS.get(platform, PLATFORMS["qqmusic"])


def normalize_platform(value: str | None) -> str:
    return value if value in PLATFORMS else "qqmusic"


def netease_weapi_encrypt(payload: dict[str, Any]) -> dict[str, str]:
    def aes_encrypt(text: str, key: str) -> str:
        pad_size = 16 - len(text.encode("utf-8")) % 16
        padded = text.encode("utf-8") + bytes([pad_size]) * pad_size
        openssl = shutil.which("openssl")
        if not openssl:
            raise QQMusicError("网易云登录需要系统 openssl 命令，请先安装 openssl")
        result = subprocess.run(
            [
                openssl,
                "enc",
                "-aes-128-cbc",
                "-K",
                key.encode("utf-8").hex(),
                "-iv",
                b"0102030405060708".hex(),
                "-nosalt",
                "-nopad",
            ],
            input=padded,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", "replace").strip()
            raise QQMusicError(f"网易云登录加密失败: {error or 'openssl 执行失败'}")
        return base64.b64encode(result.stdout).decode("ascii")

    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    sec_key = "".join(random.choice(chars) for _ in range(16))
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    params = aes_encrypt(aes_encrypt(text, NETEASE_WEAPI_NONCE), sec_key)
    reversed_key = sec_key[::-1].encode("utf-8").hex()
    enc_sec_key = format(
        pow(int(reversed_key, 16), int(NETEASE_WEAPI_PUBKEY, 16), int(NETEASE_WEAPI_MODULUS, 16)),
        "x",
    ).zfill(256)
    return {"params": params, "encSecKey": enc_sec_key}


class QQMusicAPI:
    def __init__(self, base_url: str = DEFAULT_API_BASE, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {key: value for key, value in (params or {}).items() if value is not None}
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 music-linux-client/1.0",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise QQMusicError(f"HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise QQMusicError(f"网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise QQMusicError("网络请求超时") from exc

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise QQMusicError("接口返回的不是 JSON") from exc

        code = payload.get("code")
        if code not in (None, 0, 200):
            message = payload.get("message") or payload.get("msg") or "接口返回错误"
            raise QQMusicError(str(message))
        return payload

    def _get_url_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        params = {key: value for key, value in (params or {}).items() if value is not None}
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Referer": "https://y.qq.com/portal/profile.html",
                "User-Agent": "Mozilla/5.0 music-linux-client/1.0",
                **(headers or {}),
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise QQMusicError(f"HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise QQMusicError(f"网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise QQMusicError("网络请求超时") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise QQMusicError("接口返回的不是 JSON") from exc

    def _request(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        method: str | None = None,
        follow_redirects: bool = True,
        timeout: int | None = None,
    ) -> tuple[int, bytes, list[str], str]:
        params = {key: value for key, value in (params or {}).items() if value is not None}
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "*/*",
                "User-Agent": "Mozilla/5.0 music-linux-client/1.0",
                **(headers or {}),
            },
        )

        opener = urllib.request.build_opener()
        if not follow_redirects:
            opener = urllib.request.build_opener(NoRedirectHandler)

        try:
            with opener.open(request, timeout=timeout or self.timeout) as response:
                body = response.read()
                set_cookies = response.headers.get_all("Set-Cookie") or []
                location = response.headers.get("Location", "")
                return response.status, body, set_cookies, location
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                body = exc.read()
                set_cookies = exc.headers.get_all("Set-Cookie") or []
                location = exc.headers.get("Location", "")
                return exc.code, body, set_cookies, location
            raise QQMusicError(f"HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise QQMusicError(f"网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise QQMusicError("网络请求超时") from exc

    def start_qr_login(self) -> QRLoginSession:
        _status, image, set_cookies, _location = self._request(
            "https://ssl.ptlogin2.qq.com/ptqrshow",
            {
                "appid": QQ_QR_APPID,
                "e": 2,
                "l": "M",
                "s": 3,
                "d": 72,
                "v": 4,
                "t": time.time(),
                "daid": 383,
                "pt_3rd_aid": QQ_QR_THIRD_APPID,
                "u1": "https://graph.qq.com/oauth2.0/login_jump",
            },
        )
        cookie = "; ".join(cookie_pairs(set_cookies))
        qrsig = cookie_value(cookie, "qrsig")
        if not qrsig:
            raise QQMusicError("没有拿到二维码会话，请稍后重试")
        return QRLoginSession(qrsig=qrsig, ptqrtoken=hash33(qrsig), image=image)

    def poll_qr_login(self, session: QRLoginSession) -> tuple[str, QRLoginResult | None]:
        cookie_pairs_list = [f"qrsig={session.qrsig}"]
        _status, body, set_cookies, _location = self._request(
            "https://ssl.ptlogin2.qq.com/ptqrlogin",
            {
                "u1": "https://graph.qq.com/oauth2.0/login_jump",
                "ptqrtoken": session.ptqrtoken,
                "ptredirect": 0,
                "h": 1,
                "t": 1,
                "g": 1,
                "from_ui": 1,
                "ptlang": 2052,
                "action": f"0-0-{int(time.time() * 1000)}",
                "js_ver": 23111510,
                "js_type": 1,
                "pt_uistyle": 40,
                "aid": QQ_QR_APPID,
                "daid": 383,
                "pt_3rd_aid": QQ_QR_THIRD_APPID,
                "pt_js_version": "v1.48.1",
            },
            {"Cookie": "; ".join(cookie_pairs_list), "Referer": "https://xui.ptlogin2.qq.com/"},
        )
        cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, cookie_pairs(set_cookies))
        text = body.decode("utf-8", "replace")

        if "二维码已失效" in text or "已失效" in text:
            return "expired", None
        if "二维码未失效" in text or "未扫描" in text:
            return "waiting", None
        if "认证中" in text or "确认" in text:
            return "confirming", None
        if "登录成功" not in text:
            return "waiting", None

        url_match = re.search(r"'(https?://[^']+)'", text)
        if not url_match:
            raise QQMusicError("扫码已确认，但没有拿到授权地址")
        nickname_match = re.search(r"'([^']*)'\s*\)\s*;?\s*$", text)
        nickname = nickname_match.group(1) if nickname_match else ""

        return "done", self.finish_qr_login(url_match.group(1), cookie_pairs_list, nickname)

    def finish_qr_login(self, check_sig_url: str, cookie_pairs_list: list[str], nickname: str = "") -> QRLoginResult:
        _status, _body, set_cookies, _location = self._request(
            check_sig_url,
            {"ptlang": 2052},
            {"Cookie": "; ".join(cookie_pairs_list), "Referer": "https://graph.qq.com/"},
            follow_redirects=False,
        )
        cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, cookie_pairs(set_cookies))
        cookie = "; ".join(cookie_pairs_list)
        p_skey = cookie_value(cookie, "p_skey") or cookie_value(cookie, "skey")
        if not p_skey:
            raise QQMusicError("扫码已确认，但没有拿到 QQ 音乐授权 Cookie")
        gtk = get_gtk(p_skey)

        form = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": QQ_QR_THIRD_APPID,
                "redirect_uri": QQ_QR_REDIRECT_URI,
                "scope": "get_user_info,get_app_friends",
                "state": "state",
                "switch": "",
                "from_ptlogin": 1,
                "src": 1,
                "update_auth": 1,
                "openapi": "1010_1030",
                "g_tk": gtk,
                "auth_time": str(int(time.time())),
                "ui": str(uuid.uuid4()).upper(),
            }
        ).encode("utf-8")
        _status, _body, set_cookies, location = self._request(
            "https://graph.qq.com/oauth2.0/authorize",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": "; ".join(cookie_pairs_list),
                "Referer": "https://graph.qq.com/oauth2.0/show",
            },
            data=form,
            method="POST",
            follow_redirects=False,
        )
        cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, cookie_pairs(set_cookies))
        code_match = re.search(r"[?&]code=([^&]+)", location)
        if not code_match:
            raise QQMusicError("QQ 授权失败，没有返回登录 code")

        login_payload = json.dumps(
            {
                "comm": {"g_tk": gtk, "platform": "yqq", "ct": 24, "cv": 0},
                "req": {
                    "module": "QQConnectLogin.LoginServer",
                    "method": "QQLogin",
                    "param": {"code": urllib.parse.unquote(code_match.group(1))},
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        _status, body, set_cookies, _location = self._request(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": "; ".join(cookie_pairs_list),
                "Referer": "https://y.qq.com/",
            },
            data=login_payload,
            method="POST",
        )
        cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, cookie_pairs(set_cookies))
        response = json.loads(body.decode("utf-8", "replace"))
        req_data = response.get("req", {}).get("data", {})
        musicid = str(req_data.get("musicid") or req_data.get("uin") or "")
        musickey = str(req_data.get("musickey") or "")
        if musicid:
            cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, [f"uin=o{musicid}"])
        if musickey:
            cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, [f"qm_keyst={musickey}", f"qqmusic_key={musickey}"])

        cookie = "; ".join(cookie_pairs_list)
        qq_number = musicid or qq_number_from_cookie(cookie)
        if not qq_number:
            raise QQMusicError("登录成功但没有识别到 QQ 号")
        return QRLoginResult(qq_number=qq_number, cookie=cookie, nickname=nickname)

    def start_wx_qr_login(self) -> WXLoginSession:
        params = {
            "appid": WX_QR_APPID,
            "redirect_uri": WX_QR_REDIRECT_URI,
            "response_type": "code",
            "scope": "snsapi_login",
            "state": "STATE",
            "href": "https://y.qq.com/mediastyle/music_v17/src/css/popup_wechat.css#wechat_redirect",
        }
        url = f"https://open.weixin.qq.com/connect/qrconnect?{urllib.parse.urlencode(params)}"
        _status, body, _set_cookies, _location = self._request(
            url,
            headers={"Referer": "https://y.qq.com/"},
        )
        html = body.decode("utf-8", "replace")
        uuid_match = re.search(r"/connect/qrcode/([A-Za-z0-9_-]+)", html)
        if not uuid_match:
            raise QQMusicError("没有拿到微信登录二维码，请稍后重试")
        wx_uuid = uuid_match.group(1)
        _status, image, _set_cookies, _location = self._request(
            f"https://open.weixin.qq.com/connect/qrcode/{wx_uuid}",
            headers={"Referer": url},
        )
        return WXLoginSession(uuid=wx_uuid, image=image, referer=url)

    def poll_wx_qr_login(self, session: WXLoginSession) -> tuple[str, QRLoginResult | None]:
        params = {"uuid": session.uuid}
        if session.last_code:
            params["last"] = session.last_code
        _status, body, _set_cookies, _location = self._request(
            "https://long.open.weixin.qq.com/connect/l/qrconnect",
            params,
            {"Referer": session.referer},
            timeout=35,
        )
        text = body.decode("utf-8", "replace")
        match = re.search(r"window\.wx_errcode=(\d+);window\.wx_code='([^']*)'", text)
        if not match:
            return "waiting", None
        errcode, code = match.group(1), match.group(2)
        session.last_code = errcode
        if errcode == "405" and code:
            return "done", self.finish_wx_qr_login(code)
        if errcode == "404":
            return "confirming", None
        if errcode == "402":
            return "expired", None
        if errcode == "403":
            return "cancelled", None
        return "waiting", None

    def finish_wx_qr_login(self, code: str) -> QRLoginResult:
        payload = json.dumps(
            {
                "comm": {"tmeAppID": "qqmusic", "tmeLoginType": "1", "platform": "yqq", "ct": 24, "cv": 0},
                "req": {
                    "module": "music.login.LoginServer",
                    "method": "Login",
                    "param": {"strAppid": WX_QR_APPID, "code": code},
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        _status, body, set_cookies, _location = self._request(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": WX_QR_REDIRECT_URI,
            },
            data=payload,
            method="POST",
        )
        response = parse_json_like_response(body)
        if response.get("code") not in (None, 0):
            raise QQMusicError(str(response.get("message") or response.get("msg") or "微信登录失败"))
        req = response.get("req") if isinstance(response.get("req"), dict) else {}
        if req.get("code") not in (None, 0):
            raise QQMusicError(str(req.get("message") or req.get("msg") or "微信登录失败"))
        req_data = req.get("data") if isinstance(req.get("data"), dict) else {}

        cookie_pairs_list = merge_cookie_pairs([], cookie_pairs(set_cookies))
        cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, ["login_type=2", "tmeLoginType=1"])
        wxuin = str(req_data.get("wxuin") or req_data.get("uin") or req_data.get("musicid") or "")
        musicid = str(req_data.get("musicid") or req_data.get("uin") or "")
        musickey = str(
            req_data.get("musickey")
            or req_data.get("music_key")
            or req_data.get("qm_keyst")
            or req_data.get("qqmusic_key")
            or ""
        )
        if wxuin:
            cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, [f"wxuin={wxuin}"])
        if musicid:
            cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, [f"uin=o{musicid}"])
        if musickey:
            cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, [f"qm_keyst={musickey}", f"qqmusic_key={musickey}"])

        cookie = "; ".join(cookie_pairs_list)
        qq_number = qq_number_from_cookie(cookie)
        if not qq_number:
            raise QQMusicError("微信扫码已确认，但没有识别到 QQ 音乐账号 ID")
        return QRLoginResult(qq_number=qq_number, cookie=cookie, nickname="微信登录")

    def search(self, keyword: str, count: int = 30, page: int = 1, cookie: str = "") -> list[Song]:
        if not cookie:
            raise QQMusicError("QQ 音乐搜索现在需要登录，请先登录后再搜索")
        uin = qq_number_from_cookie(cookie) or "0"
        request_data = {
            "comm": {
                "ct": 24,
                "cv": 0,
                "uin": uin,
                "format": "json",
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 1,
            },
            "req_1": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {
                    "remoteplace": "yqq.yqq.yqq",
                    "searchid": str(random.randint(10**17, 10**18 - 1)),
                    "search_type": 0,
                    "query": keyword,
                    "page_num": page,
                    "num_per_page": count,
                },
            },
        }
        _status, body, _set_cookies, _location = self._request(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            headers={
                "Cookie": cookie,
                "Referer": "https://y.qq.com/n/ryqq/search",
                "Content-Type": "application/json",
            },
            data=json.dumps(request_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
        )
        payload = parse_json_like_response(body)
        code = payload.get("code")
        req = payload.get("req_1") if isinstance(payload.get("req_1"), dict) else {}
        req_code = req.get("code")
        if code not in (None, 0) or req_code not in (None, 0):
            message = req.get("message") or req.get("msg") or payload.get("message") or payload.get("msg") or "搜索接口返回错误"
            raise QQMusicError(str(message))
        data = req.get("data") if isinstance(req.get("data"), dict) else {}
        body_data = data.get("body") if isinstance(data.get("body"), dict) else {}
        song_data = body_data.get("song") if isinstance(body_data.get("song"), dict) else {}
        items = song_data.get("list") if isinstance(song_data, dict) else []
        if not isinstance(items, list):
            items = []
        return [normalize_song(item) for item in items if isinstance(item, dict)]

    def song_url(self, mid: str, quality: str = "320", cookie: str = "", media_mid: str = "") -> str:
        try:
            qualities = [quality, "320", "128"] if quality == "flac" else [quality, "128"]
            tried: set[str] = set()
            last_error: QQMusicError | None = None
            for candidate in qualities:
                if candidate in tried:
                    continue
                tried.add(candidate)
                try:
                    return self.qq_song_url(mid, candidate, cookie, media_mid)
                except QQMusicError as exc:
                    last_error = exc
            if media_mid and media_mid != mid:
                for candidate in qualities:
                    if candidate in tried and len(tried) > len(qualities):
                        continue
                    try:
                        return self.qq_song_url(mid, candidate, cookie, mid)
                    except QQMusicError as exc:
                        last_error = exc
            raise last_error or QQMusicError("QQ 音乐没有返回可播放链接")
        except QQMusicError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise QQMusicError(f"获取播放链接失败: {exc}") from exc

    def qq_song_url(self, mid: str, quality: str = "320", cookie: str = "", media_mid: str = "") -> str:
        file_types = {
            "128": ("M500", ".mp3"),
            "320": ("M800", ".mp3"),
            "flac": ("F000", ".flac"),
        }
        prefix, suffix = file_types.get(quality, file_types["320"])
        filename = f"{prefix}{media_mid or mid}{suffix}"
        uin = qq_number_from_cookie(cookie) or "0"
        guid = str(random.randint(1000000000, 9999999999))
        request_data = {
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "filename": [filename],
                    "guid": guid,
                    "songmid": [mid],
                    "songtype": [0],
                    "uin": uin,
                    "loginflag": 1,
                    "platform": "20",
                },
            },
            "comm": {"uin": uin, "format": "json", "ct": 24, "cv": 0},
        }
        payload = self._get_url_json(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            {
                "g_tk": 5381,
                "loginUin": uin,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 0,
                "data": json.dumps(request_data, ensure_ascii=False, separators=(",", ":")),
            },
            {"Cookie": cookie, "Referer": "https://y.qq.com/"} if cookie else {"Referer": "https://y.qq.com/"},
        )
        data = payload.get("req_0", {}).get("data", {})
        midurlinfo = data.get("midurlinfo") or []
        purl = ""
        if midurlinfo and isinstance(midurlinfo[0], dict):
            purl = midurlinfo[0].get("purl") or midurlinfo[0].get("wifiurl") or ""
        if not purl:
            raise QQMusicError("QQ 音乐没有返回可播放链接，可能是会员、版权、海外地区、音质限制，或当前登录方式权限不足")
        if purl.startswith("http"):
            return purl
        sip = data.get("sip") or []
        domain = next((item for item in sip if isinstance(item, str) and not item.startswith("http://ws")), "")
        if not domain and sip:
            domain = str(sip[0])
        if not domain:
            domain = "https://dl.stream.qqmusic.qq.com/"
        return urllib.parse.urljoin(domain, purl)

    def lyric(self, mid: str, cookie: str = "") -> str:
        return self.qq_lyric(mid, cookie)

    def qq_lyric(self, mid: str, cookie: str = "") -> str:
        uin = qq_number_from_cookie(cookie) or "0"
        payload = self._get_url_json(
            "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
            {
                "songmid": mid,
                "g_tk": 5381,
                "loginUin": uin,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 0,
                "nobase64": 1,
            },
            {"Cookie": cookie, "Referer": "https://y.qq.com/"} if cookie else {"Referer": "https://y.qq.com/"},
        )
        lyric = str(payload.get("lyric") or "")
        trans = str(payload.get("trans") or "")
        if lyric and not lyric.lstrip().startswith("["):
            lyric = decode_possible_base64(lyric)
        if trans and not trans.lstrip().startswith("["):
            trans = decode_possible_base64(trans)
        if trans:
            return f"{lyric}\n\n--- 翻译 ---\n{trans}".strip()
        return lyric.strip()

    def playlist_songs_page(
        self,
        playlist_id: str,
        cookie: str = "",
        begin: int = 0,
        count: int = PLAYLIST_BACKGROUND_PAGE_SIZE,
    ) -> tuple[str, list[Song], int]:
        payload = self._get_url_json(
            "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg",
            {
                "disstid": playlist_id,
                "type": 1,
                "json": 1,
                "utf8": 1,
                "onlysong": 0,
                "new_format": 1,
                "g_tk": 5381,
                "loginUin": 0,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 0,
                "song_begin": max(0, begin),
                "song_num": max(1, count),
            },
            {
                "Cookie": cookie,
                "Referer": f"https://y.qq.com/n/ryqq/playlist/{playlist_id}",
            }
            if cookie
            else {"Referer": f"https://y.qq.com/n/ryqq/playlist/{playlist_id}"},
        )

        code = payload.get("code")
        if code not in (None, 0):
            message = payload.get("message") or payload.get("msg") or "歌单详情接口返回错误"
            raise QQMusicError(str(message))

        cdlist = payload.get("cdlist") or []
        data = cdlist[0] if isinstance(cdlist, list) and cdlist else payload.get("data") or {}
        if not isinstance(data, dict):
            raise QQMusicError("歌单详情接口返回格式异常")

        name = str(data.get("dissname") or data.get("name") or f"歌单 {playlist_id}")
        items = data.get("songlist") or data.get("songs") or []
        if not isinstance(items, list):
            items = []
        total_value = data.get("total_song_num") or data.get("songnum") or data.get("total") or data.get("song_count")
        try:
            total = int(total_value) if total_value is not None else begin + len(items)
        except (TypeError, ValueError):
            total = begin + len(items)
        songs = [normalize_song(item) for item in items if isinstance(item, dict)]
        return name, songs, max(total, begin + len(songs))

    def playlist_songs(self, playlist_id: str, cookie: str = "") -> tuple[str, list[Song]]:
        name, results, total = self.playlist_songs_page(playlist_id, cookie, 0, PLAYLIST_BACKGROUND_PAGE_SIZE)
        loaded = len(results)
        while True:
            page_name, page_results, page_total = self.playlist_songs_page(playlist_id, cookie, loaded, PLAYLIST_BACKGROUND_PAGE_SIZE)
            if page_name:
                name = page_name
            if page_total > total:
                total = page_total
            if not page_results:
                break
            results.extend(page_results)
            loaded += len(page_results)
            if len(page_results) < PLAYLIST_BACKGROUND_PAGE_SIZE:
                break
            if loaded >= total:
                total = loaded + PLAYLIST_BACKGROUND_PAGE_SIZE
        return name, results

    def user_playlists(self, qq_number: str, cookie: str = "") -> list[Playlist]:
        payload = self._get_url_json(
            "https://c.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss",
            {
                "hostUin": 0,
                "hostuin": qq_number,
                "sin": 0,
                "size": 200,
                "g_tk": 5381,
                "loginUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 0,
            },
            {"Cookie": cookie} if cookie else None,
        )

        code = payload.get("code")
        if code == 4000:
            raise QQMusicError("这个账号没有公开歌单，或当前 Cookie 没有权限读取")
        if code == 1000:
            raise QQMusicError("登录已失效，请重新导入 QQ 音乐 Cookie")

        data = payload.get("data") or {}
        items = data.get("disslist") or []
        if not isinstance(items, list):
            raise QQMusicError("用户歌单接口返回格式异常")

        playlists = [normalize_playlist(item) for item in items if isinstance(item, dict)]
        return [playlist for playlist in playlists if playlist.id]

    def user_nickname(self, qq_number: str, cookie: str = "") -> str:
        if not qq_number:
            return ""
        payload = self._get_url_json(
            "https://c.y.qq.com/rsc/fcgi-bin/fcg_get_profile_homepage.fcg",
            {
                "cid": 205360838,
                "userid": qq_number,
                "reqfrom": 1,
                "g_tk": 5381,
                "loginUin": qq_number,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.json",
                "needNewCode": 0,
            },
            {"Cookie": cookie} if cookie else None,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        creator = data.get("creator") if isinstance(data.get("creator"), dict) else {}
        return str(creator.get("nick") or creator.get("nickname") or "").strip()

    def add_song_to_playlist(
        self,
        song_id: str,
        song_type: int,
        playlist_dirid: str,
        cookie: str,
        song_mid: str = "",
        playlist_id: str = "",
    ) -> None:
        if not cookie:
            raise QQMusicError("请先登录")
        if not song_id:
            raise QQMusicError("当前歌曲缺少 QQ 音乐 song id")
        dir_id: int | str = int(playlist_dirid) if str(playlist_dirid).isdigit() else playlist_dirid
        payload = self._post_musicu_json(
            {
                "req_0": {
                    "module": "music.musicasset.PlaylistDetailWrite",
                    "method": "AddSonglist",
                    "param": {
                        "dirId": dir_id,
                        "v_songInfo": [
                            {
                                "songType": song_type,
                                "songId": int(song_id) if str(song_id).isdigit() else song_id,
                            }
                        ],
                    },
                }
            },
            cookie,
            "https://y.qq.com/n/ryqq/playlist",
            include_comm=False,
        )
        tried_legacy = False
        try:
            self._raise_if_musicu_failed(payload, "req_0", "添加歌曲")
        except QQMusicError as exc:
            if not song_mid:
                raise
            try:
                self.add_song_to_playlist_legacy(song_mid, song_type, playlist_dirid, cookie)
                tried_legacy = True
            except QQMusicError:
                raise exc
        if playlist_id and not self._playlist_contains_song_after_add(playlist_id, song_id, song_mid, cookie):
            if song_mid and not tried_legacy:
                self.add_song_to_playlist_legacy(song_mid, song_type, playlist_dirid, cookie)
                if self._playlist_contains_song_after_add(playlist_id, song_id, song_mid, cookie):
                    return
            raise QQMusicError("QQ 音乐接口返回成功，但刷新目标歌单后没有发现这首歌。请重新登录后再试。")

    def add_song_to_playlist_legacy(self, song_mid: str, song_type: int, playlist_dirid: str, cookie: str) -> None:
        uin = qq_number_from_cookie(cookie)
        payload = self._post_form_json(
            "https://c.y.qq.com/splcloud/fcgi-bin/fcg_music_add2songdir.fcg",
            {
                "g_tk": gtk_from_cookie(cookie),
                "loginUin": uin,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "notice": 0,
                "platform": "yqq.post",
                "needNewCode": 0,
                "uin": uin,
                "dirid": playlist_dirid,
                "midlist": song_mid,
                "typelist": song_type,
                "addtype": "",
                "formsender": 4,
                "source": 103,
                "utf8": 1,
            },
            cookie,
            "https://y.qq.com/n/ryqq/playlist",
        )
        self._raise_if_write_failed(payload, "添加歌曲")

    def _playlist_contains_song_after_add(self, playlist_id: str, song_id: str, song_mid: str, cookie: str) -> bool:
        for attempt in range(3):
            if attempt:
                time.sleep(1)
            _name, songs = self.playlist_songs(playlist_id, cookie)
            for song in songs:
                if song_id and song.song_id == song_id:
                    return True
                if song_mid and song.mid == song_mid:
                    return True
        return False

    def remove_song_from_playlist(self, song_id: str, song_type: int, playlist_dirid: str, cookie: str) -> None:
        if not cookie:
            raise QQMusicError("请先登录")
        if not song_id:
            raise QQMusicError("当前歌曲缺少 QQ 音乐 song id")
        dir_id: int | str = int(playlist_dirid) if str(playlist_dirid).isdigit() else playlist_dirid
        payload = self._post_musicu_json(
            {
                "req_0": {
                    "module": "music.musicasset.PlaylistDetailWrite",
                    "method": "DelSonglist",
                    "param": {
                        "dirId": dir_id,
                        "v_songInfo": [
                            {
                                "songType": song_type,
                                "songId": int(song_id) if str(song_id).isdigit() else song_id,
                            }
                        ],
                    },
                }
            },
            cookie,
            "https://y.qq.com/n/yqq/playlist",
            include_comm=False,
        )
        self._raise_if_musicu_failed(payload, "req_0", "移除歌曲")

    def create_playlist(self, name: str, cookie: str) -> str:
        if not cookie:
            raise QQMusicError("请先登录")
        uin = qq_number_from_cookie(cookie)
        payload = self._post_form_json(
            "https://c.y.qq.com/splcloud/fcgi-bin/create_playlist.fcg",
            {
                "g_tk": 5381,
                "loginUin": uin,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf8",
                "notice": 0,
                "platform": "yqq",
                "needNewCode": 0,
                "uin": uin,
                "name": name,
                "show": 1,
                "formsender": 1,
                "utf8": 1,
                "qzreferrer": "https://y.qq.com/portal/profile.html#sub=other&tab=create&",
            },
            cookie,
            "https://y.qq.com/n/yqq/playlist",
        )
        self._raise_if_write_failed(payload, "创建歌单")
        return str(payload.get("dirid") or "")

    def delete_playlist(self, playlist_dirid: str, cookie: str) -> None:
        if not cookie:
            raise QQMusicError("请先登录")
        uin = qq_number_from_cookie(cookie)
        payload = self._post_form_json(
            "https://c.y.qq.com/splcloud/fcgi-bin/fcg_fav_modsongdir.fcg",
            {
                "g_tk": 5381,
                "loginUin": uin,
                "hostUin": 0,
                "format": "json",
                "inCharset": "GB2312",
                "outCharset": "gb2312",
                "notice": 0,
                "platform": "yqq",
                "needNewCode": 0,
                "uin": uin,
                "delnum": 1,
                "deldirids": playlist_dirid,
                "forcedel": 1,
                "formsender": 1,
                "source": 103,
            },
            cookie,
            "https://y.qq.com/n/yqq/playlist",
            "gb18030",
        )
        self._raise_if_write_failed(payload, "删除歌单")

    def rename_playlist(self, playlist_dirid: str, new_name: str, cookie: str) -> None:
        if not cookie:
            raise QQMusicError("请先登录")
        dir_id: int | str = int(playlist_dirid) if str(playlist_dirid).isdigit() else playlist_dirid
        payload = self._post_musicu_json(
            {
                "req_0": {
                    "module": "music.musicasset.PlaylistBaseWrite",
                    "method": "EditPlaylist",
                    "param": {
                        "dirId": dir_id,
                        "mask": 1,
                        "dirNewName": new_name,
                    },
                }
            },
            cookie,
            f"https://y.qq.com/n/ryqq/playlist_edit/{playlist_dirid}",
            include_comm=False,
        )
        self._raise_if_musicu_failed(payload, "req_0", "重命名歌单")

    def _post_form_json(self, url: str, params: dict[str, Any], cookie: str, referer: str, encoding: str = "utf-8") -> dict[str, Any]:
        clean_params = {key: value for key, value in params.items() if value is not None}
        body = urllib.parse.urlencode(clean_params, encoding=encoding).encode(encoding, "replace")
        _status, response_body, _set_cookies, _location = self._request(
            url,
            headers={
                "Cookie": cookie,
                "Referer": referer,
                "Content-Type": f"application/x-www-form-urlencoded; charset={encoding}",
            },
            data=body,
            method="POST",
        )
        payload = parse_json_like_response(response_body)
        if not isinstance(payload, dict):
            raise QQMusicError("接口返回格式异常")
        return payload

    def _post_musicu_json(
        self,
        requests: dict[str, Any],
        cookie: str,
        referer: str = "https://y.qq.com/",
        include_comm: bool = True,
    ) -> dict[str, Any]:
        uin = qq_number_from_cookie(cookie)
        comm = {
            "g_tk": gtk_from_cookie(cookie),
            "uin": uin,
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "notice": 0,
            "platform": "yqq.json",
            "needNewCode": 1,
        }
        comm.update(musicu_login_fields(cookie))
        payload = {"comm": comm, **requests} if include_comm else dict(requests)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        _status, response_body, _set_cookies, _location = self._request(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            headers={
                "Cookie": cookie,
                "Referer": referer,
                "Content-Type": "application/json",
            },
            data=body,
            method="POST",
        )
        result = parse_json_like_response(response_body)
        if not isinstance(result, dict):
            raise QQMusicError("接口返回格式异常")
        return result

    def _raise_if_write_failed(self, payload: dict[str, Any], action: str) -> None:
        failure_code: Any = None
        for key in ("code", "subcode", "retcode", "retCode", "errcode"):
            value = payload.get(key)
            if value is None:
                continue
            try:
                is_success = int(value) == 0
            except (TypeError, ValueError):
                is_success = str(value).lower() in {"ok", "success"}
            if not is_success:
                failure_code = value
                break
        if failure_code is None:
            return
        message = payload.get("msg") or payload.get("message") or payload.get("errMsg") or f"{action}失败（错误码: {failure_code}）"
        if str(failure_code) == "1000" or (str(failure_code) == "1" and message == f"{action}失败"):
            raise QQMusicError("登录已失效，请重新登录")
        raise QQMusicError(str(message))

    def _raise_if_musicu_failed(self, payload: dict[str, Any], request_key: str, action: str) -> None:
        self._raise_if_write_failed(payload, action)
        request_payload = payload.get(request_key)
        if not isinstance(request_payload, dict):
            raise QQMusicError(f"{action}失败")
        self._raise_if_write_failed(request_payload, action)
        data = request_payload.get("data")
        if not isinstance(data, dict):
            return
        self._raise_if_write_failed(data, action)
        retcode = data.get("retCode")
        if retcode in (None, 0):
            return
        message = data.get("msg") or data.get("message") or data.get("errMsg") or f"{action}失败（错误码: {retcode}）"
        if str(retcode) == "1000":
            raise QQMusicError("登录已失效，请重新登录")
        raise QQMusicError(str(message))


class NeteaseMusicAPI:
    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.session_cookie_pairs: list[str] = []
        self.device_id = "".join(random.choice("0123456789ABCDEF") for _ in range(52))
        self._ntes_nuid = binascii.hexlify(os.urandom(32)).decode("ascii")
        self.wnmcid = "".join(random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(6)) + f".{int(time.time() * 1000)}.01.0"
        self.anonymous_ready = False

    def _base_cookie_pairs(self) -> list[str]:
        return [
            "__remember_me=true",
            "ntes_kaola_ad=1",
            f"_ntes_nuid={self._ntes_nuid}",
            f"_ntes_nnid={self._ntes_nuid},{int(time.time() * 1000)}",
            f"WNMCID={self.wnmcid}",
            "WEVNSM=1.0.0",
            "osver=Microsoft-Windows-10-Professional-build-19045-64bit",
            f"deviceId={self.device_id}",
            "os=pc",
            "channel=netease",
            "appver=3.1.17.204416",
        ]

    def _merged_cookie_header(self, explicit_cookie: str = "") -> str:
        explicit_pairs = [pair.strip() for pair in explicit_cookie.split(";") if "=" in pair]
        merged = merge_cookie_pairs(self._base_cookie_pairs(), self.session_cookie_pairs)
        merged = merge_cookie_pairs(merged, explicit_pairs)
        has_music_u = any(pair.split("=", 1)[0] == "MUSIC_U" for pair in merged)
        has_music_a = any(pair.split("=", 1)[0] == "MUSIC_A" for pair in merged)
        if not has_music_u and not has_music_a:
            # The anonymous registration call will fill MUSIC_A. Keep the
            # rest of the web client cookies stable before that happens.
            pass
        return "; ".join(merged)

    def _cloudmusic_dll_encode_id(self, value: str) -> str:
        xored = "".join(
            chr(ord(char) ^ ord(NETEASE_DEVICE_XOR_KEY[index % len(NETEASE_DEVICE_XOR_KEY)]))
            for index, char in enumerate(value)
        )
        digest = hashlib.md5(xored.encode("utf-8")).digest()
        return base64.b64encode(digest).decode("ascii")

    def ensure_anonymous_session(self) -> None:
        if self.anonymous_ready or any(pair.startswith("MUSIC_A=") for pair in self.session_cookie_pairs):
            self.anonymous_ready = True
            return
        encoded_id = base64.b64encode(f"{self.device_id} {self._cloudmusic_dll_encode_id(self.device_id)}".encode("utf-8")).decode("ascii")
        try:
            payload, set_cookies = self._post_form_json(
                "/weapi/register/anonimous",
                {"username": encoded_id},
                encrypted=True,
                include_session_cookie=False,
            )
        except QQMusicError:
            return
        if payload.get("code") == 200 and set_cookies:
            self.session_cookie_pairs = merge_cookie_pairs(self.session_cookie_pairs, cookie_pairs(set_cookies))
            self.anonymous_ready = True

    def _request(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        method: str | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        params = {key: value for key, value in (params or {}).items() if value is not None}
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request_headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
                "User-Agent": NETEASE_WEB_UA,
                **(headers or {}),
        }
        request_headers["Cookie"] = self._merged_cookie_header(request_headers.get("Cookie", ""))

        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                set_cookies = response.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as exc:
            body = exc.read()
            set_cookies = exc.headers.get_all("Set-Cookie") or []
        except urllib.error.URLError as exc:
            raise QQMusicError(f"网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise QQMusicError("网络请求超时") from exc
        payload = parse_json_like_response(body)
        if not isinstance(payload, dict):
            raise QQMusicError("网易云接口返回格式异常")
        if set_cookies:
            self.session_cookie_pairs = merge_cookie_pairs(self.session_cookie_pairs, cookie_pairs(set_cookies))
        return payload, set_cookies

    def _get_json(self, path: str, params: dict[str, Any] | None = None, cookie: str = "") -> dict[str, Any]:
        headers = {"Cookie": cookie} if cookie else None
        payload, _set_cookies = self._request(f"{NETEASE_BASE_URL}{path}", params=params, headers=headers)
        return payload

    def _post_form_json(
        self,
        path: str,
        params: dict[str, Any],
        cookie: str = "",
        encrypted: bool = False,
        include_session_cookie: bool = True,
    ) -> tuple[dict[str, Any], list[str]]:
        body_params = netease_weapi_encrypt(params) if encrypted else params
        body = urllib.parse.urlencode(body_params).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = cookie
        elif not include_session_cookie:
            headers["Cookie"] = "; ".join(self._base_cookie_pairs())
        return self._request(f"{NETEASE_BASE_URL}{path}", headers=headers, data=body, method="POST")

    def _post_weapi_json(self, path: str, params: dict[str, Any], cookie: str, action: str) -> dict[str, Any]:
        if not cookie:
            raise QQMusicError("请先登录")
        csrf_token = cookie_value(cookie, "__csrf")
        request_path = path
        if csrf_token:
            separator = "&" if "?" in request_path else "?"
            request_path = f"{request_path}{separator}csrf_token={urllib.parse.quote(csrf_token)}"
        payload_params = dict(params)
        payload_params.setdefault("csrf_token", csrf_token)
        payload, _cookies = self._post_form_json(request_path, payload_params, cookie, encrypted=True)
        self._raise_if_failed(payload, action)
        return payload

    def _raise_if_failed(self, payload: dict[str, Any], action: str) -> None:
        code = payload.get("code")
        if code in (None, 200, 201):
            return
        message = payload.get("message") or payload.get("msg") or payload.get("errmsg") or f"{action}失败（错误码: {code}）"
        if str(code) in {"301", "-462"}:
            raise QQMusicError("网易云登录已失效，请重新登录")
        raise QQMusicError(str(message))

    def send_phone_captcha(self, phone: str, country_code: str = "86") -> None:
        self.ensure_anonymous_session()
        payload, _cookies = self._post_form_json(
            "/weapi/sms/captcha/sent",
            {
                "cellphone": phone,
                "ctcode": country_code,
            },
            encrypted=True,
        )
        if payload.get("code") != 200 or payload.get("data") is False:
            payload, _cookies = self._post_form_json(
                "/weapi/sms/captcha/sent",
                {
                    "cellphone": phone,
                    "ctcode": country_code,
                    "secrete": "music_middleuser_pclogin",
                },
                encrypted=True,
            )
        if payload.get("code") != 200 or payload.get("data") is False:
            raise QQMusicError(str(payload.get("message") or payload.get("msg") or "发送验证码失败"))

    def verify_phone_captcha(self, phone: str, captcha: str, country_code: str = "86") -> None:
        self.ensure_anonymous_session()
        payload, _cookies = self._post_form_json(
            "/weapi/sms/captcha/verify",
            {
                "cellphone": phone,
                "captcha": captcha,
                "ctcode": country_code,
            },
            encrypted=True,
        )
        if payload.get("code") != 200 or payload.get("data") is False:
            message = payload.get("message") or payload.get("msg") or "验证码校验失败"
            code = payload.get("code")
            raise QQMusicError(f"{message}（错误码: {code}）" if code is not None else str(message))

    def login_with_phone_captcha(self, phone: str, captcha: str, country_code: str = "86") -> QRLoginResult:
        self.ensure_anonymous_session()
        payload, set_cookies = self._login_with_phone_captcha_once(phone, captcha, country_code)
        if str(payload.get("code")) == "10004":
            self.verify_phone_captcha(phone, captcha, country_code)
            payload, set_cookies = self._login_with_phone_captcha_once(phone, captcha, country_code)
        if payload.get("code") != 200:
            message = payload.get("message") or payload.get("msg") or "网易云登录失败"
            code = payload.get("code")
            if str(code) == "10004":
                message = f"{message}。这是网易云服务端风控，请稍后再试，或先用官方网页登录后导入 Cookie。"
            raise QQMusicError(f"{message}（错误码: {code}）" if code is not None else str(message))
        return self._login_result_from_payload(payload, set_cookies)

    def _login_with_phone_captcha_once(self, phone: str, captcha: str, country_code: str) -> tuple[dict[str, Any], list[str]]:
        payload, set_cookies = self._post_form_json(
            "/weapi/w/login/cellphone",
            {
                "type": "1",
                "https": "true",
                "phone": phone,
                "countrycode": country_code,
                "captcha": captcha,
                "remember": "true",
            },
            encrypted=True,
        )
        return payload, set_cookies

    def _login_result_from_payload(self, payload: dict[str, Any], set_cookies: list[str]) -> QRLoginResult:
        profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
        account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        user_id = str(profile.get("userId") or account.get("id") or "")
        nickname = str(profile.get("nickname") or "")
        cookie_pairs_list = cookie_pairs(set_cookies)
        cookie_text = payload.get("cookie")
        if isinstance(cookie_text, str) and cookie_text:
            cookie_pairs_list = merge_cookie_pairs(cookie_pairs_list, [pair.strip() for pair in cookie_text.split(";") if "=" in pair])
        cookie = "; ".join(cookie_pairs_list)
        if not user_id:
            raise QQMusicError("登录成功但没有识别到网易云用户 ID")
        if not cookie:
            raise QQMusicError("登录成功但没有拿到网易云 Cookie")
        return QRLoginResult(qq_number=user_id, cookie=cookie, nickname=nickname)

    def login_with_cookie(self, cookie: str) -> QRLoginResult:
        cookie = cookie.strip()
        if not cookie:
            raise QQMusicError("Cookie 不能为空")
        payload = self._get_json("/api/nuser/account/get", cookie=cookie)
        if payload.get("code") != 200:
            message = payload.get("message") or payload.get("msg") or "网易云 Cookie 校验失败"
            raise QQMusicError(str(message))
        profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
        account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        user_id = str(profile.get("userId") or account.get("id") or "")
        nickname = str(profile.get("nickname") or "")
        if not user_id:
            raise QQMusicError("这个 Cookie 没有有效网易云登录态，请确认包含 MUSIC_U")
        return QRLoginResult(qq_number=user_id, cookie=cookie, nickname=nickname)

    def search(self, keyword: str, count: int = 30, page: int = 1, cookie: str = "") -> list[Song]:
        payload, _cookies = self._post_form_json(
            "/api/cloudsearch/pc",
            {
                "s": keyword,
                "type": 1,
                "offset": max(0, page - 1) * count,
                "total": "true" if page == 1 else "false",
                "limit": count,
            },
            cookie,
        )
        if payload.get("code") not in (None, 200):
            raise QQMusicError(str(payload.get("message") or payload.get("msg") or "搜索接口返回错误"))
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        items = result.get("songs") if isinstance(result.get("songs"), list) else []
        return [normalize_netease_song(item) for item in items if isinstance(item, dict)]

    def song_url(self, mid: str, quality: str = "320", cookie: str = "", media_mid: str = "") -> str:
        bitrates = {"128": 128000, "320": 320000, "flac": 999000}
        levels = {
            "128": ("standard", "128"),
            "320": ("exhigh", "standard", "128"),
            "flac": ("lossless", "exhigh", "standard", "128"),
        }
        song_id = media_mid or mid
        normalized_id = int(song_id) if str(song_id).isdigit() else song_id
        last_message = ""
        for level in levels.get(quality, levels["320"]):
            payload = self._get_json(
                "/api/song/enhance/player/url/v1",
                {"ids": json.dumps([normalized_id]), "level": level, "encodeType": "mp3"},
                cookie,
            )
            data = payload.get("data") if isinstance(payload.get("data"), list) else []
            item = data[0] if data and isinstance(data[0], dict) else {}
            url = str(item.get("url") or "")
            last_message = str(item.get("message") or payload.get("message") or payload.get("msg") or last_message or "")
            if url and self._stream_url_available(url):
                return url
        for br in (bitrates.get(quality, 320000), 320000, 128000):
            payload = self._get_json(
                "/api/song/enhance/player/url",
                {"ids": json.dumps([normalized_id]), "br": br},
                cookie,
            )
            data = payload.get("data") if isinstance(payload.get("data"), list) else []
            item = data[0] if data and isinstance(data[0], dict) else {}
            url = str(item.get("url") or "")
            last_message = str(item.get("message") or payload.get("message") or payload.get("msg") or last_message or "")
            if url and self._stream_url_available(url):
                return url
        if last_message:
            raise QQMusicError(f"网易云没有返回可用播放链接：{last_message}")
        raise QQMusicError("网易云没有返回可播放链接，可能是会员、版权、地区或登录限制")

    def _stream_url_available(self, url: str) -> bool:
        headers = {
            "User-Agent": NETEASE_WEB_UA,
            "Referer": "https://music.163.com/",
            "Range": "bytes=0-0",
        }
        for method in ("HEAD", "GET"):
            try:
                request = urllib.request.Request(url, method=method, headers=headers)
                with urllib.request.urlopen(request, timeout=min(5, self.timeout)) as response:
                    content_type = response.headers.get("Content-Type", "")
                    if 200 <= response.status < 400 and (not content_type or "text/html" not in content_type.lower()):
                        return True
            except urllib.error.HTTPError as exc:
                if method == "HEAD" and exc.code in {403, 405, 501}:
                    continue
                return False
            except (urllib.error.URLError, TimeoutError, OSError):
                return False
        return False

    def lyric(self, mid: str, cookie: str = "") -> str:
        payload = self._get_json(
            "/api/song/lyric",
            {"os": "pc", "id": mid, "lv": -1, "kv": -1, "tv": -1},
            cookie,
        )
        lrc = payload.get("lrc") if isinstance(payload.get("lrc"), dict) else {}
        trans = payload.get("tlyric") if isinstance(payload.get("tlyric"), dict) else {}
        lyric = str(lrc.get("lyric") or "")
        translated = str(trans.get("lyric") or "")
        if translated:
            return f"{lyric}\n\n--- 翻译 ---\n{translated}".strip()
        return lyric.strip()

    def playlist_songs_page(
        self,
        playlist_id: str,
        cookie: str = "",
        begin: int = 0,
        count: int = PLAYLIST_BACKGROUND_PAGE_SIZE,
    ) -> tuple[str, list[Song], int]:
        fetch_count = max(1, begin + count)
        payload = self._get_json(
            "/api/v6/playlist/detail",
            {"id": playlist_id, "n": fetch_count, "s": 0},
            cookie,
        )
        if payload.get("code") != 200:
            raise QQMusicError(str(payload.get("message") or payload.get("msg") or "歌单详情接口返回错误"))
        playlist = payload.get("playlist") if isinstance(payload.get("playlist"), dict) else {}
        name = str(playlist.get("name") or f"歌单 {playlist_id}")
        tracks = playlist.get("tracks") if isinstance(playlist.get("tracks"), list) else []
        total = playlist.get("trackCount") or len(playlist.get("trackIds") or []) or len(tracks)
        try:
            total_count = int(total)
        except (TypeError, ValueError):
            total_count = len(tracks)
        songs = [normalize_netease_song(item) for item in tracks[begin : begin + count] if isinstance(item, dict)]
        return name, songs, max(total_count, begin + len(songs))

    def playlist_songs(self, playlist_id: str, cookie: str = "") -> tuple[str, list[Song]]:
        name, results, total = self.playlist_songs_page(playlist_id, cookie, 0, PLAYLIST_BACKGROUND_PAGE_SIZE)
        loaded = len(results)
        while True:
            page_name, page_results, page_total = self.playlist_songs_page(playlist_id, cookie, loaded, PLAYLIST_BACKGROUND_PAGE_SIZE)
            if page_name:
                name = page_name
            if page_total > total:
                total = page_total
            if not page_results:
                break
            results.extend(page_results)
            loaded += len(page_results)
            if len(page_results) < PLAYLIST_BACKGROUND_PAGE_SIZE:
                break
            if loaded >= total:
                total = loaded + PLAYLIST_BACKGROUND_PAGE_SIZE
        return name, results

    def user_playlists(self, user_id: str, cookie: str = "") -> list[Playlist]:
        payload = self._get_json(
            "/api/user/playlist/",
            {"uid": user_id, "offset": 0, "limit": 1000},
            cookie,
        )
        if payload.get("code") != 200:
            raise QQMusicError(str(payload.get("message") or payload.get("msg") or "用户歌单接口返回错误"))
        items = payload.get("playlist") if isinstance(payload.get("playlist"), list) else []
        return [normalize_netease_playlist(item) for item in items if isinstance(item, dict)]

    def add_song_to_playlist(
        self,
        song_id: str,
        _song_type: int,
        playlist_dirid: str,
        cookie: str,
        _song_mid: str = "",
        _playlist_id: str = "",
    ) -> None:
        if not song_id:
            raise QQMusicError("当前歌曲缺少网易云歌曲 ID")
        if not playlist_dirid:
            raise QQMusicError("目标歌单缺少网易云歌单 ID")
        self._post_weapi_json(
            "/weapi/playlist/manipulate/tracks",
            {
                "op": "add",
                "pid": playlist_dirid,
                "trackIds": json.dumps([int(song_id) if str(song_id).isdigit() else song_id]),
                "imme": "true",
            },
            cookie,
            "添加歌曲",
        )

    def remove_song_from_playlist(self, song_id: str, _song_type: int, playlist_dirid: str, cookie: str) -> None:
        if not song_id:
            raise QQMusicError("当前歌曲缺少网易云歌曲 ID")
        if not playlist_dirid:
            raise QQMusicError("当前歌单缺少网易云歌单 ID")
        self._post_weapi_json(
            "/weapi/playlist/manipulate/tracks",
            {
                "op": "del",
                "pid": playlist_dirid,
                "trackIds": json.dumps([int(song_id) if str(song_id).isdigit() else song_id]),
                "imme": "true",
            },
            cookie,
            "移除歌曲",
        )

    def create_playlist(self, name: str, cookie: str) -> str:
        payload = self._post_weapi_json(
            "/weapi/playlist/create",
            {
                "name": name,
                "privacy": 0,
                "type": "NORMAL",
            },
            cookie,
            "创建歌单",
        )
        playlist = payload.get("playlist") if isinstance(payload.get("playlist"), dict) else {}
        return str(playlist.get("id") or payload.get("id") or "")

    def delete_playlist(self, playlist_dirid: str, cookie: str) -> None:
        if not playlist_dirid:
            raise QQMusicError("当前歌单缺少网易云歌单 ID")
        self._post_weapi_json(
            "/weapi/playlist/remove",
            {"ids": f"[{playlist_dirid}]"},
            cookie,
            "删除歌单",
        )

    def rename_playlist(self, playlist_dirid: str, new_name: str, cookie: str) -> None:
        if not playlist_dirid:
            raise QQMusicError("当前歌单缺少网易云歌单 ID")
        self._post_weapi_json(
            "/weapi/playlist/update/name",
            {
                "id": playlist_dirid,
                "name": new_name,
            },
            cookie,
            "重命名歌单",
        )


def normalize_song(item: dict[str, Any]) -> Song:
    title = str(item.get("title") or item.get("songname") or item.get("name") or "未知歌曲")
    mid = str(item.get("mid") or item.get("songmid") or item.get("strMediaMid") or "")
    song_id = str(item.get("id") or item.get("songid") or "")
    try:
        song_type = int(item.get("type") if item.get("type") is not None else item.get("songtype") or 0)
    except (TypeError, ValueError):
        song_type = 0
    file_value = item.get("file") if isinstance(item.get("file"), dict) else {}
    media_mid = str(
        item.get("media_mid")
        or item.get("strMediaMid")
        or item.get("mediaMid")
        or file_value.get("media_mid")
        or file_value.get("strMediaMid")
        or mid
    )

    singers_value = item.get("singer") or item.get("singers") or []
    if isinstance(singers_value, list):
        singers = "/".join(str(s.get("name", "")) for s in singers_value if isinstance(s, dict))
    elif isinstance(singers_value, dict):
        singers = str(singers_value.get("name") or "")
    else:
        singers = str(singers_value or "")

    album_value = item.get("album") or item.get("albumname") or ""
    if isinstance(album_value, dict):
        album = str(album_value.get("name") or album_value.get("title") or "")
    else:
        album = str(album_value or "")

    duration = item.get("interval") or item.get("duration")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    return Song(
        title=title,
        mid=mid,
        song_id=song_id,
        song_type=song_type,
        singers=singers,
        album=album,
        duration=duration,
        media_mid=media_mid,
        raw=item,
    )


def normalize_playlist(item: dict[str, Any]) -> Playlist:
    playlist_id = str(
        item.get("tid")
        or item.get("dissid")
        or item.get("id")
        or ""
    )
    dirid = str(item.get("dirid") or item.get("tid") or item.get("dissid") or item.get("id") or "")
    name = str(
        item.get("diss_name")
        or item.get("dissname")
        or item.get("dirname")
        or item.get("name")
        or "未命名歌单"
    )
    count_value = item.get("song_cnt") or item.get("songnum") or item.get("total")
    try:
        song_count = int(count_value) if count_value is not None else None
    except (TypeError, ValueError):
        song_count = None
    cover = str(item.get("diss_cover") or item.get("logo") or item.get("cover") or "")
    return Playlist(id=playlist_id, name=name, dirid=dirid, song_count=song_count, cover=cover, raw=item)


def normalize_netease_song(item: dict[str, Any]) -> Song:
    song_id = str(item.get("id") or "")
    title = str(item.get("name") or item.get("title") or "未知歌曲")
    artists_value = item.get("ar") or item.get("artists") or []
    if isinstance(artists_value, list):
        singers = "/".join(str(artist.get("name", "")) for artist in artists_value if isinstance(artist, dict))
    else:
        singers = ""
    album_value = item.get("al") or item.get("album") or {}
    if isinstance(album_value, dict):
        album = str(album_value.get("name") or "")
    else:
        album = str(album_value or "")
    duration_value = item.get("dt") or item.get("duration")
    try:
        duration = int(duration_value) // 1000 if duration_value is not None else None
    except (TypeError, ValueError):
        duration = None
    return Song(
        title=title,
        mid=song_id,
        song_id=song_id,
        singers=singers,
        album=album,
        duration=duration,
        media_mid=song_id,
        raw=item,
    )


def normalize_netease_playlist(item: dict[str, Any]) -> Playlist:
    playlist_id = str(item.get("id") or "")
    name = str(item.get("name") or "未命名歌单")
    count_value = item.get("trackCount") or item.get("song_count")
    try:
        song_count = int(count_value) if count_value is not None else None
    except (TypeError, ValueError):
        song_count = None
    cover = str(item.get("coverImgUrl") or item.get("cover") or "")
    return Playlist(id=playlist_id, name=name, dirid=playlist_id, song_count=song_count, cover=cover, raw=item)


def auth_file_path(platform: str | None = None) -> Path:
    platform = normalize_platform(platform or load_settings().get("platform", "qqmusic"))
    if platform == "netease":
        return Path(os.environ.get("NETEASE_AUTH_FILE", DEFAULT_NETEASE_AUTH_FILE))
    return Path(os.environ.get("QQMUSIC_AUTH_FILE", DEFAULT_AUTH_FILE))


def settings_file_path() -> Path:
    return Path(os.environ.get("QQMUSIC_SETTINGS_FILE", DEFAULT_SETTINGS_FILE))


def load_settings() -> dict[str, Any]:
    path = settings_file_path()
    settings = dict(DEFAULT_SETTINGS)
    if not path.exists():
        return settings
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return settings
    if isinstance(data, dict):
        settings.update({key: value for key, value in data.items() if key in DEFAULT_SETTINGS})
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    path = settings_file_path()
    data = {key: settings.get(key, value) for key, value in DEFAULT_SETTINGS.items()}
    try:
        with path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    except OSError as exc:
        raise QQMusicError(f"保存设置失败: {exc}") from exc


def downloads_index_path(download_dir: str) -> Path:
    return Path(download_dir).expanduser() / ".qqmusic_downloads.json"


def load_download_index(download_dir: str) -> dict[str, Any]:
    path = downloads_index_path(download_dir)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_download_index(download_dir: str, index: dict[str, Any]) -> None:
    path = downloads_index_path(download_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(index, file, ensure_ascii=False, indent=2)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or "未知歌曲"


def guess_audio_extension(url: str, quality: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    suffix = Path(path).suffix
    if suffix in {".mp3", ".flac", ".m4a", ".ogg", ".wav"}:
        return suffix
    return ".flac" if quality == "flac" else ".mp3"


def downloaded_songs(download_dir: str) -> list[Song]:
    root = Path(download_dir).expanduser()
    if not root.exists():
        return []
    index = load_download_index(str(root))
    songs: list[Song] = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in {".mp3", ".flac", ".m4a", ".ogg", ".wav"}:
            continue
        meta = index.get(path.name, {}) if isinstance(index.get(path.name), dict) else {}
        songs.append(
            Song(
                title=str(meta.get("title") or path.stem),
                mid=str(meta.get("mid") or ""),
                song_id=str(meta.get("song_id") or ""),
                song_type=int(meta["song_type"]) if str(meta.get("song_type", "")).isdigit() else 0,
                singers=str(meta.get("singers") or ""),
                album=str(meta.get("album") or ""),
                duration=int(meta["duration"]) if str(meta.get("duration", "")).isdigit() else None,
                media_mid=str(meta.get("media_mid") or ""),
                local_path=str(path),
            )
        )
    return songs


def has_downloaded_songs(download_dir: str) -> bool:
    return bool(downloaded_songs(download_dir))


def is_builtin_playlist(playlist: Playlist) -> bool:
    return playlist.id == "__downloads__" or playlist.dirid == "201" or playlist.name == "我喜欢"


def is_addable_playlist(playlist: Playlist) -> bool:
    return playlist.id != "__downloads__"


def is_netease_editable_playlist(playlist: Playlist, user_id: str = "") -> bool:
    if playlist.id == "__downloads__":
        return False
    raw = playlist.raw if isinstance(playlist.raw, dict) else {}
    if raw.get("subscribed") is True:
        return False
    try:
        special_type = int(raw.get("specialType") or 0)
    except (TypeError, ValueError):
        special_type = 0
    if special_type == 5:
        return False
    if user_id:
        creator = raw.get("creator") if isinstance(raw.get("creator"), dict) else {}
        creator_id = str(raw.get("userId") or creator.get("userId") or "")
        if creator_id and creator_id != user_id:
            return False
    return bool(playlist.id)


def is_netease_addable_playlist(playlist: Playlist, user_id: str = "") -> bool:
    if playlist.id == "__downloads__":
        return False
    raw = playlist.raw if isinstance(playlist.raw, dict) else {}
    if raw.get("subscribed") is True:
        return False
    if user_id:
        creator = raw.get("creator") if isinstance(raw.get("creator"), dict) else {}
        creator_id = str(raw.get("userId") or creator.get("userId") or "")
        if creator_id and creator_id != user_id:
            return False
    return bool(playlist.id)


def is_netease_song_removable_playlist(playlist: Playlist, user_id: str = "") -> bool:
    return is_netease_addable_playlist(playlist, user_id)


def load_auth(platform: str | None = None) -> dict[str, str]:
    platform = normalize_platform(platform or load_settings().get("platform", "qqmusic"))
    path = auth_file_path(platform)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise QQMusicError(f"读取登录信息失败: {exc}") from exc
    if not isinstance(data, dict):
        return {}
    auth = {str(key): str(value) for key, value in data.items() if value is not None}
    if platform == "qqmusic":
        cookie_account = qq_number_from_cookie(auth.get("cookie", ""))
        if cookie_account:
            auth["qq_number"] = cookie_account
    auth["platform"] = platform
    return auth


def save_auth(qq_number: str, cookie: str, platform: str | None = None, nickname: str = "") -> None:
    platform = normalize_platform(platform or load_settings().get("platform", "qqmusic"))
    path = auth_file_path(platform)
    data = {
        "qq_number": qq_number.strip(),
        "cookie": cookie.strip(),
        "platform": platform,
        "updated_at": str(int(time.time())),
    }
    clean_nickname = nickname.strip()
    if clean_nickname and clean_nickname not in {"微信登录"}:
        data["nickname"] = clean_nickname
    try:
        with path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise QQMusicError(f"保存登录信息失败: {exc}") from exc


def clear_auth(platform: str | None = None) -> None:
    path = auth_file_path(platform)
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        raise QQMusicError(f"删除登录信息失败: {exc}") from exc


def qq_number_from_cookie(cookie: str) -> str:
    wxuin = cookie_value(cookie, "wxuin")
    if wxuin:
        digits = re.sub(r"\D", "", wxuin)
        if digits:
            return digits
    for key in ("uin", "wxuin"):
        match = re.search(rf"(?:^|;\s*){key}=([^;]+)", cookie)
        if match:
            digits = re.sub(r"\D", "", match.group(1))
            if digits:
                return digits
    return ""


class Player:
    def __init__(self, command: str | None = None):
        explicit_command = command or os.environ.get("QQMUSIC_PLAYER")
        self.command = explicit_command
        self.backend = "external" if explicit_command else "browser"
        self.process: subprocess.Popen[str] | None = None
        self._vlc_instance: Any | None = None
        self._vlc_player: Any | None = None
        self._vlc_media: Any | None = None

        if explicit_command:
            return

        if self._init_python_vlc():
            self.backend = "python-vlc"
            return

        self.command = find_player()
        if self.command:
            self.backend = "external"

    def available(self) -> bool:
        return self.backend == "python-vlc" or bool(self.command)

    @property
    def display_name(self) -> str:
        if self.backend == "python-vlc":
            return "python-vlc"
        if self.command:
            return os.path.basename(self.command)
        return "浏览器"

    def _init_python_vlc(self) -> bool:
        try:
            vlc_module = __import__("vlc")
            self._vlc_instance = vlc_module.Instance("--no-video", "--quiet")
            self._vlc_player = self._vlc_instance.media_player_new()
        except Exception:  # noqa: BLE001 - optional backend; external player remains available.
            self._vlc_instance = None
            self._vlc_player = None
            return False
        return True

    def play(self, url: str, start_position_ms: int = 0) -> None:
        if self.backend == "python-vlc":
            self.stop()
            if not self._vlc_instance or not self._vlc_player:
                raise QQMusicError("python-vlc 初始化失败")
            self._vlc_media = self._vlc_instance.media_new(url)
            self._vlc_player.set_media(self._vlc_media)
            if self._vlc_player.play() == -1:
                raise QQMusicError("python-vlc 播放失败")
            return

        if not self.command:
            webbrowser.open(url)
            return

        self.stop()
        args = build_player_args(self.command, url, start_position_ms)
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def pause(self) -> bool:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            self._vlc_player.pause()
            return True
        self.stop()
        return False

    def resume(self) -> bool:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            self._vlc_player.play()
            return True
        return False

    def stop(self) -> None:
        if self._vlc_player is not None:
            self._vlc_player.stop()
            self._vlc_media = None

        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def has_ended(self) -> bool:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            state = str(self._vlc_player.get_state())
            return state.endswith(".Ended") or state.endswith(".Error")
        if self.process is not None:
            return self.process.poll() is not None
        return False

    def position_ms(self) -> int | None:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            value = self._vlc_player.get_time()
            return int(value) if value is not None and value >= 0 else None
        return None

    def duration_ms(self) -> int | None:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            value = self._vlc_player.get_length()
            return int(value) if value is not None and value > 0 else None
        return None

    def buffered_ms(self) -> int | None:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            duration = self.duration_ms()
            position = self.position_ms()
            is_seekable = False
            try:
                is_seekable = bool(self._vlc_player.is_seekable())
            except Exception:
                is_seekable = False
            if is_seekable and duration:
                return duration
            return position if duration and position and position >= duration else None
        return None

    def seek_ms(self, position_ms: int) -> bool:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            target = max(0, int(position_ms))
            self._vlc_player.set_time(target)
            duration = self.duration_ms()
            if duration:
                try:
                    self._vlc_player.set_position(max(0.0, min(1.0, target / duration)))
                except Exception:
                    pass
            return True
        return False


def find_player() -> str | None:
    for name in ("mpv", "vlc", "ffplay", "xdg-open"):
        path = shutil.which(name)
        if path:
            return path
    return None


def build_player_args(command: str, url: str, start_position_ms: int = 0) -> list[str]:
    name = os.path.basename(command)
    start_seconds = max(0.0, start_position_ms / 1000)
    if name == "mpv":
        args = [command, "--force-window=yes"]
        if start_seconds:
            args.append(f"--start={start_seconds:.3f}")
        args.append(url)
        return args
    if name == "vlc":
        args = [command, "--started-from-file"]
        if start_seconds:
            args.append(f"--start-time={start_seconds:.3f}")
        args.append(url)
        return args
    if name == "ffplay":
        args = [command, "-autoexit", "-nodisp"]
        if start_seconds:
            args.extend(["-ss", f"{start_seconds:.3f}"])
        args.append(url)
        return args
    return [command, url]


def build_music_api(platform: str, api_base: str = DEFAULT_API_BASE, timeout: int = DEFAULT_TIMEOUT) -> Any:
    normalized = normalize_platform(platform)
    if normalized == "netease":
        return NeteaseMusicAPI(timeout=timeout)
    return QQMusicAPI(base_url=api_base, timeout=timeout)


def run_cli(args: argparse.Namespace) -> int:
    platform = normalize_platform(args.platform)
    api = build_music_api(platform, args.api_base, args.timeout)

    if args.command == "login":
        if platform == "netease":
            if args.send_captcha:
                if not args.phone:
                    raise QQMusicError("请用 --phone 传入手机号")
                api.send_phone_captcha(args.phone)
                print("验证码已发送")
                return 0
            if not args.phone or not args.captcha:
                raise QQMusicError("网易云登录请传入 --phone 和 --captcha，或先用 --send-captcha 发送验证码")
            result = api.login_with_phone_captcha(args.phone, args.captcha)
            save_auth(result.qq_number, result.cookie, platform, result.nickname)
            print(f"登录成功: {result.nickname or result.qq_number}")
            return 0

        if args.cookie:
            qq_number = args.qq_number or qq_number_from_cookie(args.cookie)
            if not qq_number:
                raise QQMusicError("没有 QQ 号。请传入 QQ 号，或提供包含 uin/wxuin 的 Cookie")
            save_auth(qq_number, args.cookie, platform)
            print(f"已保存登录信息: {qq_number}")
            return 0

        session = api.start_qr_login()
        qr_path = Path.cwd() / "music_login_qr.png"
        qr_path.write_bytes(session.image)
        print(f"请用手机 QQ 扫描二维码: {qr_path}", flush=True)
        print("扫码后在手机上确认登录，程序会自动继续。", flush=True)
        deadline = time.time() + args.timeout_seconds
        while time.time() < deadline:
            state, result = api.poll_qr_login(session)
            if state == "waiting":
                print("等待扫码...", flush=True)
            elif state == "confirming":
                print("已扫码，等待手机确认...", flush=True)
            elif state == "expired":
                raise QQMusicError("二维码已过期，请重新运行 login")
            elif state == "done" and result:
                save_auth(result.qq_number, result.cookie, platform, result.nickname)
                print(f"登录成功: {result.nickname or result.qq_number}")
                return 0
            time.sleep(2)
        raise QQMusicError("登录超时，请重新运行 login")
        return 0

    if args.command == "logout":
        clear_auth(platform)
        print("已退出登录")
        return 0

    if args.command == "sync-playlists":
        auth = load_auth(platform)
        qq_number = args.qq_number or auth.get("qq_number", "")
        if not qq_number:
            raise QQMusicError("还没有登录。先运行 login，或给 sync-playlists 传入账号 ID")
        playlists = api.user_playlists(qq_number, auth.get("cookie", ""))
        for index, playlist in enumerate(playlists, 1):
            print(f"{index:02d}. {playlist.display}")
            print(f"    id: {playlist.id}")
        return 0

    if args.command == "search":
        auth = load_auth(platform)
        songs = api.search(args.keyword, count=args.count, cookie=auth.get("cookie", ""))
        for index, song in enumerate(songs, 1):
            print(f"{index:02d}. {song.display}")
            print(f"    mid: {song.mid}")
        return 0

    if args.command == "play":
        auth = load_auth(platform)
        songs = api.search(args.keyword, count=args.count, cookie=auth.get("cookie", ""))
        if not songs:
            raise QQMusicError("没有搜索到歌曲")
        song = songs[min(args.index - 1, len(songs) - 1)]
        url = api.song_url(song.mid, args.quality, auth.get("cookie", ""), song.media_mid)
        print(f"正在播放: {song.display}")
        player = Player(args.player)
        player.play(url)
        if args.wait:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                player.stop()
        return 0

    if args.command == "lyric":
        auth = load_auth(platform)
        songs = api.search(args.keyword, count=args.count, cookie=auth.get("cookie", ""))
        if not songs:
            raise QQMusicError("没有搜索到歌曲")
        song = songs[min(args.index - 1, len(songs) - 1)]
        print(f"{song.display}\n")
        print(api.lyric(song.mid, auth.get("cookie", "")))
        return 0

    return 1


def run_gui(api_base: str, timeout: int, player_command: str | None) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, simpledialog, ttk
    except ImportError as exc:
        raise QQMusicError("当前 Python 没有安装 Tkinter，请用命令行模式运行") from exc

    settings = load_settings()
    current_platform = normalize_platform(str(settings.get("platform", "qqmusic")))
    api = build_music_api(current_platform, api_base, timeout)
    player = Player(player_command)
    auth = load_auth(current_platform)

    root = tk.Tk()
    root.title(f"{platform_display_name(current_platform)} music_for_all_system(developed_by_Linux_Mint)")
    root.geometry("1180x760")
    root.minsize(980, 600)
    root.configure(bg="#e9eef4")

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("App.TFrame", background="#e9eef4")
    style.configure("Panel.TFrame", background="#ffffff", relief=tk.FLAT)
    style.configure("Toolbar.TFrame", background="#dfe7ef")
    style.configure("Player.TFrame", background="#101827")
    style.configure("Title.TLabel", background="#ffffff", foreground="#151a22", font=("Sans", 12, "bold"))
    style.configure("Muted.TLabel", background="#ffffff", foreground="#68707a")
    style.configure("Account.TLabel", background="#ffffff", foreground="#0f766e", font=("Sans", 10, "bold"))
    style.configure("PlayerTitle.TLabel", background="#101827", foreground="#ffffff", font=("Sans", 11, "bold"))
    style.configure("PlayerMeta.TLabel", background="#101827", foreground="#c8d2df")
    style.configure("PlayerTime.TLabel", background="#101827", foreground="#93a4b8", font=("Sans", 9))
    style.configure("Player.Horizontal.TScale", background="#101827", troughcolor="#273244")
    style.configure("Accent.TButton", padding=(12, 7), background="#f8fafc")
    style.configure("Player.TButton", padding=(12, 6), background="#273244", foreground="#ffffff")
    style.map("Player.TButton", background=[("active", "#334155"), ("disabled", "#273244")])

    keyword_var = tk.StringVar()
    platform_var = tk.StringVar(value=platform_display_name(current_platform))
    quality_var = tk.StringVar(value=str(settings.get("quality", "320")))
    status_var = tk.StringVar(value=f"平台: {platform_display_name(current_platform)} | 播放器: {player.display_name}")
    account_var = tk.StringVar()
    player_title_var = tk.StringVar(value="未播放")
    player_meta_var = tk.StringVar(value="选择歌曲后点击播放")
    queue_var = tk.StringVar(value="0 / 0")
    progress_var = tk.DoubleVar(value=0.0)
    progress_text_var = tk.StringVar(value="0:00")
    duration_text_var = tk.StringVar(value="0:00")
    play_mode_var = tk.StringVar(value=str(settings.get("play_mode", "顺序播放")))
    songs: list[Song] = []
    playlists: list[Playlist] = []
    current_playlist: Playlist | None = None
    remote_playlists: list[Playlist] = []
    current_lyric_lines: list[LyricLine] = []
    current_lyric_index = -1
    current_song_index = -1
    playback_token = 0
    playlist_load_token = 0
    user_stopped = True
    playback_started_at = 0.0
    paused_position_ms = 0
    progress_dragging = False
    nickname_fetching = False

    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)

    top = ttk.Frame(root, padding=(12, 10), style="Toolbar.TFrame")
    top.grid(row=0, column=0, sticky="ew")
    top.columnconfigure(0, weight=1)

    entry = ttk.Entry(top, textvariable=keyword_var)
    entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
    entry.focus_set()

    platform_box = ttk.Combobox(
        top,
        textvariable=platform_var,
        values=tuple(PLATFORMS.values()),
        width=12,
        state="readonly",
    )
    platform_box.grid(row=0, column=1, padx=(0, 8))

    search_button = ttk.Button(top, text="搜索", style="Accent.TButton")
    search_button.grid(row=0, column=2, padx=(0, 8))

    settings_button = ttk.Button(top, text="设置", style="Accent.TButton")
    settings_button.grid(row=0, column=3)

    body = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
    body.grid(row=1, column=0, sticky="nsew", padx=12, pady=(12, 10))

    playlist_panel = ttk.Frame(body, padding=12, style="Panel.TFrame")
    playlist_panel.rowconfigure(3, weight=1)
    playlist_panel.columnconfigure(0, weight=1)
    body.add(playlist_panel, weight=1)

    playlist_title = ttk.Label(playlist_panel, text="我的歌单", style="Title.TLabel")
    playlist_title.grid(row=0, column=0, sticky="ew")

    account_label = ttk.Label(playlist_panel, textvariable=account_var, anchor="w", style="Account.TLabel")
    account_label.grid(row=1, column=0, sticky="ew", pady=(2, 10))

    playlist_actions = ttk.Frame(playlist_panel, style="Panel.TFrame")
    playlist_actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 10))
    new_playlist_button = ttk.Button(playlist_actions, text="新建", style="Accent.TButton")
    new_playlist_button.grid(row=0, column=0, padx=(0, 6))
    rename_playlist_button = ttk.Button(playlist_actions, text="重命名", style="Accent.TButton")
    rename_playlist_button.grid(row=0, column=1, padx=(0, 6))
    delete_playlist_button = ttk.Button(playlist_actions, text="删除", style="Accent.TButton")
    delete_playlist_button.grid(row=0, column=2)

    playlist_box = tk.Listbox(
        playlist_panel,
        activestyle="none",
        exportselection=False,
        borderwidth=0,
        highlightthickness=1,
        highlightbackground="#d7dde5",
        selectbackground="#dbeafe",
        selectforeground="#111827",
        font=("Sans", 11),
    )
    playlist_box.grid(row=3, column=0, sticky="nsew")
    playlist_scroll = ttk.Scrollbar(playlist_panel, orient=tk.VERTICAL, command=playlist_box.yview)
    playlist_scroll.grid(row=3, column=1, sticky="ns")
    playlist_box.configure(yscrollcommand=playlist_scroll.set)

    left = ttk.Frame(body, padding=12, style="Panel.TFrame")
    left.rowconfigure(2, weight=1)
    left.columnconfigure(0, weight=1)
    body.add(left, weight=3)

    song_title = ttk.Label(left, text="歌曲队列", style="Title.TLabel")
    song_title.grid(row=0, column=0, sticky="ew", pady=(0, 8))

    song_actions = ttk.Frame(left, style="Panel.TFrame")
    song_actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
    add_song_button = ttk.Button(song_actions, text="加入歌单", style="Accent.TButton")
    add_song_button.grid(row=0, column=0, padx=(0, 6))
    remove_song_button = ttk.Button(song_actions, text="从歌单移除", style="Accent.TButton")
    remove_song_button.grid(row=0, column=1, padx=(0, 6))
    download_song_button = ttk.Button(song_actions, text="下载", style="Accent.TButton")
    download_song_button.grid(row=0, column=2)

    listbox = tk.Listbox(
        left,
        activestyle="none",
        exportselection=False,
        borderwidth=0,
        highlightthickness=1,
        highlightbackground="#d7dde5",
        selectbackground="#e0f2fe",
        selectforeground="#111827",
        font=("Sans", int(settings.get("queue_font_size", 11))),
    )
    listbox.grid(row=2, column=0, sticky="nsew")
    list_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=listbox.yview)
    list_scroll.grid(row=2, column=1, sticky="ns")
    listbox.configure(yscrollcommand=list_scroll.set)

    right = ttk.Frame(body, padding=12, style="Panel.TFrame")
    right.rowconfigure(2, weight=1)
    right.columnconfigure(0, weight=1)
    body.add(right, weight=2)

    info_var = tk.StringVar(value="搜索歌曲后选择一项")
    info_title = ttk.Label(right, text="歌曲信息", style="Title.TLabel")
    info_title.grid(row=0, column=0, sticky="ew")
    info = ttk.Label(right, textvariable=info_var, justify=tk.LEFT, anchor="w", wraplength=340, style="Muted.TLabel")
    info.grid(row=1, column=0, sticky="ew", pady=(6, 10))

    lyric_text = tk.Text(
        right,
        wrap=tk.WORD,
        height=10,
        borderwidth=0,
        highlightthickness=1,
        highlightbackground="#d7dde5",
        background="#fbfdff",
        foreground="#111827",
        font=("Sans", 11),
        padx=10,
        pady=10,
    )
    lyric_text.grid(row=2, column=0, sticky="nsew")
    lyric_scroll = ttk.Scrollbar(right, orient=tk.VERTICAL, command=lyric_text.yview)
    lyric_scroll.grid(row=2, column=1, sticky="ns")
    lyric_text.configure(yscrollcommand=lyric_scroll.set)
    lyric_text.tag_configure("active_lyric", background="#dbeafe", foreground="#0f172a")

    player_bar = ttk.Frame(root, padding=(14, 10), style="Player.TFrame")
    player_bar.grid(row=2, column=0, sticky="ew")
    player_bar.columnconfigure(1, weight=1)

    control_group = ttk.Frame(player_bar, style="Player.TFrame")
    control_group.grid(row=0, column=0, sticky="w", padx=(0, 14))

    prev_button = ttk.Button(control_group, text="上一首", style="Player.TButton")
    prev_button.grid(row=0, column=0, padx=(0, 6))
    player_play_button = ttk.Button(control_group, text="播放", style="Player.TButton")
    player_play_button.grid(row=0, column=1, padx=(0, 6))
    player_stop_button = ttk.Button(control_group, text="暂停", style="Player.TButton")
    player_stop_button.grid(row=0, column=2, padx=(0, 6))
    next_button = ttk.Button(control_group, text="下一首", style="Player.TButton")
    next_button.grid(row=0, column=3)

    mode_box = ttk.Combobox(
        control_group,
        textvariable=play_mode_var,
        values=("顺序播放", "随机播放", "单曲循环"),
        width=10,
        state="readonly",
    )
    mode_box.grid(row=0, column=4, padx=(8, 0))

    def save_play_mode(_event: object | None = None) -> None:
        settings["play_mode"] = play_mode_var.get()
        try:
            save_settings(settings)
        except QQMusicError as exc:
            status_var.set(str(exc))

    mode_box.bind("<<ComboboxSelected>>", save_play_mode)

    now_playing = ttk.Frame(player_bar, style="Player.TFrame")
    now_playing.grid(row=0, column=1, sticky="ew")
    now_playing.columnconfigure(0, weight=1)
    player_title = ttk.Label(now_playing, textvariable=player_title_var, style="PlayerTitle.TLabel", anchor="w")
    player_title.grid(row=0, column=0, sticky="ew")
    player_meta = ttk.Label(now_playing, textvariable=player_meta_var, style="PlayerMeta.TLabel", anchor="w")
    player_meta.grid(row=1, column=0, sticky="ew", pady=(3, 0))

    progress_row = ttk.Frame(now_playing, style="Player.TFrame")
    progress_row.grid(row=2, column=0, sticky="ew", pady=(7, 0))
    progress_row.columnconfigure(1, weight=1)
    elapsed_label = ttk.Label(progress_row, textvariable=progress_text_var, style="PlayerTime.TLabel", width=5, anchor="w")
    elapsed_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
    progress_scale = ttk.Scale(
        progress_row,
        from_=0,
        to=100,
        orient=tk.HORIZONTAL,
        variable=progress_var,
        style="Player.Horizontal.TScale",
    )
    progress_scale.grid(row=0, column=1, sticky="ew")
    duration_label = ttk.Label(progress_row, textvariable=duration_text_var, style="PlayerTime.TLabel", width=5, anchor="e")
    duration_label.grid(row=0, column=2, sticky="e", padx=(8, 0))

    queue_label = ttk.Label(player_bar, textvariable=queue_var, style="PlayerMeta.TLabel", anchor="e", width=10)
    queue_label.grid(row=0, column=2, sticky="e", padx=(14, 0))

    def set_busy(is_busy: bool, message: str | None = None) -> None:
        state = tk.DISABLED if is_busy else tk.NORMAL
        search_button.configure(state=state)
        settings_button.configure(state=state)
        platform_box.configure(state=tk.DISABLED if is_busy else "readonly")
        write_state = state if auth.get("qq_number") and supports_playlist_write() else tk.DISABLED
        new_playlist_button.configure(state=write_state)
        rename_playlist_button.configure(state=write_state)
        delete_playlist_button.configure(state=write_state)
        add_song_button.configure(state=write_state)
        remove_song_button.configure(state=state)
        download_song_button.configure(state=state)
        if message:
            status_var.set(message)

    def supports_playlist_write() -> bool:
        return current_platform in {"qqmusic", "netease"}

    def playlist_can_write(playlist: Playlist | None) -> bool:
        if not playlist:
            return False
        if current_platform == "netease":
            return is_netease_editable_playlist(playlist, auth.get("qq_number", ""))
        return not is_builtin_playlist(playlist)

    def playlist_can_add(playlist: Playlist | None) -> bool:
        if not playlist:
            return False
        if current_platform == "netease":
            return is_netease_addable_playlist(playlist, auth.get("qq_number", ""))
        return is_addable_playlist(playlist)

    def playlist_can_remove_song(playlist: Playlist | None) -> bool:
        if not playlist:
            return False
        if current_platform == "netease":
            return is_netease_song_removable_playlist(playlist, auth.get("qq_number", ""))
        return playlist_can_write(playlist)

    def switch_platform(new_platform: str) -> None:
        nonlocal api, auth, current_platform, current_playlist, playback_token, user_stopped
        new_platform = normalize_platform(new_platform)
        if new_platform == current_platform:
            return
        player.stop()
        playback_token += 1
        user_stopped = True
        current_platform = new_platform
        platform_var.set(platform_display_name(new_platform))
        settings["platform"] = new_platform
        try:
            save_settings(settings)
        except QQMusicError as exc:
            status_var.set(str(exc))
        api = build_music_api(new_platform, api_base, timeout)
        auth.clear()
        auth.update(load_auth(new_platform))
        current_playlist = None
        remote_playlists.clear()
        playlists.clear()
        songs.clear()
        listbox.delete(0, tk.END)
        playlist_box.delete(0, tk.END)
        lyric_text.delete("1.0", tk.END)
        player_title_var.set("未播放")
        player_meta_var.set("选择歌曲后点击播放")
        queue_var.set("0 / 0")
        info_var.set("搜索歌曲后选择一项")
        root.title(f"{platform_display_name(new_platform)} music_for_all_system(developed_by_Linux_Mint)")
        refresh_account_label()
        refresh_playlist_list()
        status_var.set(f"已切换到 {platform_display_name(new_platform)}")
        if auth.get("qq_number") and settings.get("auto_sync_playlists", True):
            sync_playlists()

    def on_platform_selected(_event: object | None = None) -> None:
        selected = platform_var.get()
        selected_platform = normalize_platform(next((key for key, label in PLATFORMS.items() if label == selected), selected))
        switch_platform(selected_platform)

    platform_box.bind("<<ComboboxSelected>>", on_platform_selected)

    def current_duration_ms() -> int | None:
        duration = player.duration_ms()
        if duration:
            return duration
        if 0 <= current_song_index < len(songs):
            song_duration = songs[current_song_index].duration
            if song_duration:
                return int(song_duration) * 1000
        return None

    def current_position_ms() -> int | None:
        if user_stopped:
            return paused_position_ms
        position = player.position_ms()
        if position is None and playback_started_at:
            position = int((time.monotonic() - playback_started_at) * 1000)
        duration = current_duration_ms()
        if position is not None and duration:
            return min(position, duration)
        return position

    def progress_value_to_ms() -> int | None:
        duration = current_duration_ms()
        if not duration:
            return None
        return int(duration * max(0.0, min(100.0, progress_var.get())) / 100)

    def update_playback_progress() -> None:
        if not progress_dragging:
            duration = current_duration_ms()
            position = current_position_ms()
            duration_text_var.set(format_milliseconds(duration))
            progress_text_var.set(format_milliseconds(position))
            if duration and position is not None:
                progress_var.set(max(0.0, min(100.0, position * 100 / duration)))
            elif user_stopped:
                progress_var.set(0.0)
        root.after(500, update_playback_progress)

    def begin_progress_drag(_event: object) -> None:
        nonlocal progress_dragging
        progress_dragging = True

    def preview_progress_drag(_value: str) -> None:
        if not progress_dragging:
            return
        position = progress_value_to_ms()
        progress_text_var.set(format_milliseconds(position))

    def finish_progress_drag(_event: object) -> None:
        nonlocal playback_started_at, paused_position_ms, progress_dragging
        position = progress_value_to_ms()
        progress_dragging = False
        if position is None:
            return
        paused_position_ms = position
        if player.seek_ms(position):
            playback_started_at = time.monotonic() - (position / 1000)
            progress_text_var.set(format_milliseconds(position))
            return
        player_meta_var.set("当前播放器不支持拖动进度")

    progress_scale.configure(command=preview_progress_drag)
    progress_scale.bind("<ButtonPress-1>", begin_progress_drag)
    progress_scale.bind("<ButtonRelease-1>", finish_progress_drag)

    def song_queue_display(song: Song) -> str:
        parts = [song.title]
        if settings.get("queue_show_singers", True) and song.singers:
            parts.append(f"- {song.singers}")
        if settings.get("queue_show_album", True) and song.album:
            parts.append(f"[{song.album}]")
        if settings.get("queue_show_duration", True) and song.duration:
            parts.append(format_seconds(song.duration))
        if settings.get("queue_show_mid", False) and song.mid:
            parts.append(f"MID: {song.mid}")
        return "  ".join(parts)

    def insert_song_items(results: list[Song]) -> None:
        if results:
            listbox.insert(tk.END, *(song_queue_display(song) for song in results))

    def refresh_song_list() -> None:
        selection = selected_song_index()
        listbox.delete(0, tk.END)
        insert_song_items(songs)
        if 0 <= selection < len(songs):
            listbox.selection_set(selection)
            listbox.see(selection)

    def refresh_playlist_list() -> None:
        selected = selected_playlist()
        playlists.clear()
        playlists.extend(remote_playlists)
        if has_downloaded_songs(str(settings.get("download_dir", DEFAULT_SETTINGS["download_dir"]))):
            playlists.append(Playlist(id="__downloads__", name="已下载的歌曲", dirid="__downloads__"))
        playlist_box.delete(0, tk.END)
        for playlist in playlists:
            playlist_box.insert(tk.END, playlist.display)
        if selected:
            for index, playlist in enumerate(playlists):
                if playlist.id == selected.id:
                    playlist_box.selection_set(index)
                    playlist_box.see(index)
                    break

    def fetch_account_nickname() -> None:
        nonlocal nickname_fetching
        if nickname_fetching:
            return
        if auth.get("nickname") or not auth.get("qq_number") or not auth.get("cookie"):
            return
        nickname_fetching = True
        target_platform = current_platform
        target_account = auth.get("qq_number", "")
        target_cookie = auth.get("cookie", "")
        target_api = api

        def worker() -> None:
            nonlocal nickname_fetching
            try:
                if target_platform == "qqmusic":
                    nickname = target_api.user_nickname(target_account, target_cookie)
                elif target_platform == "netease":
                    nickname = target_api.login_with_cookie(target_cookie).nickname
                else:
                    nickname = ""
            except Exception:
                root.after(0, lambda: reset_nickname_fetching())
                return
            if not nickname:
                root.after(0, lambda: reset_nickname_fetching())
                return

            def update() -> None:
                nonlocal nickname_fetching
                nickname_fetching = False
                if current_platform != target_platform or auth.get("qq_number") != target_account:
                    return
                auth["nickname"] = nickname
                save_auth(target_account, target_cookie, target_platform, nickname)
                refresh_account_label()

            root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def reset_nickname_fetching() -> None:
        nonlocal nickname_fetching
        nickname_fetching = False

    def refresh_account_label() -> None:
        qq_number = auth.get("qq_number")
        if qq_number:
            display_name = auth.get("nickname") or auth.get("username") or qq_number
            account_var.set(f"{platform_display_name(current_platform)}已登录: {display_name}")
            if not auth.get("nickname"):
                fetch_account_nickname()
            write_state = tk.NORMAL if supports_playlist_write() else tk.DISABLED
            new_playlist_button.configure(state=write_state)
            rename_playlist_button.configure(state=write_state)
            delete_playlist_button.configure(state=write_state)
            add_song_button.configure(state=write_state)
            remove_song_button.configure(state=tk.NORMAL)
            download_song_button.configure(state=tk.NORMAL)
        else:
            account_var.set(f"{platform_display_name(current_platform)}未登录")
            new_playlist_button.configure(state=tk.DISABLED)
            rename_playlist_button.configure(state=tk.DISABLED)
            delete_playlist_button.configure(state=tk.DISABLED)
            add_song_button.configure(state=tk.DISABLED)
            remove_song_button.configure(state=tk.NORMAL)
            download_song_button.configure(state=tk.NORMAL)

    def selected_song_index() -> int:
        selection = listbox.curselection()
        if not selection:
            return -1
        index = selection[0]
        if 0 <= index < len(songs):
            return index
        return -1

    def selected_song() -> Song | None:
        index = selected_song_index()
        if index >= 0:
            return songs[index]
        return None

    def selected_playlist() -> Playlist | None:
        selection = playlist_box.curselection()
        if not selection:
            return None
        index = selection[0]
        if 0 <= index < len(playlists):
            return playlists[index]
        return None

    def render_lyrics(raw_text: str) -> None:
        nonlocal current_lyric_lines, current_lyric_index
        current_lyric_lines = []
        current_lyric_index = -1
        lyric_text.delete("1.0", tk.END)
        parsed = parse_lrc_lines(raw_text)
        if not parsed:
            lyric_text.insert(tk.END, strip_lrc_timestamps(raw_text) or "没有返回歌词")
            return
        for time_ms, line in parsed:
            start = lyric_text.index(tk.INSERT)
            lyric_text.insert(tk.END, f"{line}\n")
            end = lyric_text.index(tk.INSERT)
            current_lyric_lines.append(LyricLine(time_ms=time_ms, text=line, start_index=start, end_index=end))

    def update_realtime_lyrics() -> None:
        nonlocal current_lyric_index
        if user_stopped or not current_lyric_lines:
            root.after(500, update_realtime_lyrics)
            return
        position = player.position_ms()
        if position is None and playback_started_at:
            position = int((time.monotonic() - playback_started_at) * 1000)
        if position is None:
            root.after(500, update_realtime_lyrics)
            return
        active = -1
        for index, line in enumerate(current_lyric_lines):
            if line.time_ms <= position + 350:
                active = index
            else:
                break
        if active != current_lyric_index and 0 <= active < len(current_lyric_lines):
            lyric_text.tag_remove("active_lyric", "1.0", tk.END)
            line = current_lyric_lines[active]
            lyric_text.tag_add("active_lyric", line.start_index, line.end_index)
            lyric_text.see(line.start_index)
            player_meta_var.set(line.text)
            current_lyric_index = active
        root.after(500, update_realtime_lyrics)

    def show_song(song: Song) -> None:
        info_var.set(
            "\n".join(
                part
                for part in (
                    f"歌曲: {song.title}",
                    f"歌手: {song.singers}" if song.singers else "",
                    f"专辑: {song.album}" if song.album else "",
                    f"时长: {format_seconds(song.duration)}" if song.duration else "",
                    f"MID: {song.mid}",
                )
                if part
            )
        )
        lyric_text.delete("1.0", tk.END)
        lyric_text.insert(tk.END, "正在加载歌词...")

        def worker() -> None:
            try:
                text = api.lyric(song.mid, auth.get("cookie", ""))
                if not text:
                    text = "没有返回歌词"
                root.after(0, lambda: render_lyrics(text))
            except Exception as exc:  # noqa: BLE001 - show GUI error text instead of crashing.
                message = str(exc)
                root.after(0, lambda: (lyric_text.delete("1.0", tk.END), lyric_text.insert(tk.END, message)))

        threading.Thread(target=worker, daemon=True).start()

    def replace_songs(results: list[Song], message: str) -> None:
        nonlocal current_song_index, playback_token, user_stopped
        current_song_index = -1
        playback_token += 1
        user_stopped = True
        songs.clear()
        listbox.delete(0, tk.END)
        songs.extend(results)
        insert_song_items(results)
        set_busy(False, message)
        queue_var.set(f"0 / {len(songs)}")
        if songs:
            listbox.selection_set(0)
            show_song(songs[0])
        else:
            info_var.set("没有歌曲")
            lyric_text.delete("1.0", tk.END)
            player_meta_var.set("队列为空")

    def append_songs(results: list[Song], message: str) -> None:
        was_empty = not songs
        songs.extend(results)
        insert_song_items(results)
        set_busy(False, message)
        queue_var.set(f"{current_song_index + 1 if current_song_index >= 0 else 0} / {len(songs)}")
        if was_empty and songs:
            listbox.selection_set(0)
            show_song(songs[0])

    def search() -> None:
        nonlocal current_playlist
        keyword = keyword_var.get().strip()
        if not keyword:
            messagebox.showinfo("提示", "先输入歌曲名或歌手")
            return
        search_platform = current_platform
        search_api = build_music_api(search_platform, api_base, timeout)
        search_cookie = auth.get("cookie", "")
        set_busy(True, f"正在搜索 {platform_display_name(search_platform)}...")
        current_playlist = None
        listbox.delete(0, tk.END)
        songs.clear()

        def worker() -> None:
            try:
                results = search_api.search(keyword, cookie=search_cookie)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "搜索失败"), messagebox.showerror("搜索失败", message)))
                return

            def update() -> None:
                if search_platform != current_platform:
                    set_busy(False, f"已切换到 {platform_display_name(current_platform)}，忽略旧搜索结果")
                    return
                replace_songs(results, f"{platform_display_name(search_platform)}找到 {len(results)} 首")

            root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def open_settings() -> None:
        window = tk.Toplevel(root)
        window.title("设置")
        window.resizable(False, False)
        window.transient(root)

        container = ttk.Frame(window, padding=18, style="Panel.TFrame")
        container.grid(row=0, column=0, sticky="nsew")

        title = ttk.Label(container, text="歌曲队列显示", style="Title.TLabel")
        title.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        show_singers_var = tk.BooleanVar(value=bool(settings.get("queue_show_singers", True)))
        show_album_var = tk.BooleanVar(value=bool(settings.get("queue_show_album", True)))
        show_duration_var = tk.BooleanVar(value=bool(settings.get("queue_show_duration", True)))
        show_mid_var = tk.BooleanVar(value=bool(settings.get("queue_show_mid", False)))
        font_size_var = tk.StringVar(value=str(settings.get("queue_font_size", 11)))
        quality_setting_var = tk.StringVar(value=str(settings.get("quality", quality_var.get())))
        auto_sync_var = tk.BooleanVar(value=bool(settings.get("auto_sync_playlists", True)))
        download_dir_var = tk.StringVar(value=str(settings.get("download_dir", DEFAULT_SETTINGS["download_dir"])))

        ttk.Checkbutton(container, text="显示歌手", variable=show_singers_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(container, text="显示专辑名", variable=show_album_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(container, text="显示歌曲时长", variable=show_duration_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(container, text="显示 MID", variable=show_mid_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=4)

        font_label = ttk.Label(container, text="队列字体大小", style="Muted.TLabel")
        font_label.grid(row=5, column=0, sticky="w", pady=(12, 4), padx=(0, 12))
        font_size = ttk.Combobox(container, textvariable=font_size_var, values=("10", "11", "12", "13", "14"), width=8, state="readonly")
        font_size.grid(row=5, column=1, sticky="e", pady=(12, 4))

        playback_title = ttk.Label(container, text="播放和同步", style="Title.TLabel")
        playback_title.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(18, 8))
        quality_label = ttk.Label(container, text="默认音质", style="Muted.TLabel")
        quality_label.grid(row=7, column=0, sticky="w", pady=4, padx=(0, 12))
        quality_setting = ttk.Combobox(container, textvariable=quality_setting_var, values=("128", "320", "flac"), width=8, state="readonly")
        quality_setting.grid(row=7, column=1, sticky="e", pady=4)
        ttk.Checkbutton(container, text="启动时自动同步歌单", variable=auto_sync_var).grid(row=8, column=0, columnspan=2, sticky="w", pady=4)

        download_title = ttk.Label(container, text="下载", style="Title.TLabel")
        download_title.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(18, 8))
        download_entry = ttk.Entry(container, textvariable=download_dir_var, width=42)
        download_entry.grid(row=10, column=0, sticky="ew", pady=4, padx=(0, 8))

        def choose_download_dir() -> None:
            selected = filedialog.askdirectory(parent=window, initialdir=download_dir_var.get() or str(Path.home()))
            if selected:
                download_dir_var.set(selected)

        ttk.Button(container, text="选择目录", command=choose_download_dir, style="Accent.TButton").grid(row=10, column=1, sticky="e", pady=4)

        account_title = ttk.Label(container, text="账号", style="Title.TLabel")
        account_title.grid(row=11, column=0, columnspan=2, sticky="ew", pady=(18, 8))
        account_buttons = ttk.Frame(container, style="Panel.TFrame")
        account_buttons.grid(row=12, column=0, columnspan=2, sticky="ew")
        account_button_text = "退出登录" if auth.get("qq_number") else "登录"
        account_button_command = logout if auth.get("qq_number") else start_login_action
        ttk.Button(account_buttons, text=account_button_text, command=lambda: (window.destroy(), account_button_command()), style="Accent.TButton").grid(row=0, column=0, padx=(0, 8))
        ttk.Button(account_buttons, text="同步歌单", command=lambda: (window.destroy(), sync_playlists()), style="Accent.TButton").grid(row=0, column=1, padx=(0, 8))

        buttons = ttk.Frame(container, style="Panel.TFrame")
        buttons.grid(row=13, column=0, columnspan=2, sticky="e", pady=(18, 0))

        def apply_settings() -> None:
            settings.update(
                {
                    "queue_show_singers": show_singers_var.get(),
                    "queue_show_album": show_album_var.get(),
                    "queue_show_duration": show_duration_var.get(),
                    "queue_show_mid": show_mid_var.get(),
                    "queue_font_size": int(font_size_var.get()),
                    "quality": quality_setting_var.get(),
                    "auto_sync_playlists": auto_sync_var.get(),
                    "download_dir": download_dir_var.get().strip() or str(DEFAULT_SETTINGS["download_dir"]),
                }
            )
            try:
                save_settings(settings)
            except QQMusicError as exc:
                messagebox.showerror("设置保存失败", str(exc))
                return
            listbox.configure(font=("Sans", int(settings.get("queue_font_size", 11))))
            quality_var.set(str(settings.get("quality", "320")))
            refresh_song_list()
            refresh_playlist_list()
            status_var.set("设置已保存")
            window.destroy()

        ttk.Button(buttons, text="取消", command=window.destroy, style="Accent.TButton").grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="保存", command=apply_settings, style="Accent.TButton").grid(row=0, column=1)
        window.grab_set()

    def start_login_action() -> None:
        if current_platform == "netease":
            choose_netease_login_method()
        else:
            choose_login_method()

    def choose_netease_login_method() -> None:
        window = tk.Toplevel(root)
        window.title("网易云音乐登录")
        window.resizable(False, False)
        window.transient(root)

        container = ttk.Frame(window, padding=18, style="Panel.TFrame")
        container.grid(row=0, column=0, sticky="nsew")
        title = ttk.Label(container, text="选择网易云登录方式", style="Title.TLabel", anchor="center")
        title.grid(row=0, column=0, sticky="ew", pady=(0, 14))

        def start(action: Any) -> None:
            window.destroy()
            action()

        ttk.Button(container, text="手机号验证码", command=lambda: start(netease_phone_login), style="Accent.TButton").grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(container, text="导入网页登录Cookie", command=lambda: start(import_netease_cookie_login), style="Accent.TButton").grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(container, text="取消", command=window.destroy, style="Accent.TButton").grid(row=3, column=0, sticky="ew", pady=(4, 0))
        window.grab_set()

    def finish_netease_login(result: QRLoginResult, *windows: tk.Toplevel) -> None:
        save_auth(result.qq_number, result.cookie, current_platform, result.nickname)
        auth.clear()
        auth.update(load_auth(current_platform))
        refresh_account_label()
        set_busy(False, f"已登录: {result.nickname or result.qq_number}")
        for login_window in windows:
            if login_window.winfo_exists():
                login_window.destroy()
        sync_playlists()

    def import_netease_cookie_login() -> None:
        cookie_window = tk.Toplevel(root)
        cookie_window.title("导入网易云 Cookie")
        cookie_window.geometry("620x360")
        cookie_window.transient(root)

        cookie_container = ttk.Frame(cookie_window, padding=14, style="Panel.TFrame")
        cookie_container.grid(row=0, column=0, sticky="nsew")
        cookie_window.columnconfigure(0, weight=1)
        cookie_window.rowconfigure(0, weight=1)
        cookie_container.columnconfigure(0, weight=1)
        cookie_container.rowconfigure(2, weight=1)

        ttk.Label(cookie_container, text="粘贴官方网页版登录后的 Cookie", style="Title.TLabel").grid(row=0, column=0, sticky="ew", pady=(0, 10))
        instructions = (
            "获取方式：1. 用浏览器打开 music.163.com 并登录；"
            "2. 按 F12 打开开发者工具；3. 点 Network/网络；"
            "4. 刷新网页；5. 点任意 music.163.com 请求；"
            "6. 在 Request Headers/请求标头里复制 Cookie 整行的值。"
        )
        ttk.Label(cookie_container, text=instructions, wraplength=580, style="Muted.TLabel").grid(row=1, column=0, sticky="ew", pady=(0, 10))
        cookie_text = tk.Text(cookie_container, height=8, wrap=tk.WORD)
        cookie_text.grid(row=2, column=0, sticky="nsew")
        hint = ttk.Label(cookie_container, text="复制内容通常很长；至少需要包含 MUSIC_U。导入后会保存到本机 .netease_auth.json。", style="Muted.TLabel")
        hint.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        cookie_buttons = ttk.Frame(cookie_container, style="Panel.TFrame")
        cookie_buttons.grid(row=4, column=0, sticky="e", pady=(12, 0))

        def submit_cookie() -> None:
            cookie = cookie_text.get("1.0", tk.END).strip()
            if not cookie:
                messagebox.showinfo("提示", "请先粘贴 Cookie", parent=cookie_window)
                return
            import_button.configure(state=tk.DISABLED)

            def worker() -> None:
                try:
                    result = api.login_with_cookie(cookie)
                except Exception as exc:  # noqa: BLE001
                    message = str(exc)
                    root.after(0, lambda: (import_button.configure(state=tk.NORMAL), messagebox.showerror("导入失败", message, parent=cookie_window)))
                    return
                root.after(0, lambda: finish_netease_login(result, cookie_window))

            threading.Thread(target=worker, daemon=True).start()

        ttk.Button(cookie_buttons, text="取消", command=cookie_window.destroy, style="Accent.TButton").grid(row=0, column=0, padx=(0, 8))
        import_button = ttk.Button(cookie_buttons, text="导入", command=submit_cookie, style="Accent.TButton")
        import_button.grid(row=0, column=1)
        cookie_window.grab_set()

    def netease_phone_login() -> None:
        window = tk.Toplevel(root)
        window.title("网易云音乐登录")
        window.resizable(False, False)
        window.transient(root)

        container = ttk.Frame(window, padding=18, style="Panel.TFrame")
        container.grid(row=0, column=0, sticky="nsew")
        title = ttk.Label(container, text="手机号验证码登录", style="Title.TLabel")
        title.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 12))

        phone_var = tk.StringVar()
        captcha_var = tk.StringVar()
        country_var = tk.StringVar(value="86")
        login_status_var = tk.StringVar(value="")
        captcha_sent = False

        ttk.Label(container, text="区号", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 8))
        ttk.Entry(container, textvariable=country_var, width=6).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(container, text="手机号", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=4, padx=(0, 8))
        ttk.Entry(container, textvariable=phone_var, width=24).grid(row=2, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Label(container, text="短信验证码", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=4, padx=(0, 8))
        ttk.Entry(container, textvariable=captcha_var, width=18).grid(row=3, column=1, sticky="w", pady=4)
        status_label = ttk.Label(container, textvariable=login_status_var, style="Muted.TLabel")
        status_label.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        buttons = ttk.Frame(container, style="Panel.TFrame")
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(16, 0))

        def phone_inputs() -> tuple[str, str]:
            phone = phone_var.get().strip()
            country_code = country_var.get().strip() or "86"
            if not phone:
                raise QQMusicError("请输入手机号")
            if not country_code.isdigit():
                raise QQMusicError("区号只能填写数字")
            return phone, country_code

        def send_captcha() -> None:
            nonlocal captcha_sent
            try:
                phone, country_code = phone_inputs()
            except QQMusicError as exc:
                messagebox.showinfo("提示", str(exc), parent=window)
                return
            captcha_sent = False
            send_button.configure(state=tk.DISABLED)
            login_status_var.set("正在发送验证码...")

            def worker() -> None:
                try:
                    api.send_phone_captcha(phone, country_code)
                except Exception as exc:  # noqa: BLE001
                    message = str(exc)
                    root.after(0, lambda: (send_button.configure(state=tk.NORMAL), login_status_var.set("发送失败"), messagebox.showerror("发送失败", message, parent=window)))
                    return
                def update_sent() -> None:
                    nonlocal captcha_sent
                    captcha_sent = True
                    send_button.configure(state=tk.NORMAL)
                    login_status_var.set("验证码已发送，请输入短信里的完整数字验证码")

                root.after(0, update_sent)

            threading.Thread(target=worker, daemon=True).start()

        def submit_login() -> None:
            if not captcha_sent:
                messagebox.showinfo("提示", "请先在当前窗口发送验证码，再输入收到的新验证码登录", parent=window)
                return
            try:
                phone, country_code = phone_inputs()
            except QQMusicError as exc:
                messagebox.showinfo("提示", str(exc), parent=window)
                return
            captcha = captcha_var.get().strip()
            if not captcha:
                messagebox.showinfo("提示", "请输入验证码", parent=window)
                return
            if not captcha.isdigit() or not (4 <= len(captcha) <= 8):
                messagebox.showinfo("提示", "请输入短信里的数字验证码，网易云通常是 6 位", parent=window)
                return
            login_button.configure(state=tk.DISABLED)
            login_status_var.set("正在登录...")

            def worker() -> None:
                try:
                    result = api.login_with_phone_captcha(phone, captcha, country_code)
                    save_auth(result.qq_number, result.cookie, current_platform, result.nickname)
                except Exception as exc:  # noqa: BLE001
                    message = str(exc)
                    root.after(0, lambda: (login_button.configure(state=tk.NORMAL), login_status_var.set("登录失败"), messagebox.showerror("登录失败", message, parent=window)))
                    return

                def update() -> None:
                    auth.clear()
                    auth.update(load_auth(current_platform))
                    refresh_account_label()
                    set_busy(False, f"已登录: {result.nickname or result.qq_number}")
                    window.destroy()
                    sync_playlists()

                root.after(0, update)

            threading.Thread(target=worker, daemon=True).start()

        def import_cookie_login() -> None:
            cookie_window = tk.Toplevel(window)
            cookie_window.title("导入网易云 Cookie")
            cookie_window.geometry("620x360")
            cookie_window.transient(window)

            cookie_container = ttk.Frame(cookie_window, padding=14, style="Panel.TFrame")
            cookie_container.grid(row=0, column=0, sticky="nsew")
            cookie_window.columnconfigure(0, weight=1)
            cookie_window.rowconfigure(0, weight=1)
            cookie_container.columnconfigure(0, weight=1)
            cookie_container.rowconfigure(2, weight=1)

            ttk.Label(cookie_container, text="粘贴官方网页版登录后的 Cookie", style="Title.TLabel").grid(row=0, column=0, sticky="ew", pady=(0, 10))
            instructions = (
                "获取方式：1. 用浏览器打开 music.163.com 并登录；"
                "2. 按 F12 打开开发者工具；3. 点 Network/网络；"
                "4. 刷新网页；5. 点任意 music.163.com 请求；"
                "6. 在 Request Headers/请求标头里复制 Cookie 整行的值。"
            )
            ttk.Label(cookie_container, text=instructions, wraplength=580, style="Muted.TLabel").grid(row=1, column=0, sticky="ew", pady=(0, 10))
            cookie_text = tk.Text(cookie_container, height=8, wrap=tk.WORD)
            cookie_text.grid(row=2, column=0, sticky="nsew")
            hint = ttk.Label(cookie_container, text="复制内容通常很长；至少需要包含 MUSIC_U。导入后会保存到本机 .netease_auth.json。", style="Muted.TLabel")
            hint.grid(row=3, column=0, sticky="ew", pady=(8, 0))
            cookie_buttons = ttk.Frame(cookie_container, style="Panel.TFrame")
            cookie_buttons.grid(row=4, column=0, sticky="e", pady=(12, 0))

            def submit_cookie() -> None:
                cookie = cookie_text.get("1.0", tk.END).strip()
                if not cookie:
                    messagebox.showinfo("提示", "请先粘贴 Cookie", parent=cookie_window)
                    return
                import_button.configure(state=tk.DISABLED)

                def worker() -> None:
                    try:
                        result = api.login_with_cookie(cookie)
                        save_auth(result.qq_number, result.cookie, current_platform, result.nickname)
                    except Exception as exc:  # noqa: BLE001
                        message = str(exc)
                        root.after(0, lambda: (import_button.configure(state=tk.NORMAL), messagebox.showerror("导入失败", message, parent=cookie_window)))
                        return

                    def update() -> None:
                        auth.clear()
                        auth.update(load_auth(current_platform))
                        refresh_account_label()
                        set_busy(False, f"已登录: {result.nickname or result.qq_number}")
                        cookie_window.destroy()
                        window.destroy()
                        sync_playlists()

                    root.after(0, update)

                threading.Thread(target=worker, daemon=True).start()

            ttk.Button(cookie_buttons, text="取消", command=cookie_window.destroy, style="Accent.TButton").grid(row=0, column=0, padx=(0, 8))
            import_button = ttk.Button(cookie_buttons, text="导入", command=submit_cookie, style="Accent.TButton")
            import_button.grid(row=0, column=1)
            cookie_window.grab_set()

        send_button = ttk.Button(buttons, text="发送验证码", command=send_captcha, style="Accent.TButton")
        send_button.grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="导入Cookie", command=import_cookie_login, style="Accent.TButton").grid(row=0, column=1, padx=(0, 8))
        ttk.Button(buttons, text="取消", command=window.destroy, style="Accent.TButton").grid(row=0, column=2, padx=(0, 8))
        login_button = ttk.Button(buttons, text="登录", command=submit_login, style="Accent.TButton")
        login_button.grid(row=0, column=3)
        window.grab_set()

    def choose_login_method() -> None:
        window = tk.Toplevel(root)
        window.title("选择登录方式")
        window.resizable(False, False)
        window.transient(root)

        container = ttk.Frame(window, padding=18, style="Panel.TFrame")
        container.grid(row=0, column=0, sticky="nsew")
        title = ttk.Label(container, text="选择登录方式", style="Title.TLabel", anchor="center")
        title.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))

        def start_login(provider: str) -> None:
            window.destroy()
            login(provider)

        ttk.Button(container, text="QQ 登录", command=lambda: start_login("qq"), style="Accent.TButton").grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(container, text="微信登录", command=lambda: start_login("wechat"), style="Accent.TButton").grid(row=1, column=1, sticky="ew")
        ttk.Button(container, text="取消", command=window.destroy, style="Accent.TButton").grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        window.grab_set()

    def login(provider: str = "qq") -> None:
        provider_name = "微信" if provider == "wechat" else "QQ"
        set_busy(True, f"正在获取{provider_name}登录二维码...")
        qr_window: tk.Toplevel | None = None
        qr_label: ttk.Label | None = None
        qr_image: Any = None
        qr_cancelled = False

        def close_qr() -> None:
            nonlocal qr_cancelled
            qr_cancelled = True
            if qr_window and qr_window.winfo_exists():
                qr_window.destroy()
            set_busy(False, "已取消登录")

        def build_photo_image(image_data: bytes) -> tk.PhotoImage:
            encoded = base64.b64encode(image_data).decode("ascii")
            try:
                return tk.PhotoImage(data=encoded)
            except tk.TclError as exc:
                try:
                    from PIL import Image
                except ImportError as pil_exc:
                    raise QQMusicError("微信二维码图片需要 Pillow 支持，请先安装：python3 -m pip install pillow") from pil_exc
                output = io.BytesIO()
                Image.open(io.BytesIO(image_data)).save(output, format="PNG")
                return tk.PhotoImage(data=base64.b64encode(output.getvalue()).decode("ascii"), format="png")

        def show_qr(session: QRLoginSession | WXLoginSession) -> None:
            nonlocal qr_window, qr_label, qr_image, qr_cancelled
            qr_window = tk.Toplevel(root)
            qr_window.title("QQ 音乐登录")
            qr_window.resizable(False, False)
            qr_window.protocol("WM_DELETE_WINDOW", close_qr)

            container = ttk.Frame(qr_window, padding=18)
            container.grid(row=0, column=0, sticky="nsew")

            title = ttk.Label(container, text=f"使用{provider_name}扫码登录", anchor="center")
            title.grid(row=0, column=0, sticky="ew", pady=(0, 10))

            try:
                qr_image = build_photo_image(session.image)
            except QQMusicError as exc:
                qr_cancelled = True
                qr_window.destroy()
                set_busy(False, "二维码显示失败")
                messagebox.showerror("登录失败", str(exc))
                return
            qr_label = ttk.Label(container, image=qr_image)
            qr_label.image = qr_image  # type: ignore[attr-defined]
            qr_label.grid(row=1, column=0)

            hint = ttk.Label(container, text="扫码后在手机上确认，确认后会自动同步歌单。", anchor="center")
            hint.grid(row=2, column=0, sticky="ew", pady=(10, 0))
            status_var.set(f"等待{provider_name}扫码...")

        def finish_login(result: QRLoginResult) -> None:
            nonlocal qr_cancelled
            qr_cancelled = True
            if qr_window and qr_window.winfo_exists():
                qr_window.destroy()
            save_auth(result.qq_number, result.cookie, current_platform, result.nickname)
            auth.clear()
            auth.update(load_auth(current_platform))
            refresh_account_label()
            set_busy(False, f"已登录: {result.nickname or result.qq_number}")
            sync_playlists()

        def worker() -> None:
            try:
                session = api.start_wx_qr_login() if provider == "wechat" else api.start_qr_login()
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "获取二维码失败"), messagebox.showerror("登录失败", message)))
                return
            root.after(0, lambda: show_qr(session))

            deadline = time.time() + 180
            while time.time() < deadline and not qr_cancelled:
                try:
                    state, result = api.poll_wx_qr_login(session) if provider == "wechat" else api.poll_qr_login(session)
                except Exception as exc:  # noqa: BLE001
                    message = str(exc)
                    root.after(0, lambda: (set_busy(False, "登录失败"), messagebox.showerror("登录失败", message)))
                    return
                if state == "waiting":
                    root.after(0, lambda: status_var.set(f"等待{provider_name}扫码..."))
                elif state == "confirming":
                    root.after(0, lambda: status_var.set("已扫码，等待手机确认..."))
                elif state == "cancelled":
                    root.after(0, lambda: status_var.set("已取消确认，请重新扫码"))
                elif state == "expired":
                    root.after(0, lambda: (set_busy(False, "二维码已过期"), messagebox.showinfo("登录", "二维码已过期，请重新点击登录")))
                    return
                elif state == "done" and result:
                    root.after(0, lambda: finish_login(result))
                    return
                time.sleep(2)

            if not qr_cancelled:
                root.after(0, lambda: (set_busy(False, "登录超时"), messagebox.showinfo("登录", "登录超时，请重新点击登录")))

        threading.Thread(target=worker, daemon=True).start()

    def logout() -> None:
        player.stop()
        clear_auth(current_platform)
        auth.clear()
        remote_playlists.clear()
        playlists.clear()
        playlist_box.delete(0, tk.END)
        refresh_playlist_list()
        refresh_account_label()
        status_var.set("已退出登录")

    def sync_playlists() -> None:
        qq_number = auth.get("qq_number", "")
        if not qq_number:
            messagebox.showinfo("提示", f"先登录 {platform_display_name(current_platform)}")
            return
        set_busy(True, "正在同步歌单...")

        def worker() -> None:
            try:
                results = api.user_playlists(qq_number, auth.get("cookie", ""))
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "同步失败"), messagebox.showerror("同步失败", message)))
                return

            def update() -> None:
                remote_playlists.clear()
                remote_playlists.extend(results)
                refresh_playlist_list()
                set_busy(False, f"已同步 {len(remote_playlists)} 个歌单")
                if playlists:
                    playlist_box.selection_set(0)

            root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def load_playlist() -> None:
        nonlocal current_playlist, playlist_load_token
        playlist = selected_playlist()
        if not playlist:
            return
        playlist_load_token += 1
        load_token = playlist_load_token
        current_playlist = playlist
        if playlist.id == "__downloads__":
            replace_songs(downloaded_songs(str(settings.get("download_dir", DEFAULT_SETTINGS["download_dir"]))), "已下载的歌曲")
            return
        set_busy(True, f"正在加载歌单: {playlist.name}")

        def worker() -> None:
            try:
                playlist_name, results, total = api.playlist_songs_page(playlist.id, auth.get("cookie", ""), 0, PLAYLIST_INITIAL_PAGE_SIZE)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "歌单加载失败"), messagebox.showerror("歌单加载失败", message)))
                return
            loaded = len(results)

            def show_first_page(page_results: list[Song] = results, name: str = playlist_name, loaded_count: int = loaded, total_count: int = total) -> None:
                if load_token != playlist_load_token:
                    return
                if loaded_count < total_count:
                    replace_songs(page_results, f"{name}: 已加载 {loaded_count} / {total_count} 首")
                else:
                    replace_songs(page_results, f"{name}: {loaded_count} 首")

            root.after(0, show_first_page)
            while loaded < total:
                try:
                    page_name, page_results, page_total = api.playlist_songs_page(
                        playlist.id,
                        auth.get("cookie", ""),
                        loaded,
                        PLAYLIST_BACKGROUND_PAGE_SIZE,
                    )
                except Exception as exc:  # noqa: BLE001
                    message = str(exc)

                    def show_partial_error(error_message: str = message, loaded_count: int = loaded, total_count: int = total) -> None:
                        if load_token == playlist_load_token:
                            set_busy(False, f"已加载 {loaded_count} / {total_count} 首，后续加载失败: {error_message}")

                    root.after(0, show_partial_error)
                    return
                if page_name:
                    playlist_name = page_name
                total = max(total, page_total)
                if not page_results:
                    break
                loaded += len(page_results)

                def show_next_page(
                    page_results: list[Song] = page_results,
                    name: str = playlist_name,
                    loaded_count: int = loaded,
                    total_count: int = total,
                ) -> None:
                    if load_token != playlist_load_token:
                        return
                    if loaded_count < total_count:
                        append_songs(page_results, f"{name}: 已加载 {loaded_count} / {total_count} 首")
                    else:
                        append_songs(page_results, f"{name}: {loaded_count} 首")

                root.after(0, show_next_page)
                if len(page_results) < PLAYLIST_BACKGROUND_PAGE_SIZE and loaded >= total:
                    break

        threading.Thread(target=worker, daemon=True).start()

    def require_login_cookie() -> str | None:
        cookie = auth.get("cookie", "")
        if not cookie:
            messagebox.showinfo("提示", "请先登录")
            return None
        return cookie

    def create_playlist_action() -> None:
        cookie = require_login_cookie()
        if not cookie:
            return
        name = simpledialog.askstring("新建歌单", "歌单名称:", parent=root)
        if not name or not name.strip():
            return
        set_busy(True, "正在创建歌单...")

        def worker() -> None:
            try:
                api.create_playlist(name.strip(), cookie)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "创建失败"), messagebox.showerror("创建失败", message)))
                return
            root.after(0, lambda: (set_busy(False, "歌单已创建"), sync_playlists()))

        threading.Thread(target=worker, daemon=True).start()

    def rename_playlist_action() -> None:
        cookie = require_login_cookie()
        playlist = selected_playlist()
        if not cookie or not playlist:
            messagebox.showinfo("提示", "先选择一个歌单")
            return
        if not playlist_can_write(playlist):
            messagebox.showinfo("提示", f"“{playlist.name}”不是可重命名的自建歌单")
            return
        name = simpledialog.askstring("重命名歌单", "新名称:", initialvalue=playlist.name, parent=root)
        if not name or not name.strip() or name.strip() == playlist.name:
            return
        set_busy(True, "正在重命名歌单...")

        def worker() -> None:
            try:
                api.rename_playlist(playlist.dirid or playlist.id, name.strip(), cookie)
                results = api.user_playlists(auth.get("qq_number", ""), cookie)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "重命名失败"), messagebox.showerror("重命名失败", message)))
                return

            renamed = next(
                (
                    result
                    for result in results
                    if (playlist.dirid and result.dirid == playlist.dirid) or result.id == playlist.id
                ),
                None,
            )
            if not renamed or renamed.name != name.strip():
                root.after(
                    0,
                    lambda: (
                        set_busy(False, "重命名未生效"),
                        messagebox.showerror("重命名失败", f"{platform_display_name(current_platform)}已接受请求，但刷新后歌单名称仍未变化，请稍后重试或重新登录。"),
                    ),
                )
                return

            def update() -> None:
                nonlocal current_playlist
                remote_playlists.clear()
                remote_playlists.extend(results)
                if current_playlist and ((playlist.dirid and current_playlist.dirid == playlist.dirid) or current_playlist.id == playlist.id):
                    current_playlist = renamed
                refresh_playlist_list()
                set_busy(False, "歌单已重命名")

            root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def delete_playlist_action() -> None:
        cookie = require_login_cookie()
        playlist = selected_playlist()
        if not cookie or not playlist:
            messagebox.showinfo("提示", "先选择一个歌单")
            return
        if not playlist_can_write(playlist):
            messagebox.showinfo("提示", f"“{playlist.name}”不是可删除的自建歌单")
            return
        if not messagebox.askyesno("删除歌单", f"确定删除“{playlist.name}”吗？"):
            return
        set_busy(True, "正在删除歌单...")

        def worker() -> None:
            try:
                api.delete_playlist(playlist.dirid or playlist.id, cookie)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "删除失败"), messagebox.showerror("删除失败", message)))
                return
            root.after(0, lambda: (set_busy(False, "歌单已删除"), sync_playlists()))

        threading.Thread(target=worker, daemon=True).start()

    def add_song_to_playlist_action() -> None:
        song = selected_song()
        if not song:
            messagebox.showinfo("提示", "先选择一首歌")
            return
        cookie = require_login_cookie()
        if not cookie:
            return
        if not song.song_id:
            messagebox.showerror("添加失败", f"当前歌曲缺少{platform_display_name(current_platform)}歌曲 ID")
            return
        candidates = [playlist for playlist in remote_playlists if playlist_can_add(playlist)]
        if not candidates:
            messagebox.showinfo("提示", f"还没有可加入的{platform_display_name(current_platform)}自建歌单，请先同步或新建歌单")
            return
        if len(candidates) == 1:
            add_song_to_playlist_target(song, candidates[0], cookie)
            return

        chooser = tk.Toplevel(root)
        chooser.title("加入歌单")
        chooser.geometry("420x460")
        chooser.transient(root)

        container = ttk.Frame(chooser, padding=14, style="Panel.TFrame")
        container.grid(row=0, column=0, sticky="nsew")
        chooser.columnconfigure(0, weight=1)
        chooser.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(1, weight=1)

        title = ttk.Label(container, text=f"选择要加入的歌单：{song.title}", style="Title.TLabel")
        title.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))

        target_box = tk.Listbox(
            container,
            activestyle="none",
            exportselection=False,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#d7dde5",
            selectbackground="#dbeafe",
            selectforeground="#111827",
            font=("Sans", 11),
        )
        target_box.grid(row=1, column=0, sticky="nsew")
        target_scroll = ttk.Scrollbar(container, orient=tk.VERTICAL, command=target_box.yview)
        target_scroll.grid(row=1, column=1, sticky="ns")
        target_box.configure(yscrollcommand=target_scroll.set)
        for playlist in candidates:
            target_box.insert(tk.END, playlist.display)
        selected_target = selected_playlist()
        selected_candidate_index = next(
            (
                index
                for index, playlist in enumerate(candidates)
                if selected_target and playlist.id == selected_target.id
            ),
            0,
        )
        target_box.selection_set(selected_candidate_index)
        target_box.see(selected_candidate_index)

        buttons = ttk.Frame(container, style="Panel.TFrame")
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))

        def chooser_selected_target() -> Playlist | None:
            selection = target_box.curselection()
            if not selection:
                return None
            index = selection[0]
            return candidates[index] if 0 <= index < len(candidates) else None

        def add_to_selected() -> None:
            playlist = chooser_selected_target()
            if not playlist:
                messagebox.showinfo("提示", "先选择一个歌单", parent=chooser)
                return
            chooser.destroy()
            add_song_to_playlist_target(song, playlist, cookie)

        ttk.Button(buttons, text="取消", command=chooser.destroy, style="Accent.TButton").grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="加入", command=add_to_selected, style="Accent.TButton").grid(row=0, column=1)
        target_box.bind("<Double-Button-1>", lambda _event: add_to_selected())
        chooser.grab_set()

    def add_song_to_playlist_target(song: Song, playlist: Playlist, cookie: str) -> None:
        set_busy(True, f"正在加入歌单: {playlist.name}")

        def worker() -> None:
            try:
                api.add_song_to_playlist(song.song_id, song.song_type, playlist.dirid or playlist.id, cookie, song.mid, playlist.id)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "添加失败"), messagebox.showerror("添加失败", message)))
                return

            def update() -> None:
                set_busy(False, f"已加入: {playlist.name}")
                if current_playlist and current_playlist.id == playlist.id:
                    load_playlist()
                else:
                    sync_playlists()

            root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def remove_song_from_playlist_action() -> None:
        song = selected_song()
        if not current_playlist:
            messagebox.showinfo("提示", "先双击打开一个歌单，再选择要移除的歌曲")
            return
        if not song:
            messagebox.showinfo("提示", "先选择一首歌")
            return
        if current_playlist.id == "__downloads__":
            if not song.local_path:
                messagebox.showerror("移除失败", "当前本地歌曲没有文件路径")
                return
            if not messagebox.askyesno("删除本地文件", f"删除本地文件“{Path(song.local_path).name}”？"):
                return
            try:
                path = Path(song.local_path)
                path.unlink()
                index = load_download_index(str(path.parent))
                index.pop(path.name, None)
                save_download_index(str(path.parent), index)
            except OSError as exc:
                messagebox.showerror("删除失败", str(exc))
                return
            refresh_playlist_list()
            load_playlist()
            return
        cookie = require_login_cookie()
        if not cookie:
            return
        if not song.song_id:
            messagebox.showerror("移除失败", f"当前歌曲缺少{platform_display_name(current_platform)}歌曲 ID")
            return
        if not playlist_can_remove_song(current_playlist):
            messagebox.showinfo("提示", f"“{current_playlist.name}”不支持移除歌曲")
            return
        if not messagebox.askyesno("移除歌曲", f"从“{current_playlist.name}”移除“{song.title}”？"):
            return
        target_playlist = current_playlist
        set_busy(True, "正在移除歌曲...")

        def worker() -> None:
            try:
                api.remove_song_from_playlist(song.song_id, song.song_type, target_playlist.dirid or target_playlist.id, cookie)
                synced_playlists = api.user_playlists(auth.get("qq_number", ""), cookie)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "移除失败"), messagebox.showerror("移除失败", message)))
                return

            def update() -> None:
                nonlocal current_playlist
                remote_playlists.clear()
                remote_playlists.extend(synced_playlists)
                refreshed = next(
                    (
                        playlist
                        for playlist in remote_playlists
                        if (target_playlist.dirid and playlist.dirid == target_playlist.dirid) or playlist.id == target_playlist.id
                    ),
                    target_playlist,
                )
                current_playlist = refreshed
                refresh_playlist_list()
                set_busy(False, "歌曲已移除，歌单已同步")
                load_playlist()

            root.after(0, update)

        threading.Thread(target=worker, daemon=True).start()

    def download_song_action() -> None:
        song = selected_song()
        if not song:
            messagebox.showinfo("提示", "先选择一首歌")
            return
        if song.local_path:
            messagebox.showinfo("提示", "这首歌已经是本地文件")
            return
        download_dir = str(settings.get("download_dir", DEFAULT_SETTINGS["download_dir"]))
        quality = quality_var.get()
        set_busy(True, f"正在下载: {song.title}")

        def worker() -> None:
            try:
                url = api.song_url(song.mid, quality, auth.get("cookie", ""), song.media_mid)
                target_dir = Path(download_dir).expanduser()
                target_dir.mkdir(parents=True, exist_ok=True)
                extension = guess_audio_extension(url, quality)
                base_name = safe_filename(f"{song.title} - {song.singers}" if song.singers else song.title)
                target = target_dir / f"{base_name}{extension}"
                counter = 2
                while target.exists():
                    target = target_dir / f"{base_name} ({counter}){extension}"
                    counter += 1

                request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 music-linux-client/1.0"})
                with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as file:
                    shutil.copyfileobj(response, file)

                index = load_download_index(str(target_dir))
                index[target.name] = {
                    "title": song.title,
                    "mid": song.mid,
                    "song_id": song.song_id,
                    "song_type": song.song_type,
                    "singers": song.singers,
                    "album": song.album,
                    "duration": song.duration,
                    "media_mid": song.media_mid,
                    "quality": quality,
                    "downloaded_at": int(time.time()),
                }
                save_download_index(str(target_dir), index)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "下载失败"), messagebox.showerror("下载失败", message)))
                return
            root.after(0, lambda: (refresh_playlist_list(), set_busy(False, f"已下载: {song.title}")))

        threading.Thread(target=worker, daemon=True).start()

    def next_index_for_mode(auto: bool = False, reverse: bool = False) -> int:
        if not songs:
            return -1
        mode = play_mode_var.get()
        index = current_song_index if current_song_index >= 0 else selected_song_index()
        if index < 0:
            index = 0
        if mode == "单曲循环":
            return index
        if mode == "随机播放":
            if len(songs) == 1:
                return index
            choices = [candidate for candidate in range(len(songs)) if candidate != index]
            return random.choice(choices)
        if reverse:
            return index - 1 if index > 0 else len(songs) - 1
        next_index = index + 1
        if next_index < len(songs):
            return next_index
        return -1 if auto else 0

    def monitor_playback(token: int) -> None:
        time.sleep(4)
        while token == playback_token and not user_stopped:
            if player.has_ended():
                root.after(0, lambda: auto_advance(token))
                return
            time.sleep(1)

    def auto_advance(token: int) -> None:
        if token != playback_token or user_stopped:
            return
        next_index = next_index_for_mode(auto=True)
        if next_index < 0:
            player_meta_var.set("播放完成")
            progress_var.set(100.0)
            status_var.set("播放完成")
            return
        play_song_at(next_index)

    def play_song_at(index: int, start_position_ms: int = 0) -> None:
        nonlocal current_song_index, playback_token, user_stopped, playback_started_at, paused_position_ms
        if not (0 <= index < len(songs)):
            messagebox.showinfo("提示", "先选择一首歌")
            return
        song = songs[index]
        if not song.mid:
            messagebox.showerror("播放失败", "这条搜索结果没有 MID")
            return
        playback_token += 1
        play_token = playback_token
        user_stopped = False
        paused_position_ms = max(0, int(start_position_ms))
        player_stop_button.configure(text="暂停")
        current_song_index = index
        listbox.selection_clear(0, tk.END)
        listbox.selection_set(index)
        listbox.see(index)
        show_song(song)
        player_title_var.set(song.title)
        player_meta_var.set(song.singers or song.album or "正在获取播放链接")
        queue_var.set(f"{index + 1} / {len(songs)}")
        progress_var.set(0.0)
        progress_text_var.set(format_milliseconds(paused_position_ms))
        duration_text_var.set(format_milliseconds(song.duration * 1000 if song.duration else None))
        set_busy(True, "正在获取播放链接...")

        def worker() -> None:
            try:
                url = song.local_path or api.song_url(song.mid, quality_var.get(), auth.get("cookie", ""), song.media_mid)
                start_position = paused_position_ms
                player.play(url, start_position)
                if start_position:
                    player.seek_ms(start_position)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "播放失败"), messagebox.showerror("播放失败", message)))
                return
            def update_started() -> None:
                nonlocal playback_started_at
                playback_started_at = time.monotonic() - (start_position / 1000)
                set_busy(False)
                player_meta_var.set(song.singers or song.album or "正在播放")
                threading.Thread(target=monitor_playback, args=(play_token,), daemon=True).start()

            root.after(0, update_started)

        threading.Thread(target=worker, daemon=True).start()

    def play_selected() -> None:
        index = selected_song_index()
        if index < 0 and songs:
            index = current_song_index if current_song_index >= 0 else 0
        if user_stopped and index == current_song_index and paused_position_ms:
            if player.resume():
                nonlocal_update_resume()
                return
            play_song_at(index, paused_position_ms)
            return
        play_song_at(index)

    def resume_paused_song() -> None:
        if not (user_stopped and paused_position_ms and 0 <= current_song_index < len(songs)):
            play_selected()
            return
        if player.resume():
            nonlocal_update_resume()
            return
        play_song_at(current_song_index, paused_position_ms)

    def nonlocal_update_resume() -> None:
        nonlocal playback_token, user_stopped, playback_started_at
        playback_token += 1
        token = playback_token
        user_stopped = False
        playback_started_at = time.monotonic() - (paused_position_ms / 1000)
        player_meta_var.set("继续播放")
        player_stop_button.configure(text="暂停")
        status_var.set("继续播放")
        threading.Thread(target=monitor_playback, args=(token,), daemon=True).start()

    def play_previous() -> None:
        if not songs:
            messagebox.showinfo("提示", "当前没有歌曲队列")
            return
        play_song_at(next_index_for_mode(reverse=True))

    def play_next() -> None:
        if not songs:
            messagebox.showinfo("提示", "当前没有歌曲队列")
            return
        next_index = next_index_for_mode()
        if next_index >= 0:
            play_song_at(next_index)

    def stop() -> None:
        nonlocal playback_token, user_stopped, playback_started_at, paused_position_ms
        playback_token += 1
        position = current_position_ms()
        if position is not None:
            paused_position_ms = position
        user_stopped = True
        playback_started_at = 0.0
        player.pause()
        player_meta_var.set("已暂停")
        player_stop_button.configure(text="继续播放")
        progress_text_var.set(format_milliseconds(paused_position_ms))
        duration = current_duration_ms()
        if duration:
            progress_var.set(max(0.0, min(100.0, paused_position_ms * 100 / duration)))
        status_var.set("已暂停")

    def toggle_pause_resume() -> None:
        if user_stopped and paused_position_ms:
            resume_paused_song()
            return
        stop()

    def on_select(_event: object) -> None:
        song = selected_song()
        if song:
            show_song(song)

    search_button.configure(command=search)
    settings_button.configure(command=open_settings)
    new_playlist_button.configure(command=create_playlist_action)
    rename_playlist_button.configure(command=rename_playlist_action)
    delete_playlist_button.configure(command=delete_playlist_action)
    add_song_button.configure(command=add_song_to_playlist_action)
    remove_song_button.configure(command=remove_song_from_playlist_action)
    download_song_button.configure(command=download_song_action)
    prev_button.configure(command=play_previous)
    player_play_button.configure(command=play_selected)
    player_stop_button.configure(command=toggle_pause_resume)
    next_button.configure(command=play_next)
    entry.bind("<Return>", lambda _event: search())
    playlist_box.bind("<Double-Button-1>", lambda _event: load_playlist())
    listbox.bind("<<ListboxSelect>>", on_select)
    listbox.bind("<Double-Button-1>", lambda _event: play_selected())
    root.protocol("WM_DELETE_WINDOW", lambda: (player.stop(), root.destroy()))
    refresh_account_label()
    refresh_playlist_list()
    update_realtime_lyrics()
    update_playback_progress()
    if auth.get("qq_number") and settings.get("auto_sync_playlists", True):
        sync_playlists()

    root.mainloop()
    return 0


def run_flet_gui(api_base: str, timeout: int, player_command: str | None) -> int:
    try:
        import flet as ft
    except ImportError as exc:
        raise QQMusicError("当前 Python 没有安装 Flet，请在 common 环境里运行或安装：uv pip install flet flet-desktop；旧界面请运行 qqmusic_client_old_ui.py") from exc

    def app(page: Any) -> None:
        settings = load_settings()
        current_platform = normalize_platform(str(settings.get("platform", "qqmusic")))
        api = build_music_api(current_platform, api_base, timeout)
        player = Player(player_command)
        atexit.register(player.stop)
        auth = load_auth(current_platform)
        songs: list[Song] = []
        playlists: list[Playlist] = []
        remote_playlists: list[Playlist] = []
        current_playlist: Playlist | None = None
        current_song_index = -1
        selected_playlist_tile_index = -1
        selected_song_tile_index = -1
        playlist_load_token = 0
        playback_token = 0
        user_stopped = True
        playback_started_at = 0.0
        paused_position_ms = 0
        progress_dragging = False
        displayed_lyric_key = ""
        current_lyric_entries: list[tuple[int, str]] = []
        current_lyric_index = -1
        pending_buffer_resume_ms: int | None = None
        last_song_click_index = -1
        last_song_click_at = 0.0
        cover_cache: dict[str, str] = {}
        cover_controls: dict[str, list[Any]] = {}
        pending_cover_urls: dict[str, str] = {}
        playlist_fallback_cover_pending: set[str] = set()
        playlist_fallback_cover_done: set[str] = set()
        playlist_fallback_cover_generation = 0
        cover_sync_running = False

        bg = "#080b12"
        panel = "#111827"
        panel_soft = "#0f172a"
        line = "#243244"
        text = "#f8fafc"
        muted = "#9aa8bb"
        accent = "#22c55e"
        accent_2 = "#38bdf8"
        danger = "#fb7185"

        def border_all(color: str, width: int = 1) -> Any:
            side = ft.BorderSide(width, color)
            return ft.Border(top=side, right=side, bottom=side, left=side)

        page.title = f"{platform_display_name(current_platform)} music_for_all_system(developed_by_Linux_Mint)"
        page.bgcolor = bg
        page.padding = 0
        page.theme_mode = ft.ThemeMode.DARK
        try:
            page.window.width = 1180
            page.window.height = 760
            page.window.min_width = 980
            page.window.min_height = 620
        except Exception:
            pass

        def option(value: str, label: str) -> Any:
            return ft.dropdown.Option(key=value, text=label)

        QUALITY_LABELS = {
            "128": "标准音质",
            "320": "高音质",
            "flac": "无损音质",
        }

        keyword_field = ft.TextField(
            hint_text="搜索歌曲、歌手或专辑",
            expand=True,
            height=52,
            border_radius=8,
            bgcolor="#020617",
            border_color=line,
            focused_border_color=accent,
            color=text,
            text_size=16,
            content_padding=ft.Padding(14, 12, 14, 12),
            on_submit=lambda _event: search(),
        )
        platform_dropdown = ft.Dropdown(
            value=current_platform,
            options=[option(key, label) for key, label in PLATFORMS.items()],
            width=164,
            height=52,
            border_radius=8,
            bgcolor="#020617",
            border_color=line,
            focused_border_color=accent_2,
            color=text,
            text_size=16,
            content_padding=ft.Padding(14, 12, 8, 12),
            on_select=lambda _event: switch_platform(str(platform_dropdown.value)),
        )
        quality_dropdown = ft.Dropdown(
            value=str(settings.get("quality", "320")),
            options=[option(key, label) for key, label in QUALITY_LABELS.items()],
            width=150,
            height=52,
            border_radius=8,
            bgcolor="#020617",
            border_color=line,
            color=text,
            text_size=16,
            content_padding=ft.Padding(14, 12, 8, 12),
            on_select=lambda _event: save_quality(),
        )
        play_mode_dropdown = ft.Dropdown(
            value=str(settings.get("play_mode", "顺序播放")),
            options=[option("顺序播放", "顺序播放"), option("随机播放", "随机播放"), option("单曲循环", "单曲循环")],
            width=188,
            height=52,
            border_radius=8,
            bgcolor="#020617",
            border_color=line,
            color=text,
            text_size=16,
            content_padding=ft.Padding(14, 12, 8, 12),
            on_select=lambda _event: save_play_mode(),
        )

        status_text = ft.Text("", color=muted, size=12, width=150, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        lyric_preview_text = ft.Text("", color="#dbeafe", size=13, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True)
        account_text = ft.Text("", color=accent, size=14, weight=ft.FontWeight.BOLD, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        playlist_list = ft.ListView(expand=True, spacing=8, padding=ft.Padding(0, 0, 4, 0), auto_scroll=False)
        song_list = ft.ListView(expand=True, spacing=8, padding=10, auto_scroll=False)
        song_info = ft.Text("搜索歌曲后选择一项", color=muted, size=14, selectable=True)
        lyric_list = ft.ListView(expand=True, spacing=8, padding=ft.Padding(0, 8, 0, 0), auto_scroll=False)
        player_title = ft.Text("未播放", color=text, size=24, weight=ft.FontWeight.BOLD, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        player_meta = ft.Text("选择歌曲后点击播放", color=muted, size=13, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
        queue_text = ft.Text("0 / 0", color=muted, size=13)
        hero_cover = ft.Container(
            width=120,
            height=120,
            border_radius=8,
            bgcolor="#0f172a",
            border=border_all(line),
            alignment=ft.Alignment(0, 0),
            content=ft.Icon(ft.Icons.MUSIC_NOTE, color="#ffffff", size=54),
        )
        elapsed_text = ft.Text("0:00", color=muted, size=12, width=44)
        duration_text = ft.Text("0:00", color=muted, size=12, width=44, text_align=ft.TextAlign.RIGHT)
        progress_slider = ft.Slider(
            min=0,
            max=100,
            value=0,
            width=520,
            active_color=accent,
            secondary_active_color="#3b82f6",
            inactive_color=line,
            secondary_track_value=0,
            on_change_start=lambda _event: begin_progress_drag(),
            on_change=lambda event: preview_progress_drag(event),
            on_change_end=lambda event: finish_progress_drag(event),
        )

        def clamp(value: float, minimum: float, maximum: float) -> float:
            return max(minimum, min(maximum, value))

        def resize_progress(_event: object | None = None) -> None:
            page_width = float(getattr(page, "width", 1180) or 1180)
            progress_slider.width = clamp(page_width - 760, 360, 760)
            safe_update()

        def button(label: str, handler: Any, primary: bool = False, danger_button: bool = False) -> Any:
            return ft.Button(
                ft.Text(label, size=16, no_wrap=True, text_align=ft.TextAlign.CENTER),
                on_click=lambda _event: handler(),
                bgcolor=accent if primary else "#1e293b",
                color="#04100a" if primary else ("#fecdd3" if danger_button else text),
                elevation=0,
                height=46,
            )

        search_button = button("搜索", lambda: search(), primary=True)
        settings_button = button("设置", lambda: open_settings())
        new_playlist_button = button("新建歌单", lambda: create_playlist_action())
        rename_playlist_button = button("改名歌单", lambda: rename_playlist_action())
        delete_playlist_button = button("删除歌单", lambda: delete_playlist_action(), danger_button=True)
        add_song_button = button("加入歌单", lambda: add_song_to_playlist_action())
        remove_song_button = button("移除", lambda: remove_song_from_playlist_action(), danger_button=True)
        download_song_button = button("下载", lambda: download_song_action())
        prev_button = button("上一首", lambda: play_previous())
        play_button = button("播放", lambda: toggle_play_pause(), primary=True)
        next_button = button("下一首", lambda: play_next())

        def card(content: Any, expand: Any = None, padding: int = 16) -> Any:
            return ft.Container(
                content=content,
                expand=expand,
                padding=padding,
                bgcolor=panel,
                border=border_all(line),
                border_radius=8,
            )

        def clean_image_url(url: str) -> str:
            url = (url or "").strip()
            if url.startswith("//"):
                return f"https:{url}"
            return url

        def first_string(*values: Any) -> str:
            for value in values:
                if value:
                    return str(value)
            return ""

        def qq_album_cover(album_mid: str) -> str:
            album_mid = album_mid.strip()
            if not album_mid:
                return ""
            return f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg"

        def song_cover_url(song: Song) -> str:
            raw = song.raw or {}
            direct = first_string(raw.get("picUrl"), raw.get("picurl"), raw.get("cover"), raw.get("coverUrl"), raw.get("imgurl"))
            if direct:
                return clean_image_url(direct)
            album_value = raw.get("al") or raw.get("album") or {}
            if isinstance(album_value, dict):
                direct = first_string(album_value.get("picUrl"), album_value.get("picurl"), album_value.get("cover"), album_value.get("coverUrl"))
                if direct:
                    return clean_image_url(direct)
                album_mid = first_string(album_value.get("pmid"), album_value.get("mid"), album_value.get("albummid"))
                if album_mid:
                    return qq_album_cover(album_mid)
            album_mid = first_string(raw.get("albummid"), raw.get("albumMid"), raw.get("album_mid"), raw.get("pmid"))
            if album_mid:
                return qq_album_cover(album_mid)
            return ""

        def playlist_cover_url(playlist: Playlist) -> str:
            return clean_image_url(playlist.cover)

        def cover_placeholder(label: str) -> Any:
            return ft.Icon(ft.Icons.MUSIC_NOTE, color="#ffffff", size=24)

        def reset_hero_cover() -> None:
            hero_cover.bgcolor = "#0f172a"
            hero_cover.border = border_all(line)
            hero_cover.content = ft.Icon(ft.Icons.MUSIC_NOTE, color="#ffffff", size=54)

        def set_cover_image(control: Any, url: str, label: str) -> None:
            control.bgcolor = "#020617"
            control.border = None
            control.content = ft.Image(
                src=url,
                width=42,
                height=42,
                fit=ft.BoxFit.COVER,
                border_radius=8,
                error_content=cover_placeholder(label),
            )

        def set_hero_cover(url: str = "") -> None:
            url = clean_image_url(url)
            if not url:
                reset_hero_cover()
                return
            hero_cover.bgcolor = "#020617"
            hero_cover.border = None
            hero_cover.content = ft.Image(
                src=url,
                width=120,
                height=120,
                fit=ft.BoxFit.COVER,
                border_radius=8,
                error_content=ft.Icon(ft.Icons.MUSIC_NOTE, color="#ffffff", size=54),
            )

        def schedule_cover_sync() -> None:
            nonlocal cover_sync_running
            if cover_sync_running or not pending_cover_urls:
                return
            cover_sync_running = True

            def worker() -> None:
                nonlocal cover_sync_running
                while pending_cover_urls:
                    batch = dict(list(pending_cover_urls.items())[:24])
                    for key in batch:
                        pending_cover_urls.pop(key, None)
                    for key, url in batch.items():
                        cover_cache[key] = url
                        for control in cover_controls.get(key, []):
                            set_cover_image(control, url, str(getattr(control, "data", "") or "M"))
                    safe_update()
                    time.sleep(0.03)
                cover_sync_running = False

            page.run_thread(worker)

        def mini_cover(label: str = "M", cover_key: str = "", cover_url: str = "") -> Any:
            cover_key = cover_key or label
            cover_url = clean_image_url(cover_url)
            cover_controls.setdefault(cover_key, [])
            control = ft.Container(
                width=42,
                height=42,
                border_radius=8,
                bgcolor="#0f172a",
                border=border_all(line),
                alignment=ft.Alignment(0, 0),
                content=cover_placeholder(label),
                data=label,
            )
            cover_controls[cover_key].append(control)
            if cover_url:
                cached_url = cover_cache.get(cover_key)
                if cached_url:
                    set_cover_image(control, cached_url, label)
                else:
                    pending_cover_urls[cover_key] = cover_url
            return control

        def set_cover_for_key(cover_key: str, cover_url: str) -> None:
            cover_url = clean_image_url(cover_url)
            if not cover_url:
                return
            cover_cache[cover_key] = cover_url
            pending_cover_urls.pop(cover_key, None)
            for control in cover_controls.get(cover_key, []):
                set_cover_image(control, cover_url, str(getattr(control, "data", "") or "M"))

        def find_first_song_cover(playlist: Playlist, generation: int) -> str:
            offset = 0
            page_size = PLAYLIST_INITIAL_PAGE_SIZE
            while generation == playlist_fallback_cover_generation:
                _name, page_songs, total = api.playlist_songs_page(playlist.id, auth.get("cookie", ""), offset, page_size)
                for song in page_songs:
                    cover_url = song_cover_url(song)
                    if cover_url:
                        return cover_url
                offset += len(page_songs)
                if not page_songs or (len(page_songs) < page_size and offset >= total):
                    return ""
                page_size = PLAYLIST_BACKGROUND_PAGE_SIZE
            return ""

        def schedule_playlist_fallback_cover(playlist: Playlist, cover_key: str) -> None:
            if playlist.id == "__downloads__" or playlist_cover_url(playlist) or cover_key in playlist_fallback_cover_done or cover_key in playlist_fallback_cover_pending:
                return
            playlist_fallback_cover_pending.add(cover_key)
            generation = playlist_fallback_cover_generation

            def worker() -> None:
                try:
                    cover_url = find_first_song_cover(playlist, generation)
                except Exception:
                    cover_url = ""
                playlist_fallback_cover_pending.discard(cover_key)
                playlist_fallback_cover_done.add(cover_key)
                if cover_url and generation == playlist_fallback_cover_generation:
                    playlist.cover = cover_url
                    set_cover_for_key(cover_key, cover_url)
                    safe_update()

            page.run_thread(worker)

        def safe_update() -> None:
            try:
                page.update()
            except Exception:
                pass

        def set_status(message: str) -> None:
            status_text.value = message
            safe_update()

        def show_message(title: str, message: str) -> None:
            dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text(title),
                content=ft.Text(message, selectable=True),
                actions=[ft.TextButton("确定", on_click=lambda _event: (page.pop_dialog(), safe_update()))],
            )
            page.show_dialog(dialog)
            safe_update()

        def ask_text(title: str, label: str, initial: str = "", multiline: bool = False, on_submit: Any | None = None) -> None:
            field = ft.TextField(value=initial, label=label, multiline=multiline, min_lines=5 if multiline else None, max_lines=8 if multiline else 1)

            def submit(_event: object | None = None) -> None:
                value = field.value.strip()
                page.pop_dialog()
                safe_update()
                if on_submit:
                    on_submit(value)

            page.show_dialog(
                ft.AlertDialog(
                    modal=True,
                    title=ft.Text(title),
                    content=field,
                    actions=[
                        ft.TextButton("取消", on_click=lambda _event: (page.pop_dialog(), safe_update())),
                        ft.TextButton("确定", on_click=submit),
                    ],
                )
            )
            safe_update()

        def confirm(title: str, message: str, on_yes: Any) -> None:
            page.show_dialog(
                ft.AlertDialog(
                    modal=True,
                    title=ft.Text(title),
                    content=ft.Text(message),
                    actions=[
                        ft.TextButton("取消", on_click=lambda _event: (page.pop_dialog(), safe_update())),
                        ft.TextButton("确定", on_click=lambda _event: (page.pop_dialog(), safe_update(), on_yes())),
                    ],
                )
            )
            safe_update()

        def run_worker(work: Any, busy_message: str = "") -> None:
            if busy_message:
                set_status(busy_message)
            page.run_thread(work)

        def current_duration_ms() -> int | None:
            duration = player.duration_ms()
            if duration:
                return duration
            if 0 <= current_song_index < len(songs):
                song_duration = songs[current_song_index].duration
                if song_duration:
                    return int(song_duration) * 1000
            return None

        def current_position_ms() -> int | None:
            if user_stopped:
                return paused_position_ms
            position = player.position_ms()
            if position is None and playback_started_at:
                position = int((time.monotonic() - playback_started_at) * 1000)
            duration = current_duration_ms()
            if position is not None and duration:
                return min(position, duration)
            return position

        def known_buffered_ms() -> int | None:
            duration = current_duration_ms()
            if 0 <= current_song_index < len(songs) and songs[current_song_index].local_path and duration:
                return duration
            buffered = player.buffered_ms()
            if buffered is None:
                return None
            if duration:
                return max(0, min(buffered, duration))
            return buffered

        def current_buffered_ms() -> int | None:
            buffered = known_buffered_ms()
            return buffered if buffered is not None else current_position_ms()

        def selected_song() -> Song | None:
            if 0 <= current_song_index < len(songs):
                return songs[current_song_index]
            return songs[0] if songs else None

        def selected_playlist() -> Playlist | None:
            return current_playlist

        def save_quality() -> None:
            settings["quality"] = quality_dropdown.value
            save_settings(settings)
            set_status("默认音质已保存")

        def save_play_mode() -> None:
            settings["play_mode"] = play_mode_dropdown.value
            save_settings(settings)
            set_status("播放模式已保存")

        def supports_playlist_write() -> bool:
            return current_platform in {"qqmusic", "netease"}

        def playlist_can_write(playlist: Playlist | None) -> bool:
            if not playlist:
                return False
            if current_platform == "netease":
                return is_netease_editable_playlist(playlist, auth.get("qq_number", ""))
            return not is_builtin_playlist(playlist)

        def playlist_can_add(playlist: Playlist | None) -> bool:
            if not playlist:
                return False
            if current_platform == "netease":
                return is_netease_addable_playlist(playlist, auth.get("qq_number", ""))
            return is_addable_playlist(playlist)

        def playlist_can_remove_song(playlist: Playlist | None) -> bool:
            if not playlist:
                return False
            if current_platform == "netease":
                return is_netease_song_removable_playlist(playlist, auth.get("qq_number", ""))
            return playlist_can_write(playlist)

        def refresh_account_label() -> None:
            qq_number = auth.get("qq_number")
            if qq_number:
                display_name = auth.get("nickname") or auth.get("username") or qq_number
                account_text.value = f"{platform_display_name(current_platform)}已登录: {display_name}"
            else:
                account_text.value = f"{platform_display_name(current_platform)}未登录"
            writable = bool(qq_number) and supports_playlist_write()
            for control in (new_playlist_button, rename_playlist_button, delete_playlist_button, add_song_button):
                control.disabled = not writable
            remove_song_button.disabled = False
            download_song_button.disabled = False
            safe_update()

        def refresh_playlist_list() -> None:
            nonlocal selected_playlist_tile_index
            playlists.clear()
            playlists.extend(remote_playlists)
            if has_downloaded_songs(str(settings.get("download_dir", DEFAULT_SETTINGS["download_dir"]))):
                playlists.append(Playlist(id="__downloads__", name="已下载的歌曲", dirid="__downloads__"))
            playlist_list.controls.clear()
            for index, playlist in enumerate(playlists):
                active = current_playlist and playlist.id == current_playlist.id
                cover_key = f"playlist:{current_platform}:{playlist.id}"
                cover_url = playlist_cover_url(playlist)
                playlist_list.controls.append(
                    ft.Container(
                        content=ft.Row(
                            [
                                mini_cover(playlist.name, cover_key, cover_url),
                                ft.Column(
                                    [
                                        ft.Text(playlist.name, color=text, weight=ft.FontWeight.BOLD, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                        ft.Text(f"{playlist.song_count or 0} 首" if playlist.song_count is not None else "歌单", color=muted, size=12),
                                    ],
                                    spacing=3,
                                    expand=True,
                                ),
                            ],
                            spacing=10,
                        ),
                        padding=9,
                        border_radius=8,
                        border=border_all(accent_2 if active else "#00000000"),
                        bgcolor="#1e293b" if active else "#0f172a",
                        on_click=lambda _event, selected=index: load_playlist_at(selected),
                    )
                )
                if not cover_url:
                    schedule_playlist_fallback_cover(playlist, cover_key)
            selected_playlist_tile_index = next((index for index, playlist in enumerate(playlists) if current_playlist and playlist.id == current_playlist.id), -1)
            safe_update()
            schedule_cover_sync()

        def apply_playlist_tile_style(index: int, active: bool) -> None:
            if not (0 <= index < len(playlist_list.controls)):
                return
            tile = playlist_list.controls[index]
            if not hasattr(tile, "border") or not hasattr(tile, "bgcolor"):
                return
            tile.border = border_all(accent_2 if active else "#00000000")
            tile.bgcolor = "#1e293b" if active else "#0f172a"

        def update_playlist_selection(previous: int, current: int) -> None:
            nonlocal selected_playlist_tile_index
            if previous != current:
                apply_playlist_tile_style(previous, False)
            apply_playlist_tile_style(current, True)
            selected_playlist_tile_index = current
            safe_update()

        def song_queue_display(song: Song) -> tuple[str, str]:
            meta_parts = []
            if settings.get("queue_show_singers", True) and song.singers:
                meta_parts.append(song.singers)
            if settings.get("queue_show_album", True) and song.album:
                meta_parts.append(song.album)
            if settings.get("queue_show_duration", True) and song.duration:
                meta_parts.append(format_seconds(song.duration))
            if settings.get("queue_show_mid", False) and song.mid:
                meta_parts.append(f"MID: {song.mid}")
            return song.title, " · ".join(meta_parts)

        def apply_song_tile_style(index: int, active: bool) -> None:
            if not (0 <= index < len(song_list.controls)):
                return
            tile = song_list.controls[index]
            if not hasattr(tile, "border") or not hasattr(tile, "bgcolor"):
                return
            tile.border = border_all(accent_2 if active else "#00000000")
            tile.bgcolor = "#1e293b" if active else "#0f172a"

        def update_song_selection(previous: int, current: int) -> None:
            nonlocal selected_song_tile_index
            if previous != current:
                apply_song_tile_style(previous, False)
            apply_song_tile_style(current, True)
            selected_song_tile_index = current
            queue_text.value = f"{current + 1 if current >= 0 else 0} / {len(songs)}"
            safe_update()

        def build_song_tile(index: int, song: Song) -> Any:
            title, meta = song_queue_display(song)
            active = index == current_song_index
            cover_key = f"song:{current_platform}:{song_identity(song)}"
            return ft.Container(
                content=ft.Row(
                    [
                        mini_cover(song.title, cover_key, song_cover_url(song)),
                        ft.Column(
                            [
                                ft.Text(title, color=text, weight=ft.FontWeight.BOLD, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                ft.Text(meta or "未知歌手", color=muted, size=12, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ],
                            spacing=3,
                            expand=True,
                        ),
                        ft.Text(f"{index + 1:02d}", color=muted, size=12, width=36, text_align=ft.TextAlign.RIGHT),
                    ],
                    spacing=10,
                ),
                padding=10,
                border_radius=8,
                border=border_all(accent_2 if active else "#00000000"),
                bgcolor="#1e293b" if active else "#0f172a",
                on_click=lambda _event, selected=index: click_song(selected),
                on_long_press=lambda _event, selected=index: play_song_at(selected),
            )

        def refresh_song_list() -> None:
            nonlocal selected_song_tile_index
            song_list.controls.clear()
            for index, song in enumerate(songs):
                song_list.controls.append(build_song_tile(index, song))
            selected_song_tile_index = current_song_index
            queue_text.value = f"{current_song_index + 1 if current_song_index >= 0 else 0} / {len(songs)}"
            safe_update()
            schedule_cover_sync()

        def append_song_items(results: list[Song]) -> None:
            start = len(songs)
            songs.extend(results)
            for offset, song in enumerate(results):
                song_list.controls.append(build_song_tile(start + offset, song))
            queue_text.value = f"{current_song_index + 1 if current_song_index >= 0 else 0} / {len(songs)}"
            safe_update()
            schedule_cover_sync()

        def song_identity(song: Song) -> str:
            return song.mid or song.song_id or song.local_path or song.title

        def clear_lyric_preview() -> None:
            nonlocal displayed_lyric_key, current_lyric_entries, current_lyric_index
            displayed_lyric_key = ""
            current_lyric_entries = []
            current_lyric_index = -1
            lyric_preview_text.value = ""

        def lyric_index_for_position(position_ms: int | None) -> int:
            if position_ms is None or not current_lyric_entries:
                return -1
            active = -1
            for index, (line_time_ms, _line) in enumerate(current_lyric_entries):
                if line_time_ms > position_ms + 350:
                    break
                active = index
            return active

        def update_lyric_preview(position_ms: int | None, force: bool = False) -> None:
            nonlocal current_lyric_index
            index = lyric_index_for_position(position_ms)
            if not force and index == current_lyric_index:
                return
            current_lyric_index = index
            lyric_preview_text.value = current_lyric_entries[index][1] if index >= 0 else ""

        def render_lyrics(raw_text: str, song_key: str = "") -> None:
            nonlocal displayed_lyric_key, current_lyric_entries, current_lyric_index
            lyric_list.controls.clear()
            parsed = parse_lrc_lines(raw_text)
            current_lyric_entries = parsed
            current_lyric_index = -1
            lines = [line for _time_ms, line in parsed] if parsed else (strip_lrc_timestamps(raw_text) or "没有返回歌词").splitlines()
            for line in lines:
                lyric_list.controls.append(ft.Text(line, color="#bfdbfe", size=14, selectable=True))
            if song_key:
                displayed_lyric_key = song_key
            update_lyric_preview(current_position_ms(), force=True)
            safe_update()

        def show_song(song: Song, load_lyrics: bool = True) -> None:
            song_key = song_identity(song)
            parts = [
                f"歌曲: {song.title}",
                f"歌手: {song.singers}" if song.singers else "",
                f"专辑: {song.album}" if song.album else "",
                f"时长: {format_seconds(song.duration)}" if song.duration else "",
                f"MID: {song.mid}",
            ]
            song_info.value = "\n".join(part for part in parts if part)
            if not load_lyrics:
                if displayed_lyric_key != song_key:
                    lyric_list.controls[:] = [ft.Text("播放时加载歌词", color=muted)]
                safe_update()
                return
            if displayed_lyric_key == song_key:
                safe_update()
                return
            lyric_list.controls[:] = [ft.Text("正在加载歌词...", color=muted)]
            safe_update()

            def worker() -> None:
                try:
                    text_value = api.lyric(song.mid, auth.get("cookie", "")) or "没有返回歌词"
                    render_lyrics(text_value, song_key)
                except Exception as exc:
                    clear_lyric_preview()
                    lyric_list.controls[:] = [ft.Text(str(exc), color=danger, selectable=True)]
                    safe_update()

            run_worker(worker)

        def select_song(index: int) -> None:
            nonlocal current_song_index
            if not (0 <= index < len(songs)):
                return
            previous = current_song_index
            current_song_index = index
            update_song_selection(previous, index)
            show_song(songs[index], load_lyrics=False)

        def click_song(index: int) -> None:
            nonlocal last_song_click_index, last_song_click_at
            now = time.monotonic()
            if last_song_click_index == index and now - last_song_click_at <= 0.45:
                last_song_click_index = -1
                last_song_click_at = 0.0
                play_song_at(index)
                return
            last_song_click_index = index
            last_song_click_at = now
            select_song(index)

        def replace_songs(results: list[Song], message: str) -> None:
            nonlocal current_song_index, playback_token, user_stopped
            playback_token += 1
            user_stopped = True
            clear_lyric_preview()
            current_song_index = 0 if results else -1
            songs.clear()
            songs.extend(results)
            if results:
                show_song(results[0], load_lyrics=False)
            else:
                song_info.value = "没有歌曲"
                lyric_list.controls.clear()
            refresh_song_list()
            set_status(message)

        def append_songs(results: list[Song], message: str) -> None:
            if not results:
                set_status(message)
                return
            append_song_items(results)
            set_status(message)

        def update_playlist_cover_from_songs(playlist: Playlist, results: list[Song]) -> None:
            if playlist.id == "__downloads__" or playlist_cover_url(playlist):
                return
            for song in results:
                cover_url = song_cover_url(song)
                if cover_url:
                    playlist.cover = cover_url
                    cover_key = f"playlist:{current_platform}:{playlist.id}"
                    set_cover_for_key(cover_key, cover_url)
                    safe_update()
                    return

        def clear_song_queue(message: str) -> None:
            nonlocal current_song_index, playback_token, user_stopped
            playback_token += 1
            user_stopped = True
            current_song_index = -1
            songs.clear()
            clear_lyric_preview()
            song_info.value = message
            lyric_list.controls.clear()
            song_list.controls[:] = [
                ft.Container(
                    content=ft.Text(message, color=muted),
                    padding=12,
                    border_radius=8,
                    bgcolor="#0f172a",
                )
            ]
            queue_text.value = "0 / 0"
            set_status(message)

        def search() -> None:
            nonlocal current_playlist, playlist_load_token
            keyword = keyword_field.value.strip()
            if not keyword:
                show_message("提示", "先输入歌曲名或歌手")
                return
            playlist_load_token += 1
            current_playlist = None
            search_platform = current_platform
            search_api = build_music_api(search_platform, api_base, timeout)
            search_cookie = auth.get("cookie", "")
            songs.clear()
            refresh_song_list()

            def worker() -> None:
                try:
                    results = search_api.search(keyword, cookie=search_cookie)
                except Exception as exc:
                    show_message("搜索失败", str(exc))
                    set_status("搜索失败")
                    return
                if search_platform != current_platform:
                    set_status(f"已切换到 {platform_display_name(current_platform)}，忽略旧搜索结果")
                    return
                replace_songs(results, f"{platform_display_name(search_platform)}找到 {len(results)} 首")

            run_worker(worker, f"正在搜索 {platform_display_name(search_platform)}...")

        def switch_platform(new_platform: str) -> None:
            nonlocal api, auth, current_platform, current_playlist, playback_token, playlist_load_token, playlist_fallback_cover_generation, user_stopped, current_song_index, displayed_lyric_key
            new_platform = normalize_platform(new_platform)
            if new_platform == current_platform:
                return
            player.stop()
            playback_token += 1
            playlist_load_token += 1
            playlist_fallback_cover_generation += 1
            user_stopped = True
            current_platform = new_platform
            settings["platform"] = new_platform
            save_settings(settings)
            api = build_music_api(new_platform, api_base, timeout)
            auth = load_auth(new_platform)
            current_playlist = None
            current_song_index = -1
            displayed_lyric_key = ""
            remote_playlists.clear()
            songs.clear()
            player_title.value = "未播放"
            player_meta.value = "选择歌曲后点击播放"
            reset_hero_cover()
            song_info.value = "搜索歌曲后选择一项"
            lyric_list.controls.clear()
            refresh_song_list()
            refresh_playlist_list()
            refresh_account_label()
            page.title = f"{platform_display_name(new_platform)} music_for_all_system(developed_by_Linux_Mint)"
            set_status(f"已切换到 {platform_display_name(new_platform)}")
            if auth.get("qq_number") and settings.get("auto_sync_playlists", True):
                sync_playlists()

        def start_login_action() -> None:
            if auth.get("qq_number"):
                logout()
            elif current_platform == "netease":
                show_netease_login_dialog()
            else:
                show_qq_login_dialog()

        def show_qq_login_dialog() -> None:
            def start_provider(provider: str) -> None:
                page.pop_dialog()
                safe_update()
                login(provider)

            page.show_dialog(
                ft.AlertDialog(
                    modal=True,
                    title=ft.Text("选择登录方式"),
                    content=ft.Row(
                        [
                            ft.Button("QQ 登录", on_click=lambda _event: start_provider("qq")),
                            ft.Button("微信登录", on_click=lambda _event: start_provider("wechat")),
                        ],
                        spacing=10,
                    ),
                    actions=[ft.TextButton("取消", on_click=lambda _event: (page.pop_dialog(), safe_update()))],
                )
            )
            safe_update()

        def login(provider: str = "qq") -> None:
            provider_name = "微信" if provider == "wechat" else "QQ"

            def worker() -> None:
                try:
                    session = api.start_wx_qr_login() if provider == "wechat" else api.start_qr_login()
                except Exception as exc:
                    show_message("登录失败", str(exc))
                    set_status("获取二维码失败")
                    return
                status = ft.Text(f"等待{provider_name}扫码...", color=muted)
                dialog = ft.AlertDialog(
                    modal=True,
                    title=ft.Text(f"使用{provider_name}扫码登录"),
                    content=ft.Column(
                        [
                            ft.Image(src=session.image, width=220, height=220),
                            status,
                        ],
                        tight=True,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    actions=[ft.TextButton("取消", on_click=lambda _event: (page.pop_dialog(), safe_update()))],
                )
                page.show_dialog(dialog)
                safe_update()
                deadline = time.time() + 180
                while time.time() < deadline:
                    try:
                        state, result = api.poll_wx_qr_login(session) if provider == "wechat" else api.poll_qr_login(session)
                    except Exception as exc:
                        show_message("登录失败", str(exc))
                        set_status("登录失败")
                        return
                    if state == "done" and result:
                        save_auth(result.qq_number, result.cookie, current_platform, result.nickname)
                        auth.clear()
                        auth.update(load_auth(current_platform))
                        page.pop_dialog()
                        refresh_account_label()
                        set_status(f"已登录: {result.nickname or result.qq_number}")
                        sync_playlists()
                        return
                    if state == "confirming":
                        status.value = "已扫码，等待手机确认..."
                    elif state == "expired":
                        page.pop_dialog()
                        show_message("登录", "二维码已过期，请重新点击登录")
                        set_status("二维码已过期")
                        return
                    safe_update()
                    time.sleep(2)
                page.pop_dialog()
                show_message("登录", "登录超时，请重新点击登录")
                set_status("登录超时")

            run_worker(worker, f"正在获取{provider_name}登录二维码...")

        def show_netease_login_dialog() -> None:
            phone = ft.TextField(label="手机号", width=260)
            country = ft.TextField(label="区号", value="86", width=90)
            captcha = ft.TextField(label="短信验证码", width=180)
            status = ft.Text("", color=muted)
            captcha_sent = {"value": False}

            def send(_event: object | None = None) -> None:
                phone_value = phone.value.strip()
                country_value = country.value.strip() or "86"
                if not phone_value:
                    status.value = "请输入手机号"
                    safe_update()
                    return

                def worker() -> None:
                    try:
                        api.send_phone_captcha(phone_value, country_value)
                    except Exception as exc:
                        status.value = str(exc)
                        safe_update()
                        return
                    captcha_sent["value"] = True
                    status.value = "验证码已发送，请输入短信里的完整数字验证码"
                    safe_update()

                run_worker(worker, "正在发送验证码...")

            def submit(_event: object | None = None) -> None:
                if not captcha_sent["value"]:
                    status.value = "请先发送验证码"
                    safe_update()
                    return

                def worker() -> None:
                    try:
                        result = api.login_with_phone_captcha(phone.value.strip(), captcha.value.strip(), country.value.strip() or "86")
                        save_auth(result.qq_number, result.cookie, current_platform, result.nickname)
                    except Exception as exc:
                        status.value = str(exc)
                        safe_update()
                        return
                    auth.clear()
                    auth.update(load_auth(current_platform))
                    page.pop_dialog()
                    refresh_account_label()
                    set_status(f"已登录: {result.nickname or result.qq_number}")
                    sync_playlists()

                run_worker(worker, "正在登录...")

            def import_cookie(_event: object | None = None) -> None:
                page.pop_dialog()
                safe_update()
                ask_text("导入网易云 Cookie", "粘贴官方网页版 Cookie，至少包含 MUSIC_U", multiline=True, on_submit=submit_cookie_login)

            page.show_dialog(
                ft.AlertDialog(
                    modal=True,
                    title=ft.Text("网易云音乐登录"),
                    content=ft.Column([ft.Row([country, phone]), captcha, status], tight=True),
                    actions=[
                        ft.TextButton("发送验证码", on_click=send),
                        ft.TextButton("导入Cookie", on_click=import_cookie),
                        ft.TextButton("取消", on_click=lambda _event: (page.pop_dialog(), safe_update())),
                        ft.TextButton("登录", on_click=submit),
                    ],
                )
            )
            safe_update()

        def submit_cookie_login(cookie: str) -> None:
            if not cookie:
                show_message("提示", "请先粘贴 Cookie")
                return

            def worker() -> None:
                try:
                    result = api.login_with_cookie(cookie)
                    save_auth(result.qq_number, result.cookie, current_platform, result.nickname)
                except Exception as exc:
                    show_message("导入失败", str(exc))
                    return
                auth.clear()
                auth.update(load_auth(current_platform))
                refresh_account_label()
                set_status(f"已登录: {result.nickname or result.qq_number}")
                sync_playlists()

            run_worker(worker, "正在导入 Cookie...")

        def logout() -> None:
            nonlocal displayed_lyric_key, playlist_load_token, playlist_fallback_cover_generation
            player.stop()
            playlist_load_token += 1
            playlist_fallback_cover_generation += 1
            clear_auth(current_platform)
            auth.clear()
            displayed_lyric_key = ""
            remote_playlists.clear()
            songs.clear()
            refresh_song_list()
            refresh_playlist_list()
            refresh_account_label()
            set_status("已退出登录")

        def sync_playlists() -> None:
            nonlocal playlist_fallback_cover_generation
            qq_number = auth.get("qq_number", "")
            if not qq_number:
                show_message("提示", f"先登录 {platform_display_name(current_platform)}")
                return
            playlist_fallback_cover_generation += 1
            playlist_fallback_cover_pending.clear()
            playlist_fallback_cover_done.clear()

            def worker() -> None:
                try:
                    results = api.user_playlists(qq_number, auth.get("cookie", ""))
                except Exception as exc:
                    show_message("同步失败", str(exc))
                    set_status("同步失败")
                    return
                remote_playlists.clear()
                remote_playlists.extend(results)
                refresh_playlist_list()
                set_status(f"已同步 {len(remote_playlists)} 个歌单")

            run_worker(worker, "正在同步歌单...")

        def load_playlist_at(index: int) -> None:
            nonlocal current_playlist
            if not (0 <= index < len(playlists)):
                return
            previous = selected_playlist_tile_index
            current_playlist = playlists[index]
            update_playlist_selection(previous, index)
            load_playlist()

        def load_playlist() -> None:
            nonlocal playlist_load_token
            playlist = selected_playlist()
            if not playlist:
                return
            playlist_load_token += 1
            load_token = playlist_load_token
            if playlist.id == "__downloads__":
                replace_songs(downloaded_songs(str(settings.get("download_dir", DEFAULT_SETTINGS["download_dir"]))), "已下载的歌曲")
                return
            clear_song_queue(f"正在加载歌单: {playlist.name}")

            def worker() -> None:
                try:
                    playlist_name, results, total = api.playlist_songs_page(playlist.id, auth.get("cookie", ""), 0, PLAYLIST_INITIAL_PAGE_SIZE)
                except Exception as exc:
                    if load_token != playlist_load_token:
                        return
                    show_message("歌单加载失败", str(exc))
                    set_status("歌单加载失败")
                    return
                if load_token != playlist_load_token:
                    return
                loaded = len(results)
                update_playlist_cover_from_songs(playlist, results)
                replace_songs(results, f"{playlist_name}: 已加载 {loaded} / {total} 首")

                while load_token == playlist_load_token:
                    try:
                        page_name, page_results, page_total = api.playlist_songs_page(
                            playlist.id,
                            auth.get("cookie", ""),
                            loaded,
                            PLAYLIST_BACKGROUND_PAGE_SIZE,
                        )
                    except Exception as exc:
                        if load_token == playlist_load_token:
                            set_status(f"已加载 {loaded} / {total} 首，后续加载失败: {exc}")
                        return
                    if load_token != playlist_load_token:
                        return
                    if page_name:
                        playlist_name = page_name
                    total = max(total, page_total)
                    if not page_results:
                        break
                    update_playlist_cover_from_songs(playlist, page_results)

                    chunk_start = 0
                    while chunk_start < len(page_results) and load_token == playlist_load_token:
                        chunk = page_results[chunk_start : chunk_start + 100]
                        loaded += len(chunk)
                        append_songs(chunk, f"{playlist_name}: 已加载 {min(loaded, total)} / {total} 首")
                        chunk_start += len(chunk)
                        time.sleep(0.01)
                    if len(page_results) < PLAYLIST_BACKGROUND_PAGE_SIZE:
                        break
                    if loaded >= total:
                        total = loaded + PLAYLIST_BACKGROUND_PAGE_SIZE

                if load_token == playlist_load_token:
                    set_status(f"{playlist_name}: {len(songs)} 首")

            run_worker(worker, f"正在加载歌单: {playlist.name}")

        def require_login_cookie() -> str | None:
            cookie = auth.get("cookie", "")
            if not cookie:
                show_message("提示", "请先登录")
                return None
            return cookie

        def create_playlist_action() -> None:
            cookie = require_login_cookie()
            if not cookie:
                return

            def submit(name: str) -> None:
                if not name:
                    return

                def worker() -> None:
                    try:
                        api.create_playlist(name, cookie)
                    except Exception as exc:
                        show_message("创建失败", str(exc))
                        return
                    set_status("歌单已创建")
                    sync_playlists()

                run_worker(worker, "正在创建歌单...")

            ask_text("新建歌单", "歌单名称", on_submit=submit)

        def rename_playlist_action() -> None:
            cookie = require_login_cookie()
            playlist = selected_playlist()
            if not cookie or not playlist:
                show_message("提示", "先选择一个歌单")
                return
            if not playlist_can_write(playlist):
                show_message("提示", f"“{playlist.name}”不是可重命名的自建歌单")
                return

            def submit(name: str) -> None:
                if not name or name == playlist.name:
                    return

                def worker() -> None:
                    try:
                        api.rename_playlist(playlist.dirid or playlist.id, name, cookie)
                    except Exception as exc:
                        show_message("重命名失败", str(exc))
                        return
                    set_status("歌单已重命名")
                    sync_playlists()

                run_worker(worker, "正在重命名歌单...")

            ask_text("重命名歌单", "新名称", playlist.name, on_submit=submit)

        def delete_playlist_action() -> None:
            cookie = require_login_cookie()
            playlist = selected_playlist()
            if not cookie or not playlist:
                show_message("提示", "先选择一个歌单")
                return
            if not playlist_can_write(playlist):
                show_message("提示", f"“{playlist.name}”不是可删除的自建歌单")
                return

            def delete_it() -> None:
                def worker() -> None:
                    try:
                        api.delete_playlist(playlist.dirid or playlist.id, cookie)
                    except Exception as exc:
                        show_message("删除失败", str(exc))
                        return
                    set_status("歌单已删除")
                    sync_playlists()

                run_worker(worker, "正在删除歌单...")

            confirm("删除歌单", f"确定删除“{playlist.name}”吗？", delete_it)

        def add_song_to_playlist_action() -> None:
            song = selected_song()
            cookie = require_login_cookie()
            if not song or not cookie:
                show_message("提示", "先选择一首歌")
                return
            candidates = [playlist for playlist in remote_playlists if playlist_can_add(playlist)]
            if not candidates:
                show_message("提示", f"还没有可加入的{platform_display_name(current_platform)}自建歌单")
                return
            if len(candidates) == 1:
                add_song_to_playlist_target(song, candidates[0], cookie)
                return
            choices = ft.ListView(width=420, height=360, spacing=8)

            def choose(target: Playlist) -> None:
                page.pop_dialog()
                safe_update()
                add_song_to_playlist_target(song, target, cookie)

            for playlist in candidates:
                choices.controls.append(ft.ListTile(title=playlist.name, subtitle=playlist.display, on_click=lambda _event, target=playlist: choose(target)))
            page.show_dialog(
                ft.AlertDialog(
                    modal=True,
                    title=ft.Text(f"选择要加入的歌单：{song.title}"),
                    content=choices,
                    actions=[ft.TextButton("取消", on_click=lambda _event: (page.pop_dialog(), safe_update()))],
                )
            )
            safe_update()

        def add_song_to_playlist_target(song: Song, playlist: Playlist, cookie: str) -> None:
            def worker() -> None:
                try:
                    api.add_song_to_playlist(song.song_id, song.song_type, playlist.dirid or playlist.id, cookie, song.mid, playlist.id)
                except Exception as exc:
                    show_message("添加失败", str(exc))
                    set_status("添加失败")
                    return
                set_status(f"已加入: {playlist.name}")
                sync_playlists()

            run_worker(worker, f"正在加入歌单: {playlist.name}")

        def remove_song_from_playlist_action() -> None:
            song = selected_song()
            playlist = selected_playlist()
            cookie = require_login_cookie()
            if not song or not playlist or not cookie:
                show_message("提示", "先打开歌单并选择要移除的歌曲")
                return
            if not playlist_can_remove_song(playlist):
                show_message("提示", f"“{playlist.name}”不支持移除歌曲")
                return

            def remove_it() -> None:
                def worker() -> None:
                    try:
                        api.remove_song_from_playlist(song.song_id, song.song_type, playlist.dirid or playlist.id, cookie)
                    except Exception as exc:
                        show_message("移除失败", str(exc))
                        return
                    set_status("歌曲已移除")
                    load_playlist()

                run_worker(worker, "正在移除歌曲...")

            confirm("移除歌曲", f"从“{playlist.name}”移除“{song.title}”？", remove_it)

        def download_song_action() -> None:
            song = selected_song()
            if not song:
                show_message("提示", "先选择一首歌")
                return
            if song.local_path:
                show_message("提示", "这首歌已经是本地文件")
                return

            def worker() -> None:
                try:
                    url = api.song_url(song.mid, str(quality_dropdown.value), auth.get("cookie", ""), song.media_mid)
                    target_dir = Path(str(settings.get("download_dir", DEFAULT_SETTINGS["download_dir"]))).expanduser()
                    target_dir.mkdir(parents=True, exist_ok=True)
                    extension = guess_audio_extension(url, str(quality_dropdown.value))
                    base_name = safe_filename(f"{song.title} - {song.singers}" if song.singers else song.title)
                    target = target_dir / f"{base_name}{extension}"
                    counter = 2
                    while target.exists():
                        target = target_dir / f"{base_name} ({counter}){extension}"
                        counter += 1
                    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 music-linux-client/1.0"})
                    with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as file:
                        shutil.copyfileobj(response, file)
                except Exception as exc:
                    show_message("下载失败", str(exc))
                    set_status("下载失败")
                    return
                set_status(f"已下载: {song.title}")
                refresh_playlist_list()

            run_worker(worker, f"正在下载: {song.title}")

        def next_index_for_mode(auto: bool = False, reverse: bool = False) -> int:
            if not songs:
                return -1
            mode = str(play_mode_dropdown.value)
            index = current_song_index if current_song_index >= 0 else 0
            if mode == "单曲循环":
                return index
            if mode == "随机播放":
                if len(songs) == 1:
                    return index
                choices = [candidate for candidate in range(len(songs)) if candidate != index]
                return random.choice(choices)
            if reverse:
                return index - 1 if index > 0 else len(songs) - 1
            next_index = index + 1
            if next_index < len(songs):
                return next_index
            return -1 if auto else 0

        def monitor_playback(token: int) -> None:
            time.sleep(4)
            while token == playback_token and not user_stopped:
                if player.has_ended():
                    auto_advance(token)
                    return
                time.sleep(1)

        def auto_advance(token: int) -> None:
            if token != playback_token or user_stopped:
                return
            next_index = next_index_for_mode(auto=True)
            if next_index < 0:
                player_meta.value = "播放完成"
                progress_slider.value = 100
                progress_slider.secondary_track_value = 100
                play_button.content.value = "播放"
                reset_hero_cover()
                set_status("播放完成")
                return
            play_song_at(next_index)

        def set_progress_values(position: int | None, duration: int | None) -> None:
            if duration and position is not None:
                progress_slider.value = max(0, min(100, position * 100 / duration))
                buffered = current_buffered_ms()
                if buffered is not None:
                    progress_slider.secondary_track_value = max(progress_slider.value or 0, min(100, buffered * 100 / duration))
                else:
                    progress_slider.secondary_track_value = progress_slider.value
            elif user_stopped:
                progress_slider.value = 0
                progress_slider.secondary_track_value = 0

        def refresh_playback_progress() -> None:
            if progress_dragging:
                return
            duration = current_duration_ms()
            position = current_position_ms()
            duration_text.value = format_milliseconds(duration)
            elapsed_text.value = format_milliseconds(position)
            set_progress_values(position, duration)
            update_lyric_preview(position)
            safe_update()

        def drive_playback_progress(token: int) -> None:
            while token == playback_token and not user_stopped:
                refresh_playback_progress()
                time.sleep(0.25)
            refresh_playback_progress()

        def play_song_at(index: int, start_position_ms: int = 0) -> None:
            nonlocal current_song_index, playback_token, user_stopped, playback_started_at, paused_position_ms, pending_buffer_resume_ms
            if not (0 <= index < len(songs)):
                show_message("提示", "先选择一首歌")
                return
            song = songs[index]
            previous_index = current_song_index
            playback_token += 1
            token = playback_token
            current_song_index = index
            user_stopped = False
            pending_buffer_resume_ms = None
            paused_position_ms = max(0, int(start_position_ms))
            play_button.content.value = "暂停"
            player_title.value = song.title
            player_meta.value = song.singers or song.album or "正在获取播放链接"
            set_hero_cover(song_cover_url(song))
            duration = song.duration * 1000 if song.duration else None
            set_progress_values(paused_position_ms, duration)
            elapsed_text.value = format_milliseconds(paused_position_ms)
            duration_text.value = format_milliseconds(duration)
            update_lyric_preview(paused_position_ms, force=True)
            show_song(song, load_lyrics=False)
            update_song_selection(previous_index, index)

            def worker() -> None:
                nonlocal playback_started_at
                try:
                    url = song.local_path or api.song_url(song.mid, str(quality_dropdown.value), auth.get("cookie", ""), song.media_mid)
                    player.play(url, paused_position_ms)
                    if paused_position_ms:
                        player.seek_ms(paused_position_ms)
                except Exception as exc:
                    play_button.content.value = "播放"
                    show_message("播放失败", str(exc))
                    set_status("播放失败")
                    return
                playback_started_at = time.monotonic() - (paused_position_ms / 1000)
                player_meta.value = song.singers or song.album or "正在播放"
                set_status("正在播放")
                page.run_thread(lambda: drive_playback_progress(token))
                page.run_thread(lambda: monitor_playback(token))
                if displayed_lyric_key != song_identity(song):
                    show_song(song, load_lyrics=True)

            run_worker(worker, "正在获取播放链接...")

        def play_selected() -> None:
            index = current_song_index if current_song_index >= 0 else (0 if songs else -1)
            if user_stopped and index == current_song_index and paused_position_ms:
                resume_paused_song()
                return
            play_song_at(index)

        def resume_paused_song() -> None:
            nonlocal playback_token, user_stopped, playback_started_at, pending_buffer_resume_ms
            if not (user_stopped and paused_position_ms and 0 <= current_song_index < len(songs)):
                play_selected()
                return
            if not player.resume():
                play_song_at(current_song_index, paused_position_ms)
                return
            playback_token += 1
            token = playback_token
            user_stopped = False
            pending_buffer_resume_ms = None
            playback_started_at = time.monotonic() - (paused_position_ms / 1000)
            play_button.content.value = "暂停"
            player_meta.value = "继续"
            set_status("继续")
            page.run_thread(lambda: drive_playback_progress(token))
            page.run_thread(lambda: monitor_playback(token))

        def play_previous() -> None:
            if not songs:
                show_message("提示", "当前没有歌曲队列")
                return
            play_song_at(next_index_for_mode(reverse=True))

        def play_next() -> None:
            if not songs:
                show_message("提示", "当前没有歌曲队列")
                return
            next_index = next_index_for_mode()
            if next_index >= 0:
                play_song_at(next_index)

        def stop() -> None:
            nonlocal playback_token, user_stopped, playback_started_at, paused_position_ms, pending_buffer_resume_ms
            playback_token += 1
            position = current_position_ms()
            if position is not None:
                paused_position_ms = position
            user_stopped = True
            pending_buffer_resume_ms = None
            playback_started_at = 0.0
            player.pause()
            player_meta.value = "已暂停"
            play_button.content.value = "播放"
            elapsed_text.value = format_milliseconds(paused_position_ms)
            duration = current_duration_ms()
            set_progress_values(paused_position_ms, duration)
            update_lyric_preview(paused_position_ms)
            set_status("已暂停")

        def toggle_play_pause() -> None:
            if user_stopped:
                play_selected()
            else:
                stop()

        def cleanup_playback(_event: object | None = None) -> None:
            nonlocal playback_token, user_stopped, playback_started_at, pending_buffer_resume_ms
            playback_token += 1
            user_stopped = True
            pending_buffer_resume_ms = None
            playback_started_at = 0.0
            player.stop()

        page.on_close = cleanup_playback
        page.on_disconnect = cleanup_playback

        def begin_progress_drag() -> None:
            nonlocal progress_dragging
            progress_dragging = True

        def slider_event_value(event: object | None = None) -> float:
            candidates = [
                getattr(getattr(event, "control", None), "value", None),
                getattr(event, "data", None),
                progress_slider.value,
            ]
            for candidate in candidates:
                try:
                    return max(0.0, min(100.0, float(candidate)))
                except (TypeError, ValueError):
                    continue
            return 0.0

        def progress_value_to_ms(value: float | None = None) -> int | None:
            duration = current_duration_ms()
            if not duration:
                return None
            slider_value = slider_event_value() if value is None else max(0.0, min(100.0, float(value)))
            return int(duration * slider_value / 100)

        def preview_progress_drag(event: object | None = None) -> None:
            if not progress_dragging:
                return
            value = slider_event_value(event)
            progress_slider.value = value
            position = progress_value_to_ms(value)
            elapsed_text.value = format_milliseconds(position)
            progress_slider.label = format_milliseconds(position)
            update_lyric_preview(position, force=True)
            safe_update()

        def wait_for_buffer_then_resume(token: int, target_position_ms: int) -> None:
            nonlocal playback_started_at, user_stopped, pending_buffer_resume_ms
            while token == playback_token and pending_buffer_resume_ms == target_position_ms:
                buffered = current_buffered_ms()
                if buffered is None or buffered + 500 >= target_position_ms:
                    if player.seek_ms(target_position_ms):
                        playback_started_at = time.monotonic() - (target_position_ms / 1000)
                    player.resume()
                    user_stopped = False
                    pending_buffer_resume_ms = None
                    play_button.content.value = "暂停"
                    set_status("正在播放")
                    page.run_thread(lambda: drive_playback_progress(token))
                    page.run_thread(lambda: monitor_playback(token))
                    return
                elapsed_text.value = format_milliseconds(target_position_ms)
                set_progress_values(target_position_ms, current_duration_ms())
                safe_update()
                time.sleep(0.3)

        def finish_progress_drag(event: object | None = None) -> None:
            nonlocal playback_started_at, paused_position_ms, progress_dragging, user_stopped, playback_token, pending_buffer_resume_ms
            value = slider_event_value(event)
            progress_slider.value = value
            position = progress_value_to_ms(value)
            progress_dragging = False
            progress_slider.label = None
            if position is None:
                return
            paused_position_ms = position
            buffered = known_buffered_ms()
            duration = current_duration_ms()
            if buffered is not None and duration and buffered + 500 < position:
                playback_token += 1
                token = playback_token
                pending_buffer_resume_ms = position
                user_stopped = True
                player.pause()
                play_button.content.value = "播放"
                player_meta.value = "等待缓冲"
                elapsed_text.value = format_milliseconds(position)
                set_progress_values(position, duration)
                update_lyric_preview(position, force=True)
                set_status("等待缓冲")
                page.run_thread(lambda: wait_for_buffer_then_resume(token, position))
            elif player.seek_ms(position):
                playback_started_at = time.monotonic() - (position / 1000)
                elapsed_text.value = format_milliseconds(position)
                set_progress_values(position, duration)
                update_lyric_preview(position, force=True)
            else:
                player_meta.value = "当前播放器不支持拖动进度"
            safe_update()

        def open_settings() -> None:
            show_singers = ft.Checkbox(label="显示歌手", value=bool(settings.get("queue_show_singers", True)))
            show_album = ft.Checkbox(label="显示专辑名", value=bool(settings.get("queue_show_album", True)))
            show_duration = ft.Checkbox(label="显示歌曲时长", value=bool(settings.get("queue_show_duration", True)))
            show_mid = ft.Checkbox(label="显示 MID", value=bool(settings.get("queue_show_mid", False)))
            auto_sync = ft.Checkbox(label="启动时自动同步歌单", value=bool(settings.get("auto_sync_playlists", True)))
            font_size = ft.Dropdown(
                value=str(settings.get("queue_font_size", 11)),
                options=[option(str(size), str(size)) for size in (10, 11, 12, 13, 14)],
                width=100,
            )
            download_dir = ft.TextField(label="下载目录", value=str(settings.get("download_dir", DEFAULT_SETTINGS["download_dir"])), width=420)
            account_status = ft.Text(account_text.value, color=accent, weight=ft.FontWeight.BOLD)

            def close_then(action: Any) -> None:
                page.pop_dialog()
                safe_update()
                action()

            def apply(_event: object | None = None) -> None:
                settings.update(
                    {
                        "queue_show_singers": show_singers.value,
                        "queue_show_album": show_album.value,
                        "queue_show_duration": show_duration.value,
                        "queue_show_mid": show_mid.value,
                        "queue_font_size": int(font_size.value),
                        "auto_sync_playlists": auto_sync.value,
                        "download_dir": download_dir.value.strip() or str(DEFAULT_SETTINGS["download_dir"]),
                    }
                )
                save_settings(settings)
                refresh_song_list()
                page.pop_dialog()
                set_status("设置已保存")

            page.show_dialog(
                ft.AlertDialog(
                    modal=True,
                    title=ft.Text("设置"),
                    content=ft.Column(
                        [
                            ft.Text("账号", color=text, weight=ft.FontWeight.BOLD),
                            account_status,
                            ft.Row(
                                [
                                    ft.Button(
                                        ft.Text("退出登录" if auth.get("qq_number") else "登录", no_wrap=True),
                                        on_click=lambda _event: close_then(start_login_action),
                                        bgcolor="#1e293b",
                                        color=text,
                                    ),
                                    ft.Button(
                                        ft.Text("同步歌单", no_wrap=True),
                                        on_click=lambda _event: close_then(sync_playlists),
                                        bgcolor="#1e293b",
                                        color=text,
                                        disabled=not bool(auth.get("qq_number")),
                                    ),
                                ],
                                spacing=8,
                            ),
                            ft.Divider(color=line),
                            show_singers,
                            show_album,
                            show_duration,
                            show_mid,
                            ft.Row([ft.Text("队列字体大小"), font_size]),
                            auto_sync,
                            download_dir,
                        ],
                        tight=True,
                    ),
                    actions=[
                        ft.TextButton("取消", on_click=lambda _event: (page.pop_dialog(), safe_update())),
                        ft.TextButton("保存", on_click=apply),
                    ],
                )
            )
            safe_update()

        sidebar = card(
            ft.Column(
                [
                    ft.Column([ft.Text("Music", color=text, size=20, weight=ft.FontWeight.BOLD), account_text], spacing=4),
                    ft.Container(height=1, bgcolor=line),
                    ft.Column(
                        [
                            ft.Row([new_playlist_button, rename_playlist_button], spacing=8),
                            ft.Row([delete_playlist_button], spacing=8),
                        ],
                        spacing=8,
                    ),
                    ft.Text("我的歌单", color=text, size=13, weight=ft.FontWeight.BOLD),
                    playlist_list,
                ],
                spacing=12,
                expand=True,
            ),
            expand=False,
            padding=16,
        )
        sidebar.width = 310

        hero = ft.Container(
            content=ft.Row(
                [
                    hero_cover,
                    ft.Column([ft.Text("NOW PLAYING", color=muted, size=12, weight=ft.FontWeight.BOLD), player_title, player_meta], spacing=8, expand=True),
                    queue_text,
                ],
                spacing=18,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=18,
            border_radius=8,
            border=border_all(line),
            bgcolor=panel_soft,
        )

        main = card(
            ft.Column(
                [
                    ft.Row([keyword_field, platform_dropdown, search_button, settings_button, quality_dropdown], spacing=10),
                    hero,
                    ft.Row(
                        [
                            ft.Container(
                                content=ft.Column(
                                    [
                                        ft.Row([ft.Text("歌曲队列", color=text, size=16, weight=ft.FontWeight.BOLD), ft.Row([add_song_button, remove_song_button, download_song_button], spacing=8)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                                        song_list,
                                    ],
                                    expand=True,
                                ),
                                expand=3,
                                padding=14,
                                bgcolor="#020617",
                                border=border_all(line),
                                border_radius=8,
                            ),
                            ft.Container(
                                content=ft.Column(
                                    [
                                        ft.Text("歌曲信息", color=text, size=16, weight=ft.FontWeight.BOLD),
                                        song_info,
                                        ft.Divider(color=line),
                                        ft.Text("歌词", color=text, size=16, weight=ft.FontWeight.BOLD),
                                        lyric_list,
                                    ],
                                    expand=True,
                                ),
                                expand=2,
                                padding=14,
                                bgcolor="#020617",
                                border=border_all(line),
                                border_radius=8,
                            ),
                        ],
                        spacing=14,
                        expand=True,
                    ),
                ],
                spacing=14,
                expand=True,
            ),
            expand=True,
            padding=16,
        )

        player_bar = ft.Container(
            content=ft.Row(
                [
                    ft.Row([prev_button, play_button, next_button, play_mode_dropdown], spacing=8),
                    ft.Container(
                        content=ft.Column(
                            [
                                ft.Row([status_text, lyric_preview_text], spacing=12),
                                ft.Row([elapsed_text, progress_slider, duration_text], spacing=8),
                            ],
                            spacing=4,
                        ),
                        expand=True,
                    ),
                ],
                spacing=18,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(14, 10, 14, 10),
            bgcolor="#101827",
            border=ft.Border(top=ft.BorderSide(1, line)),
        )

        page.add(
            ft.Column(
                [
                    ft.Row([sidebar, main], spacing=16, expand=True),
                    player_bar,
                ],
                expand=True,
                spacing=0,
            )
        )
        refresh_account_label()
        refresh_playlist_list()
        refresh_playback_progress()
        resize_progress()
        page.on_resize = resize_progress
        if auth.get("qq_number") and settings.get("auto_sync_playlists", True):
            sync_playlists()

    ft.run(app, view=ft.AppView.FLET_APP)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Music Linux client")
    parser.add_argument("--api-base", default=os.environ.get("QQMUSIC_API_BASE", DEFAULT_API_BASE))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("QQMUSIC_TIMEOUT", DEFAULT_TIMEOUT)))
    parser.add_argument("--player", default=os.environ.get("QQMUSIC_PLAYER"))
    parser.add_argument(
        "--platform",
        default=os.environ.get("MUSIC_PLATFORM", str(load_settings().get("platform", "qqmusic"))),
        choices=tuple(PLATFORMS.keys()),
        help="音乐平台",
    )

    subparsers = parser.add_subparsers(dest="command")

    login_parser = subparsers.add_parser("login", help="扫码登录 QQ 音乐")
    login_parser.add_argument("qq_number", nargs="?", help="仅 --cookie 调试模式需要；普通登录不用填写")
    login_parser.add_argument("--cookie", help="调试用：直接保存 QQ 音乐网页登录 Cookie")
    login_parser.add_argument("--timeout-seconds", type=int, default=180, help="扫码登录超时时间")
    login_parser.add_argument("--phone", help="网易云手机号登录使用")
    login_parser.add_argument("--captcha", help="网易云短信验证码")
    login_parser.add_argument("--send-captcha", action="store_true", help="网易云发送短信验证码")

    subparsers.add_parser("logout", help="删除本地登录信息")

    sync_parser = subparsers.add_parser("sync-playlists", help="同步并列出我的歌单")
    sync_parser.add_argument("qq_number", nargs="?", help="QQ 号；不传时使用已保存的登录信息")

    search_parser = subparsers.add_parser("search", help="搜索歌曲")
    search_parser.add_argument("keyword")
    search_parser.add_argument("-n", "--count", type=int, default=10)

    play_parser = subparsers.add_parser("play", help="搜索并播放歌曲")
    play_parser.add_argument("keyword")
    play_parser.add_argument("-n", "--count", type=int, default=10)
    play_parser.add_argument("-i", "--index", type=int, default=1, help="播放第几个搜索结果")
    play_parser.add_argument("-q", "--quality", default="320", choices=("128", "320", "flac"))
    play_parser.add_argument("--wait", action="store_true", help="前台等待，Ctrl+C 停止")

    lyric_parser = subparsers.add_parser("lyric", help="搜索并显示歌词")
    lyric_parser.add_argument("keyword")
    lyric_parser.add_argument("-n", "--count", type=int, default=10)
    lyric_parser.add_argument("-i", "--index", type=int, default=1, help="显示第几个搜索结果")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command:
            return run_cli(args)
        return run_flet_gui(args.api_base, args.timeout, args.player)
    except QQMusicError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
