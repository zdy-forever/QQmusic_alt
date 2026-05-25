#!/usr/bin/env python3
"""Small QQ Music client for Linux.

This client uses metadata and playback-link APIs. It does not try to bypass
paid, region-locked, DRM-protected, or account-only content.
"""

from __future__ import annotations

import argparse
import base64
import binascii
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
DEFAULT_SETTINGS_FILE = Path(__file__).with_name(".qqmusic_settings.json")
PLAYLIST_INITIAL_PAGE_SIZE = 50
PLAYLIST_BACKGROUND_PAGE_SIZE = 500
QQ_QR_APPID = "716027609"
QQ_QR_THIRD_APPID = "100497308"
WX_QR_APPID = "wx48db31d50e334801"
WX_QR_REDIRECT_URI = "https://y.qq.com/portal/wx_redirect.html?login_type=2&surl=https%3A%2F%2Fy.qq.com%2F"

DEFAULT_SETTINGS: dict[str, Any] = {
    "queue_show_singers": True,
    "queue_show_album": True,
    "queue_show_duration": True,
    "queue_show_mid": False,
    "queue_font_size": 11,
    "quality": "320",
    "play_mode": "顺序播放",
    "auto_sync_playlists": True,
    "download_dir": str(Path.home() / "音乐" / "QQMusic"),
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
                "User-Agent": "Mozilla/5.0 qqmusic-linux-client/1.0",
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
                "User-Agent": "Mozilla/5.0 qqmusic-linux-client/1.0",
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
                "User-Agent": "Mozilla/5.0 qqmusic-linux-client/1.0",
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
                "redirect_uri": "https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/",
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
        while loaded < total:
            page_name, page_results, page_total = self.playlist_songs_page(playlist_id, cookie, loaded, PLAYLIST_BACKGROUND_PAGE_SIZE)
            if page_name:
                name = page_name
            if page_total > total:
                total = page_total
            if not page_results:
                break
            results.extend(page_results)
            loaded += len(page_results)
            if len(page_results) < PLAYLIST_BACKGROUND_PAGE_SIZE and loaded >= page_total:
                break
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


def auth_file_path() -> Path:
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


def load_auth() -> dict[str, str]:
    path = auth_file_path()
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
    cookie_account = qq_number_from_cookie(auth.get("cookie", ""))
    if cookie_account:
        auth["qq_number"] = cookie_account
    return auth


def save_auth(qq_number: str, cookie: str) -> None:
    path = auth_file_path()
    data = {
        "qq_number": qq_number.strip(),
        "cookie": cookie.strip(),
        "updated_at": str(int(time.time())),
    }
    try:
        with path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise QQMusicError(f"保存登录信息失败: {exc}") from exc


def clear_auth() -> None:
    path = auth_file_path()
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

    def play(self, url: str) -> None:
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
        args = build_player_args(self.command, url)
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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

    def seek_ms(self, position_ms: int) -> bool:
        if self.backend == "python-vlc" and self._vlc_player is not None:
            self._vlc_player.set_time(max(0, int(position_ms)))
            return True
        return False


def find_player() -> str | None:
    for name in ("mpv", "vlc", "ffplay", "xdg-open"):
        path = shutil.which(name)
        if path:
            return path
    return None


def build_player_args(command: str, url: str) -> list[str]:
    name = os.path.basename(command)
    if name == "mpv":
        return [command, "--force-window=yes", url]
    if name == "vlc":
        return [command, "--started-from-file", url]
    if name == "ffplay":
        return [command, "-autoexit", "-nodisp", url]
    return [command, url]


def run_cli(args: argparse.Namespace) -> int:
    api = QQMusicAPI(base_url=args.api_base, timeout=args.timeout)
    player = Player(args.player)

    if args.command == "login":
        if args.cookie:
            qq_number = args.qq_number or qq_number_from_cookie(args.cookie)
            if not qq_number:
                raise QQMusicError("没有 QQ 号。请传入 QQ 号，或提供包含 uin/wxuin 的 Cookie")
            save_auth(qq_number, args.cookie)
            print(f"已保存登录信息: {qq_number}")
            return 0

        session = api.start_qr_login()
        qr_path = Path.cwd() / "qqmusic_login_qr.png"
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
                save_auth(result.qq_number, result.cookie)
                print(f"登录成功: {result.nickname or result.qq_number}")
                return 0
            time.sleep(2)
        raise QQMusicError("登录超时，请重新运行 login")
        return 0

    if args.command == "logout":
        clear_auth()
        print("已退出登录")
        return 0

    if args.command == "sync-playlists":
        auth = load_auth()
        qq_number = args.qq_number or auth.get("qq_number", "")
        if not qq_number:
            raise QQMusicError("还没有登录。先运行 login，或给 sync-playlists 传入 QQ 号")
        playlists = api.user_playlists(qq_number, auth.get("cookie", ""))
        for index, playlist in enumerate(playlists, 1):
            print(f"{index:02d}. {playlist.display}")
            print(f"    id: {playlist.id}")
        return 0

    if args.command == "search":
        auth = load_auth()
        songs = api.search(args.keyword, count=args.count, cookie=auth.get("cookie", ""))
        for index, song in enumerate(songs, 1):
            print(f"{index:02d}. {song.display}")
            print(f"    mid: {song.mid}")
        return 0

    if args.command == "play":
        auth = load_auth()
        songs = api.search(args.keyword, count=args.count, cookie=auth.get("cookie", ""))
        if not songs:
            raise QQMusicError("没有搜索到歌曲")
        song = songs[min(args.index - 1, len(songs) - 1)]
        url = api.song_url(song.mid, args.quality, auth.get("cookie", ""), song.media_mid)
        print(f"正在播放: {song.display}")
        player.play(url)
        if args.wait:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                player.stop()
        return 0

    if args.command == "lyric":
        auth = load_auth()
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

    api = QQMusicAPI(base_url=api_base, timeout=timeout)
    player = Player(player_command)
    auth = load_auth()
    settings = load_settings()

    root = tk.Tk()
    root.title("QQ music_for_all_system(developed_by_Linux_Mint)")
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
    quality_var = tk.StringVar(value=str(settings.get("quality", "320")))
    status_var = tk.StringVar(value=f"播放器: {player.display_name}")
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
    progress_dragging = False

    root.columnconfigure(0, weight=1)
    root.rowconfigure(1, weight=1)

    top = ttk.Frame(root, padding=(12, 10), style="Toolbar.TFrame")
    top.grid(row=0, column=0, sticky="ew")
    top.columnconfigure(0, weight=1)

    entry = ttk.Entry(top, textvariable=keyword_var)
    entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
    entry.focus_set()

    search_button = ttk.Button(top, text="搜索", style="Accent.TButton")
    search_button.grid(row=0, column=1, padx=(0, 8))

    settings_button = ttk.Button(top, text="设置", style="Accent.TButton")
    settings_button.grid(row=0, column=2)

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
    player_stop_button = ttk.Button(control_group, text="停止", style="Player.TButton")
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
        new_playlist_button.configure(state=state if auth.get("qq_number") else tk.DISABLED)
        rename_playlist_button.configure(state=state if auth.get("qq_number") else tk.DISABLED)
        delete_playlist_button.configure(state=state if auth.get("qq_number") else tk.DISABLED)
        add_song_button.configure(state=state if auth.get("qq_number") else tk.DISABLED)
        remove_song_button.configure(state=state if auth.get("qq_number") else tk.DISABLED)
        download_song_button.configure(state=state)
        if message:
            status_var.set(message)

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
            return 0
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
        nonlocal playback_started_at, progress_dragging
        position = progress_value_to_ms()
        progress_dragging = False
        if position is None:
            return
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

    def refresh_account_label() -> None:
        qq_number = auth.get("qq_number")
        if qq_number:
            account_var.set(f"已登录: {qq_number}")
            new_playlist_button.configure(state=tk.NORMAL)
            rename_playlist_button.configure(state=tk.NORMAL)
            delete_playlist_button.configure(state=tk.NORMAL)
            add_song_button.configure(state=tk.NORMAL)
            remove_song_button.configure(state=tk.NORMAL)
            download_song_button.configure(state=tk.NORMAL)
        else:
            account_var.set("未登录")
            new_playlist_button.configure(state=tk.DISABLED)
            rename_playlist_button.configure(state=tk.DISABLED)
            delete_playlist_button.configure(state=tk.DISABLED)
            add_song_button.configure(state=tk.DISABLED)
            remove_song_button.configure(state=tk.DISABLED)
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
        set_busy(True, "正在搜索...")
        current_playlist = None
        listbox.delete(0, tk.END)
        songs.clear()

        def worker() -> None:
            try:
                results = api.search(keyword, cookie=auth.get("cookie", ""))
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "搜索失败"), messagebox.showerror("搜索失败", message)))
                return
            root.after(0, lambda: replace_songs(results, f"找到 {len(results)} 首"))

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
        account_button_command = logout if auth.get("qq_number") else choose_login_method
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
            save_auth(result.qq_number, result.cookie)
            auth.clear()
            auth.update(load_auth())
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
        clear_auth()
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
            messagebox.showinfo("提示", "先点击登录，用手机 QQ 扫码确认")
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
        if is_builtin_playlist(playlist):
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
                        messagebox.showerror("重命名失败", "QQ 音乐已接受请求，但刷新后歌单名称仍未变化，请稍后重试或重新登录。"),
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
        if is_builtin_playlist(playlist):
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
            messagebox.showerror("添加失败", "当前歌曲缺少 QQ 音乐 song id")
            return
        candidates = [playlist for playlist in remote_playlists if is_addable_playlist(playlist)]
        if not candidates:
            messagebox.showinfo("提示", "还没有可加入的 QQ 音乐歌单，请先同步或新建歌单")
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
            messagebox.showerror("移除失败", "当前歌曲缺少 QQ 音乐 song id")
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

                request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 qqmusic-linux-client/1.0"})
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

    def play_song_at(index: int) -> None:
        nonlocal current_song_index, playback_token, user_stopped, playback_started_at
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
        current_song_index = index
        listbox.selection_clear(0, tk.END)
        listbox.selection_set(index)
        listbox.see(index)
        show_song(song)
        player_title_var.set(song.title)
        player_meta_var.set(song.singers or song.album or "正在获取播放链接")
        queue_var.set(f"{index + 1} / {len(songs)}")
        progress_var.set(0.0)
        progress_text_var.set("0:00")
        duration_text_var.set(format_milliseconds(song.duration * 1000 if song.duration else None))
        set_busy(True, "正在获取播放链接...")

        def worker() -> None:
            try:
                url = song.local_path or api.song_url(song.mid, quality_var.get(), auth.get("cookie", ""), song.media_mid)
                player.play(url)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                root.after(0, lambda: (set_busy(False, "播放失败"), messagebox.showerror("播放失败", message)))
                return
            def update_started() -> None:
                nonlocal playback_started_at
                playback_started_at = time.monotonic()
                set_busy(False)
                player_meta_var.set(song.singers or song.album or "正在播放")
                threading.Thread(target=monitor_playback, args=(play_token,), daemon=True).start()

            root.after(0, update_started)

        threading.Thread(target=worker, daemon=True).start()

    def play_selected() -> None:
        index = selected_song_index()
        if index < 0 and songs:
            index = current_song_index if current_song_index >= 0 else 0
        play_song_at(index)

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
        nonlocal playback_token, user_stopped, playback_started_at
        playback_token += 1
        user_stopped = True
        playback_started_at = 0.0
        player.stop()
        player_meta_var.set("已停止")
        progress_var.set(0.0)
        progress_text_var.set("0:00")
        status_var.set("已停止")

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
    player_stop_button.configure(command=stop)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QQ Music Linux client")
    parser.add_argument("--api-base", default=os.environ.get("QQMUSIC_API_BASE", DEFAULT_API_BASE))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("QQMUSIC_TIMEOUT", DEFAULT_TIMEOUT)))
    parser.add_argument("--player", default=os.environ.get("QQMUSIC_PLAYER"))

    subparsers = parser.add_subparsers(dest="command")

    login_parser = subparsers.add_parser("login", help="扫码登录 QQ 音乐")
    login_parser.add_argument("qq_number", nargs="?", help="仅 --cookie 调试模式需要；普通登录不用填写")
    login_parser.add_argument("--cookie", help="调试用：直接保存 QQ 音乐网页登录 Cookie")
    login_parser.add_argument("--timeout-seconds", type=int, default=180, help="扫码登录超时时间")

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
        return run_gui(args.api_base, args.timeout, args.player)
    except QQMusicError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
