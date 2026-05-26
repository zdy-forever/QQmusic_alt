let INITIAL_PAGE_SIZE = 50;
let BACKGROUND_PAGE_SIZE = 500;

const $ = (id) => document.getElementById(id);
const audio = $("audio");

const state = {
  loggedIn: false,
  account: "",
  playlists: [],
  currentPlaylist: null,
  songs: [],
  selectedIndex: -1,
  playIndex: -1,
  playMode: "顺序播放",
  settings: {},
  settingsFiles: {},
  loadToken: 0,
  lyricLines: [],
  activeLyric: -1,
  pendingAddSong: null,
  loginPoll: null,
  loginToken: 0,
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function qs(params) {
  return new URLSearchParams(params).toString();
}

async function api(path, options = {}) {
  const init = { ...options };
  if (init.body && typeof init.body !== "string") {
    init.body = JSON.stringify(init.body);
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `请求失败: ${response.status}`);
  }
  return data;
}

function toast(message, timeout = 2400) {
  const node = $("toast");
  node.textContent = message;
  node.classList.remove("hidden");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.add("hidden"), timeout);
}

function setBusy(message) {
  if (message) toast(message, 1200);
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0:00";
  const total = Math.floor(seconds);
  const min = Math.floor(total / 60);
  const sec = String(total % 60).padStart(2, "0");
  return `${min}:${sec}`;
}

function songCover(song, size = 150) {
  if (song?.cover) return song.cover;
  const mid = song?.media_mid || song?.mid || "";
  return mid ? `https://y.qq.com/music/photo_new/T002R${size}x${size}M000${mid}.jpg?max_age=2592000` : "";
}

function updateAccount() {
  $("accountText").textContent = state.loggedIn ? `已登录: ${state.account}` : "未登录";
  $("accountButton").textContent = state.loggedIn ? "退出登录" : "登录";
  $("syncButton").disabled = !state.loggedIn;
  $("newPlaylistButton").disabled = !state.loggedIn;
  $("renamePlaylistButton").disabled = !state.loggedIn || !state.currentPlaylist || state.currentPlaylist.builtin;
  $("deletePlaylistButton").disabled = !state.loggedIn || !state.currentPlaylist || state.currentPlaylist.builtin;
  $("addButton").disabled = !state.loggedIn || state.selectedIndex < 0;
  $("removeButton").disabled = !state.loggedIn || !state.currentPlaylist || state.selectedIndex < 0;
  $("downloadButton").disabled = state.selectedIndex < 0;
}

function renderPlaylists() {
  $("playlistCount").textContent = state.playlists.length;
  $("playlistList").innerHTML = state.playlists.map((playlist) => {
    const active = state.currentPlaylist?.id === playlist.id ? " active" : "";
    const cover = playlist.cover
      ? `<span class="playlist-cover"><img src="${escapeHtml(playlist.cover)}" alt="" loading="lazy" onerror="this.remove()"></span>`
      : `<span class="playlist-cover"></span>`;
    const count = playlist.song_count == null ? "" : `${playlist.song_count} 首`;
    return `
      <div class="playlist-item${active}" role="button" tabindex="0" data-playlist-id="${escapeHtml(playlist.id)}">
        ${cover}
        <span class="playlist-copy">
          <span class="playlist-name">${escapeHtml(playlist.name)}</span>
          <span class="playlist-meta">${escapeHtml(count || playlist.dirid || "")}</span>
        </span>
      </div>`;
  }).join("");
  updateAccount();
}

function renderSongs(append = false, newSongs = []) {
  const list = $("songList");
  const rows = (append ? newSongs : state.songs).map((song, offset) => {
    const index = append ? state.songs.length - newSongs.length + offset : offset;
    const active = index === state.selectedIndex ? " active" : "";
    const cover = songCover(song, 68);
    return `
      <div class="song-item${active}" role="button" tabindex="0" data-song-index="${index}">
        ${cover ? `<span class="mini-cover"><img src="${escapeHtml(cover)}" alt="" loading="lazy" onerror="this.remove()"></span>` : `<span class="mini-cover"></span>`}
        <span class="song-copy">
          <span class="song-title">${escapeHtml(song.title)}</span>
          <span class="song-meta">${escapeHtml([song.singers, song.album, song.duration_text].filter(Boolean).join("  "))}</span>
        </span>
        <span class="song-index">${index + 1}</span>
      </div>`;
  }).join("");
  if (append) {
    list.insertAdjacentHTML("beforeend", rows);
  } else {
    list.innerHTML = rows;
  }
  $("queueSummary").textContent = `${state.songs.length} 首`;
  updateAccount();
}

function selectSong(index) {
  if (index < 0 || index >= state.songs.length) return;
  state.selectedIndex = index;
  const song = state.songs[index];
  document.querySelectorAll(".song-item.active").forEach((node) => node.classList.remove("active"));
  const row = document.querySelector(`[data-song-index="${index}"]`);
  if (row) row.classList.add("active");
  $("trackTitle").textContent = song.title;
  $("trackMeta").textContent = [song.singers, song.album, song.duration_text].filter(Boolean).join("  ") || "QQ 音乐";
  $("detailMeta").textContent = song.mid || "未选择";
  $("songInfo").innerHTML = `
    <div><span>歌曲</span><strong>${escapeHtml(song.title)}</strong></div>
    <div><span>歌手</span><strong>${escapeHtml(song.singers || "未知")}</strong></div>
    <div><span>专辑</span><strong>${escapeHtml(song.album || "未知")}</strong></div>
    <div><span>时长</span><strong>${escapeHtml(song.duration_text || "0:00")}</strong></div>
    <div><span>MID</span><strong>${escapeHtml(song.mid || "")}</strong></div>`;
  const cover = songCover(song, 300);
  $("coverImage").style.display = cover ? "block" : "none";
  $("coverImage").src = cover || "";
  $("addButton").disabled = !state.loggedIn;
  $("removeButton").disabled = !state.loggedIn || !state.currentPlaylist;
  $("downloadButton").disabled = false;
  loadLyrics(song);
}

function clearQueue(label = "队列") {
  state.songs = [];
  state.selectedIndex = -1;
  state.lyricLines = [];
  $("songList").innerHTML = "";
  $("lyrics").innerHTML = "";
  $("queueSummary").textContent = "0 首";
  $("sourceLabel").textContent = label;
  updateAccount();
}

async function init() {
  bindEvents();
  try {
    await loadSettings();
    const data = await api("/api/state");
    state.loggedIn = data.logged_in;
    state.account = data.account || "";
    updateAccount();
    if (state.loggedIn && state.settings.auto_sync_playlists !== false) await syncPlaylists(false);
  } catch (error) {
    toast(error.message);
  }
}

async function loadSettings() {
  const data = await api("/api/settings");
  state.settings = data.settings || {};
  state.settingsFiles = data.files || {};
  applySettings();
}

function applySettings() {
  INITIAL_PAGE_SIZE = Number(state.settings.initial_page_size || 50);
  BACKGROUND_PAGE_SIZE = Number(state.settings.background_page_size || 500);
  state.playMode = state.settings.play_mode || "顺序播放";
  $("qualitySelect").value = state.settings.quality || "320";
  $("playModeSelect").value = state.playMode;
  $("settingQualitySelect").value = state.settings.quality || "320";
  $("settingPlayModeSelect").value = state.playMode;
  $("settingInitialPageSize").value = INITIAL_PAGE_SIZE;
  $("settingBackgroundPageSize").value = BACKGROUND_PAGE_SIZE;
  $("settingAutoSync").checked = state.settings.auto_sync_playlists !== false;
  $("authFilePath").textContent = state.settingsFiles.auth || "";
  $("settingsFilePath").textContent = state.settingsFiles.settings || "";
}

async function saveSettings(partial, showToast = false) {
  const data = await api("/api/settings", { method: "PUT", body: { ...state.settings, ...partial } });
  state.settings = data.settings || state.settings;
  applySettings();
  if (showToast) toast("设置已保存");
}

async function syncPlaylists(showToast = true) {
  if (!state.loggedIn) return;
  if (showToast) setBusy("正在同步歌单...");
  const data = await api("/api/playlists");
  state.playlists = data.playlists || [];
  if (state.currentPlaylist) {
    state.currentPlaylist = state.playlists.find((item) => item.id === state.currentPlaylist.id) || state.currentPlaylist;
  }
  renderPlaylists();
  if (showToast) toast(`已同步 ${state.playlists.length} 个歌单`);
}

async function loadPlaylist(playlist) {
  state.currentPlaylist = playlist;
  state.loadToken += 1;
  const token = state.loadToken;
  renderPlaylists();
  clearQueue(playlist.name);
  setBusy(`正在加载 ${playlist.name}...`);
  let begin = 0;
  let total = 0;
  try {
    const first = await api(`/api/playlists/${encodeURIComponent(playlist.id)}/songs?${qs({ begin, count: INITIAL_PAGE_SIZE })}`);
    if (token !== state.loadToken) return;
    total = first.total || first.songs.length;
    state.songs = first.songs || [];
    $("sourceLabel").textContent = first.name || playlist.name;
    renderSongs(false);
    if (state.songs.length) selectSong(0);
    toast(`${first.name || playlist.name}: 已加载 ${state.songs.length} / ${total} 首`);
    begin = state.songs.length;
    while (begin < total) {
      const page = await api(`/api/playlists/${encodeURIComponent(playlist.id)}/songs?${qs({ begin, count: BACKGROUND_PAGE_SIZE })}`);
      if (token !== state.loadToken) return;
      const pageSongs = page.songs || [];
      if (!pageSongs.length) break;
      total = Math.max(total, page.total || begin + pageSongs.length);
      state.songs.push(...pageSongs);
      renderSongs(true, pageSongs);
      begin = state.songs.length;
      $("queueSummary").textContent = `${state.songs.length} / ${total} 首`;
    }
    toast(`${playlist.name}: ${state.songs.length} 首`);
  } catch (error) {
    toast(error.message, 4200);
  }
}

async function searchSongs() {
  const keyword = $("searchInput").value.trim();
  if (!keyword) return toast("请输入搜索关键词");
  clearQueue("搜索结果");
  setBusy("正在搜索...");
  try {
    const data = await api(`/api/search?${qs({ q: keyword, count: 50 })}`);
    state.currentPlaylist = null;
    state.songs = data.songs || [];
    renderPlaylists();
    renderSongs(false);
    if (state.songs.length) selectSong(0);
    toast(`找到 ${state.songs.length} 首`);
  } catch (error) {
    toast(error.message, 4200);
  }
}

async function playSelected() {
  const index = state.selectedIndex >= 0 ? state.selectedIndex : 0;
  if (!state.songs[index]) return toast("先选择一首歌");
  await playAt(index);
}

async function playAt(index) {
  const song = state.songs[index];
  if (!song) return;
  state.playIndex = index;
  selectSong(index);
  setBusy("正在获取播放链接...");
  try {
    const data = await api(`/api/song-url?${qs({
      mid: song.mid,
      media_mid: song.media_mid || "",
      quality: $("qualitySelect").value,
    })}`);
    audio.src = data.url;
    await audio.play();
    document.body.classList.add("playing");
    $("playButton").textContent = "暂停";
    toast(`正在播放: ${song.title}`);
  } catch (error) {
    document.body.classList.remove("playing");
    toast(error.message, 5000);
  }
}

function pauseOrResume() {
  if (!audio.src) {
    playSelected();
    return;
  }
  if (audio.paused) {
    audio.play();
    document.body.classList.add("playing");
    $("playButton").textContent = "暂停";
  } else {
    audio.pause();
    document.body.classList.remove("playing");
    $("playButton").textContent = "播放";
  }
}

function stopPlayback() {
  audio.pause();
  audio.currentTime = 0;
  document.body.classList.remove("playing");
  $("playButton").textContent = "播放";
}

function nextIndex(reverse = false, auto = false) {
  if (!state.songs.length) return -1;
  const index = state.playIndex >= 0 ? state.playIndex : state.selectedIndex;
  if (state.playMode === "单曲循环") return Math.max(index, 0);
  if (state.playMode === "随机播放") {
    if (state.songs.length === 1) return 0;
    let next = index;
    while (next === index) next = Math.floor(Math.random() * state.songs.length);
    return next;
  }
  if (reverse) return index > 0 ? index - 1 : state.songs.length - 1;
  const next = index + 1;
  if (next < state.songs.length) return next;
  return auto ? -1 : 0;
}

async function loadLyrics(song) {
  state.lyricLines = [];
  state.activeLyric = -1;
  $("lyrics").innerHTML = "<div class=\"lyric-line\">正在加载歌词...</div>";
  if (!song?.mid) return;
  try {
    const data = await api(`/api/lyric?${qs({ mid: song.mid })}`);
    state.lyricLines = data.lines || [];
    if (state.lyricLines.length) {
      $("lyrics").innerHTML = state.lyricLines.map((line, index) =>
        `<div class="lyric-line" data-lyric-index="${index}">${escapeHtml(line.text)}</div>`
      ).join("");
    } else {
      $("lyrics").innerHTML = escapeHtml(data.lyric || "没有歌词").split("\n").map((line) =>
        `<div class="lyric-line">${line}</div>`
      ).join("");
    }
  } catch (error) {
    $("lyrics").innerHTML = `<div class="lyric-line">${escapeHtml(error.message)}</div>`;
  }
}

function updateLyricHighlight() {
  if (!state.lyricLines.length) return;
  const position = audio.currentTime * 1000 + 350;
  let active = -1;
  for (let index = 0; index < state.lyricLines.length; index += 1) {
    if (state.lyricLines[index].time_ms <= position) active = index;
    else break;
  }
  if (active === state.activeLyric || active < 0) return;
  document.querySelectorAll(".lyric-line.active").forEach((node) => node.classList.remove("active"));
  const node = document.querySelector(`[data-lyric-index="${active}"]`);
  if (node) {
    node.classList.add("active");
    node.scrollIntoView({ block: "center", behavior: "smooth" });
    $("trackMeta").textContent = node.textContent;
  }
  state.activeLyric = active;
}

function updateProgress() {
  $("currentTime").textContent = formatTime(audio.currentTime);
  $("durationTime").textContent = formatTime(audio.duration);
  if (Number.isFinite(audio.duration) && audio.duration > 0) {
    $("progress").value = Math.floor((audio.currentTime / audio.duration) * 1000);
  } else {
    $("progress").value = 0;
  }
  updateLyricHighlight();
}

function openModal(id) {
  $(id).classList.remove("hidden");
}

function closeModal(id) {
  $(id).classList.add("hidden");
}

function setLoginProvider(provider) {
  document.querySelectorAll(".login-tabs button").forEach((node) => {
    node.classList.toggle("active", node.dataset.provider === provider);
  });
}

async function startLogin(provider) {
  setLoginProvider(provider);
  clearInterval(state.loginPoll);
  state.loginToken += 1;
  const token = state.loginToken;
  const label = provider === "wechat" ? "微信" : "QQ";
  $("loginStatus").textContent = `正在生成${label}登录二维码...`;
  $("qrImage").removeAttribute("src");
  try {
    const data = await api("/api/login/start", { method: "POST", body: { provider } });
    if (token !== state.loginToken) return;
    if (data.provider !== provider) throw new Error("登录方式返回不一致，请重新打开登录窗口");
    $("qrImage").src = data.image;
    $("loginStatus").textContent = provider === "wechat" ? "请使用微信扫码并确认" : "请使用手机 QQ 扫码并确认";
    state.loginPoll = setInterval(async () => {
      try {
        if (token !== state.loginToken) return;
        const poll = await api(`/api/login/poll?${qs({ id: data.id })}`);
        if (token !== state.loginToken) return;
        if (poll.state === "done") {
          clearInterval(state.loginPoll);
          state.loggedIn = true;
          state.account = poll.account;
          closeModal("loginModal");
          updateAccount();
          await syncPlaylists();
          toast(`已登录: ${poll.nickname || poll.account}`);
        } else if (poll.state === "expired" || poll.state === "cancelled") {
          clearInterval(state.loginPoll);
          $("loginStatus").textContent = poll.state === "expired" ? "二维码已过期" : "已取消登录";
        } else {
          $("loginStatus").textContent = poll.state === "confirming" ? "请在手机上确认" : "等待扫码...";
        }
      } catch (error) {
        clearInterval(state.loginPoll);
        $("loginStatus").textContent = error.message;
      }
    }, 1600);
  } catch (error) {
    $("loginStatus").textContent = error.message;
  }
}

async function logout() {
  await api("/api/logout", { method: "POST" });
  state.loggedIn = false;
  state.account = "";
  state.playlists = [];
  state.currentPlaylist = null;
  renderPlaylists();
  clearQueue("队列");
  toast("已退出登录");
}

async function createPlaylist() {
  const name = prompt("歌单名称");
  if (!name?.trim()) return;
  await api("/api/playlists", { method: "POST", body: { name: name.trim() } });
  await syncPlaylists();
  toast("歌单已创建");
}

async function renamePlaylist() {
  const playlist = state.currentPlaylist;
  if (!playlist || playlist.builtin) return toast("这个歌单不能重命名");
  const name = prompt("新名称", playlist.name);
  if (!name?.trim() || name.trim() === playlist.name) return;
  await api(`/api/playlists/${encodeURIComponent(playlist.dirid || playlist.id)}`, { method: "PUT", body: { name: name.trim() } });
  await syncPlaylists();
  toast("歌单已重命名");
}

async function deletePlaylist() {
  const playlist = state.currentPlaylist;
  if (!playlist || playlist.builtin) return toast("这个歌单不能删除");
  if (!confirm(`确定删除“${playlist.name}”？`)) return;
  await api(`/api/playlists/${encodeURIComponent(playlist.dirid || playlist.id)}`, { method: "DELETE" });
  state.currentPlaylist = null;
  await syncPlaylists();
  clearQueue("队列");
  toast("歌单已删除");
}

function openAddDialog() {
  const song = state.songs[state.selectedIndex];
  if (!song) return toast("先选择一首歌");
  state.pendingAddSong = song;
  const candidates = state.playlists.filter((playlist) => playlist.id);
  $("playlistPickerList").innerHTML = candidates.map((playlist) => `
    <button class="picker-item" data-target-playlist="${escapeHtml(playlist.dirid || playlist.id)}" data-target-id="${escapeHtml(playlist.id)}">
      <strong>${escapeHtml(playlist.name)}</strong>
      <div class="playlist-meta">${playlist.song_count == null ? "" : `${playlist.song_count} 首`}</div>
    </button>
  `).join("");
  openModal("playlistModal");
}

async function addToPlaylist(dirid, playlistId) {
  if (!state.pendingAddSong) return;
  await api(`/api/playlists/${encodeURIComponent(dirid)}/songs`, {
    method: "POST",
    body: { song: state.pendingAddSong, playlist_id: playlistId },
  });
  closeModal("playlistModal");
  await syncPlaylists(false);
  if (state.currentPlaylist?.id === playlistId) await loadPlaylist(state.currentPlaylist);
  toast("已加入歌单");
}

async function removeFromPlaylist() {
  const playlist = state.currentPlaylist;
  const song = state.songs[state.selectedIndex];
  if (!playlist || !song) return toast("先打开歌单并选择歌曲");
  if (!confirm(`从“${playlist.name}”移除“${song.title}”？`)) return;
  await api(`/api/playlists/${encodeURIComponent(playlist.dirid || playlist.id)}/songs`, {
    method: "DELETE",
    body: { song_id: song.song_id, song_type: song.song_type },
  });
  await syncPlaylists(false);
  await loadPlaylist(playlist);
  toast("歌曲已移除");
}

function downloadSelected() {
  const song = state.songs[state.selectedIndex];
  if (!song) return toast("先选择一首歌");
  const url = `/api/download?${qs({
    mid: song.mid,
    media_mid: song.media_mid || "",
    quality: $("qualitySelect").value,
    title: song.title,
  })}`;
  window.location.href = url;
}

function bindEvents() {
  $("accountButton").addEventListener("click", () => {
    if (state.loggedIn) logout().catch((error) => toast(error.message));
    else {
      openModal("loginModal");
      startLogin("qq");
    }
  });
  $("syncButton").addEventListener("click", () => syncPlaylists().catch((error) => toast(error.message)));
  $("searchButton").addEventListener("click", () => searchSongs());
  $("searchInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") searchSongs();
  });
  $("playlistList").addEventListener("click", (event) => {
    const item = event.target.closest("[data-playlist-id]");
    if (!item) return;
    const playlist = state.playlists.find((entry) => entry.id === item.dataset.playlistId);
    if (playlist) loadPlaylist(playlist);
  });
  $("playlistList").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const item = event.target.closest("[data-playlist-id]");
    if (!item) return;
    event.preventDefault();
    const playlist = state.playlists.find((entry) => entry.id === item.dataset.playlistId);
    if (playlist) loadPlaylist(playlist);
  });
  $("songList").addEventListener("click", (event) => {
    const item = event.target.closest("[data-song-index]");
    if (item) selectSong(Number(item.dataset.songIndex));
  });
  $("songList").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const item = event.target.closest("[data-song-index]");
    if (!item) return;
    event.preventDefault();
    if (event.key === "Enter") playAt(Number(item.dataset.songIndex));
    else selectSong(Number(item.dataset.songIndex));
  });
  $("songList").addEventListener("dblclick", (event) => {
    const item = event.target.closest("[data-song-index]");
    if (item) playAt(Number(item.dataset.songIndex));
  });
  $("playButton").addEventListener("click", pauseOrResume);
  $("stopButton").addEventListener("click", stopPlayback);
  $("prevButton").addEventListener("click", () => {
    const index = nextIndex(true);
    if (index >= 0) playAt(index);
  });
  $("nextButton").addEventListener("click", () => {
    const index = nextIndex(false);
    if (index >= 0) playAt(index);
  });
  $("playModeSelect").addEventListener("change", () => {
    state.playMode = $("playModeSelect").value;
    saveSettings({ play_mode: state.playMode }).catch((error) => toast(error.message, 4200));
  });
  $("qualitySelect").addEventListener("change", () => {
    saveSettings({ quality: $("qualitySelect").value }).catch((error) => toast(error.message, 4200));
  });
  $("progress").addEventListener("input", () => {
    if (Number.isFinite(audio.duration) && audio.duration > 0) {
      audio.currentTime = Number($("progress").value) * audio.duration / 1000;
    }
  });
  audio.addEventListener("timeupdate", updateProgress);
  audio.addEventListener("pause", () => {
    if (!audio.ended) {
      document.body.classList.remove("playing");
      $("playButton").textContent = "播放";
    }
  });
  audio.addEventListener("play", () => {
    document.body.classList.add("playing");
    $("playButton").textContent = "暂停";
  });
  audio.addEventListener("ended", () => {
    document.body.classList.remove("playing");
    $("playButton").textContent = "播放";
    const index = nextIndex(false, true);
    if (index >= 0) playAt(index);
  });
  document.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.close === "loginModal") {
        clearInterval(state.loginPoll);
        state.loginToken += 1;
      }
      closeModal(button.dataset.close);
    });
  });
  document.querySelectorAll(".login-tabs button").forEach((button) => {
    button.addEventListener("click", () => {
      startLogin(button.dataset.provider);
    });
  });
  $("playlistPickerList").addEventListener("click", (event) => {
    const item = event.target.closest("[data-target-playlist]");
    if (item) addToPlaylist(item.dataset.targetPlaylist, item.dataset.targetId).catch((error) => toast(error.message, 4200));
  });
  $("newPlaylistButton").addEventListener("click", () => createPlaylist().catch((error) => toast(error.message, 4200)));
  $("renamePlaylistButton").addEventListener("click", () => renamePlaylist().catch((error) => toast(error.message, 4200)));
  $("deletePlaylistButton").addEventListener("click", () => deletePlaylist().catch((error) => toast(error.message, 4200)));
  $("addButton").addEventListener("click", openAddDialog);
  $("removeButton").addEventListener("click", () => removeFromPlaylist().catch((error) => toast(error.message, 4200)));
  $("downloadButton").addEventListener("click", downloadSelected);
  const settingsButton = $("settingsButton");
  if (settingsButton) settingsButton.addEventListener("click", () => openModal("settingsModal"));
  $("saveSettingsButton").addEventListener("click", () => {
    saveSettings({
      quality: $("settingQualitySelect").value,
      play_mode: $("settingPlayModeSelect").value,
      initial_page_size: Number($("settingInitialPageSize").value || 50),
      background_page_size: Number($("settingBackgroundPageSize").value || 500),
      auto_sync_playlists: $("settingAutoSync").checked,
    }, true).then(() => closeModal("settingsModal")).catch((error) => toast(error.message, 4200));
  });
}

init();
