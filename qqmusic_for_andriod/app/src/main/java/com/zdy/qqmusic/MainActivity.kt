package com.zdy.qqmusic

import android.content.Context
import android.content.Intent
import android.media.MediaPlayer
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

data class Playlist(
    val id: String,
    val name: String,
    val songCount: Int,
    val builtin: Boolean,
)

data class Song(
    val title: String,
    val mid: String,
    val songId: String,
    val songType: Int,
    val singers: String,
    val album: String,
    val durationText: String,
    val mediaMid: String,
)

data class LocalSettings(
    val baseUrl: String = "http://10.0.2.2:8765",
    val quality: String = "320",
    val initialPageSize: Int = 50,
    val platform: String = "qqmusic",
)

data class BackendState(
    val account: String = "",
    val platform: String = "qqmusic",
    val platformName: String = "QQ 音乐",
)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MusicTheme {
                MusicApp()
            }
        }
    }
}

@Composable
private fun MusicTheme(content: @Composable () -> Unit) {
    val colors = darkColorScheme(
        primary = Color(0xFF22C878),
        background = Color(0xFF07111F),
        surface = Color(0xFF0D1728),
        surfaceVariant = Color(0xFF132237),
        onPrimary = Color(0xFF03130B),
        onBackground = Color(0xFFEAF2FF),
        onSurface = Color(0xFFEAF2FF),
        onSurfaceVariant = Color(0xFFAAB7C7),
    )
    MaterialTheme(colorScheme = colors, content = content)
}

@Composable
private fun MusicApp() {
    val context = LocalContext.current
    val uriHandler = LocalUriHandler.current
    val scope = rememberCoroutineScope()
    val player = remember { AudioController() }
    val store = remember { LocalJsonStore(context) }
    val savedSettings = remember { store.loadSettings() }

    var baseUrl by remember { mutableStateOf(savedSettings.baseUrl) }
    var account by remember { mutableStateOf("") }
    var status by remember { mutableStateOf("准备就绪") }
    var playlists by remember { mutableStateOf<List<Playlist>>(emptyList()) }
    var selectedPlaylist by remember { mutableStateOf<Playlist?>(null) }
    var songs by remember { mutableStateOf<List<Song>>(emptyList()) }
    var selectedSong by remember { mutableStateOf<Song?>(null) }
    var query by remember { mutableStateOf("") }
    var lyric by remember { mutableStateOf("") }
    var platform by remember { mutableStateOf(savedSettings.platform) }
    var platformName by remember { mutableStateOf(if (savedSettings.platform == "netease") "网易云音乐" else "QQ 音乐") }

    val api = remember(baseUrl) { MusicApi(baseUrl) }

    fun runTask(block: suspend () -> Unit) {
        scope.launch {
            try {
                block()
            } catch (exc: Exception) {
                status = exc.message ?: "请求失败"
            }
        }
    }

    suspend fun refreshAll() {
        val state = api.state()
        account = state.account
        platform = state.platform
        platformName = state.platformName
        store.saveAuth(state.account, if (state.account.isBlank()) "" else "backend")
        if (state.account.isNotBlank()) {
            playlists = api.playlists()
            selectedPlaylist = playlists.firstOrNull()
            selectedPlaylist?.let { playlist ->
                songs = api.playlistSongs(playlist.id)
            }
            status = "已同步"
        } else {
            playlists = emptyList()
            songs = emptyList()
            selectedPlaylist = null
            status = "未登录"
        }
    }

    suspend fun switchPlatform(nextPlatform: String) {
        api.updatePlatform(nextPlatform)
        platform = nextPlatform
        platformName = if (nextPlatform == "netease") "网易云音乐" else "QQ 音乐"
        store.saveSettings(savedSettings.copy(baseUrl = baseUrl, platform = nextPlatform))
        account = ""
        playlists = emptyList()
        songs = emptyList()
        selectedPlaylist = null
        selectedSong = null
        player.release()
        refreshAll()
    }

    suspend fun openPlaylist(playlist: Playlist) {
        selectedPlaylist = playlist
        songs = api.playlistSongs(playlist.id)
        selectedSong = songs.firstOrNull()
        status = "已加载 ${songs.size} 首"
    }

    suspend fun play(song: Song) {
        selectedSong = song
        lyric = api.lyric(song.mid)
        val url = api.songUrl(song)
        player.play(url)
        status = "正在播放：${song.title}"
    }

    LaunchedEffect(Unit) {
        refreshAll()
    }

    LaunchedEffect(baseUrl) {
        store.saveSettings(store.loadSettings().copy(baseUrl = baseUrl))
    }

    DisposableEffect(Unit) {
        onDispose { player.release() }
    }

    Surface(modifier = Modifier.fillMaxSize()) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .background(MaterialTheme.colorScheme.background)
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Header(
                baseUrl = baseUrl,
                onBaseUrlChange = { baseUrl = it },
                account = account,
                platform = platform,
                platformName = platformName,
                status = status,
                onRefresh = { runTask { refreshAll() } },
                onLogout = {
                    runTask {
                        api.logout()
                        store.saveAuth("", "")
                        refreshAll()
                    }
                },
                onQQLogin = {
                    if (!MobileAuthProvider.openQQAuth(context)) {
                        status = "未配置 QQ Open SDK，已打开本地网页登录"
                        uriHandler.openUri(baseUrl)
                    }
                },
                onWeChatLogin = {
                    if (!MobileAuthProvider.openWeChatAuth(context)) {
                        status = "未配置微信 Open SDK，已打开本地网页登录"
                        uriHandler.openUri(baseUrl)
                    }
                },
                onNeteaseLogin = {
                    status = "请在浏览器里使用手机号验证码或 Cookie 登录网易云"
                    uriHandler.openUri(baseUrl)
                },
            )

            SearchBar(
                query = query,
                onQueryChange = { query = it },
                platform = platform,
                onPlatformChange = { next -> runTask { switchPlatform(next) } },
                onSearch = {
                    runTask {
                        songs = api.search(query)
                        selectedPlaylist = null
                        status = "搜索到 ${songs.size} 首"
                    }
                },
            )

            Row(
                modifier = Modifier.weight(1f),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                PlaylistPanel(
                    modifier = Modifier.weight(0.38f),
                    playlists = playlists,
                    selected = selectedPlaylist,
                    onSelect = { playlist -> runTask { openPlaylist(playlist) } },
                    onCreate = {
                        runTask {
                            api.createPlaylist("新建歌单")
                            refreshAll()
                        }
                    },
                    onDelete = {
                        selectedPlaylist?.let { playlist ->
                            runTask {
                                api.deletePlaylist(playlist.id)
                                refreshAll()
                            }
                        }
                    },
                )

                SongPanel(
                    modifier = Modifier.weight(0.62f),
                    title = selectedPlaylist?.name ?: "歌曲",
                    songs = songs,
                    selectedSong = selectedSong,
                    lyric = lyric,
                    onPlay = { song -> runTask { play(song) } },
                    onRemove = {
                        val playlist = selectedPlaylist
                        val song = selectedSong
                        if (playlist != null && song != null) {
                            runTask {
                                api.removeSong(playlist.id, song)
                                openPlaylist(playlist)
                            }
                        }
                    },
                )
            }

            PlayerBar(
                song = selectedSong,
                isPlaying = player.isPlaying,
                onPlayPause = {
                    if (player.isPlaying) player.pause() else player.resume()
                },
                onNext = {
                    selectedSong?.let { current ->
                        val next = songs.getOrNull(songs.indexOf(current) + 1) ?: songs.firstOrNull()
                        next?.let { runTask { play(it) } }
                    }
                },
            )
        }
    }
}

@Composable
private fun Header(
    baseUrl: String,
    onBaseUrlChange: (String) -> Unit,
    account: String,
    platform: String,
    platformName: String,
    status: String,
    onRefresh: () -> Unit,
    onLogout: () -> Unit,
    onQQLogin: () -> Unit,
    onWeChatLogin: () -> Unit,
    onNeteaseLogin: () -> Unit,
) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                LogoBox()
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    Text(platformName, fontSize = 24.sp, fontWeight = FontWeight.Bold)
                    Text(if (account.isBlank()) "未登录" else "已登录：$account", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Text(status, maxLines = 1, overflow = TextOverflow.Ellipsis, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            OutlinedTextField(
                modifier = Modifier.fillMaxWidth(),
                value = baseUrl,
                onValueChange = onBaseUrlChange,
                singleLine = true,
                label = { Text("后端地址") },
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onRefresh) { Text("同步") }
                if (account.isBlank()) {
                    if (platform == "netease") {
                        OutlinedButton(onClick = onNeteaseLogin) { Text("网易云登录") }
                    } else {
                        OutlinedButton(onClick = onQQLogin) { Text("QQ 登录") }
                        OutlinedButton(onClick = onWeChatLogin) { Text("微信登录") }
                    }
                } else {
                    OutlinedButton(onClick = onLogout) { Text("退出登录") }
                }
            }
        }
    }
}

@Composable
private fun SearchBar(
    query: String,
    onQueryChange: (String) -> Unit,
    platform: String,
    onPlatformChange: (String) -> Unit,
    onSearch: () -> Unit,
) {
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
        OutlinedTextField(
            modifier = Modifier.weight(1f),
            value = query,
            onValueChange = onQueryChange,
            singleLine = true,
            label = { Text("搜索歌曲、歌手、专辑") },
        )
        OutlinedButton(onClick = { onPlatformChange(if (platform == "netease") "qqmusic" else "netease") }) {
            Text(if (platform == "netease") "网易云" else "QQ 音乐")
        }
        Button(onClick = onSearch) { Text("搜索") }
    }
}

@Composable
private fun PlaylistPanel(
    modifier: Modifier,
    playlists: List<Playlist>,
    selected: Playlist?,
    onSelect: (Playlist) -> Unit,
    onCreate: () -> Unit,
    onDelete: () -> Unit,
) {
    Card(modifier = modifier.fillMaxSize(), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onCreate, modifier = Modifier.weight(1f)) { Text("新建") }
                OutlinedButton(onClick = onDelete, modifier = Modifier.weight(1f)) { Text("删除") }
            }
            Text("我的歌单", fontWeight = FontWeight.Bold)
            LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(playlists, key = { it.id }) { playlist ->
                    val active = selected?.id == playlist.id
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(
                                if (active) MaterialTheme.colorScheme.surfaceVariant else Color.Transparent,
                                RoundedCornerShape(10.dp),
                            )
                            .clickable { onSelect(playlist) }
                            .padding(10.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Cover()
                        Spacer(Modifier.width(10.dp))
                        Column(Modifier.weight(1f)) {
                            Text(playlist.name, maxLines = 1, overflow = TextOverflow.Ellipsis, fontWeight = FontWeight.Bold)
                            Text("${playlist.songCount} 首", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun SongPanel(
    modifier: Modifier,
    title: String,
    songs: List<Song>,
    selectedSong: Song?,
    lyric: String,
    onPlay: (Song) -> Unit,
    onRemove: () -> Unit,
) {
    Card(modifier = modifier.fillMaxSize(), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(title, fontSize = 20.sp, fontWeight = FontWeight.Bold)
                    Text("${songs.size} 首", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                OutlinedButton(onClick = onRemove) { Text("移除") }
            }
            LazyColumn(modifier = Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(songs, key = { "${it.mid}-${it.songId}" }) { song ->
                    val active = selectedSong?.mid == song.mid
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(
                                if (active) MaterialTheme.colorScheme.surfaceVariant else Color(0xFF0A1325),
                                RoundedCornerShape(10.dp),
                            )
                            .clickable { onPlay(song) }
                            .padding(10.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Cover()
                        Spacer(Modifier.width(10.dp))
                        Column(Modifier.weight(1f)) {
                            Text(song.title, maxLines = 1, overflow = TextOverflow.Ellipsis, fontWeight = FontWeight.Bold)
                            Text("${song.singers} ${song.durationText}", maxLines = 1, overflow = TextOverflow.Ellipsis, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
            }
            selectedSong?.let { song ->
                Text("歌曲信息", fontWeight = FontWeight.Bold)
                Text("${song.title}\n${song.singers}\n${song.album}\nMID: ${song.mid}", color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text("歌词", fontWeight = FontWeight.Bold)
                Text(
                    lyric.ifBlank { "暂无歌词" },
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(120.dp),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}

@Composable
private fun PlayerBar(song: Song?, isPlaying: Boolean, onPlayPause: () -> Unit, onNext: () -> Unit) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            OutlinedButton(onClick = onPlayPause) { Text(if (isPlaying) "暂停" else "播放") }
            OutlinedButton(onClick = onNext) { Text("下一首") }
            Column(Modifier.weight(1f)) {
                Text(song?.title ?: "未选择歌曲", maxLines = 1, overflow = TextOverflow.Ellipsis, fontWeight = FontWeight.Bold)
                Text(song?.singers ?: "", maxLines = 1, overflow = TextOverflow.Ellipsis, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
private fun LogoBox() {
    Box(
        modifier = Modifier
            .size(58.dp)
            .background(Brush.linearGradient(listOf(Color(0xFF26D17C), Color(0xFF35B9E9))), RoundedCornerShape(12.dp)),
        contentAlignment = Alignment.Center,
    ) {
        Text("Q", color = Color(0xFF07111F), fontSize = 32.sp, fontWeight = FontWeight.Black)
    }
}

@Composable
private fun Cover() {
    Box(
        modifier = Modifier
            .size(52.dp)
            .background(Brush.linearGradient(listOf(Color(0xFF27CF7A), Color(0xFF35BCE8))), RoundedCornerShape(8.dp)),
        contentAlignment = Alignment.Center,
    ) {
        Text("Q", color = Color(0xFF07111F), fontWeight = FontWeight.Black)
    }
}

class MusicApi(private val baseUrl: String) {
    suspend fun state(): BackendState {
        val json = request("GET", "/api/state")
        return BackendState(
            account = json.optString("account"),
            platform = json.optString("platform", "qqmusic"),
            platformName = json.optString("platform_name", "QQ 音乐"),
        )
    }

    suspend fun updatePlatform(platform: String) {
        request("PUT", "/api/settings", JSONObject().put("platform", platform))
    }

    suspend fun logout() {
        request("POST", "/api/logout")
    }

    suspend fun playlists(): List<Playlist> {
        val json = request("GET", "/api/playlists")
        val rows = json.getJSONArray("playlists")
        return List(rows.length()) { index ->
            val item = rows.getJSONObject(index)
            Playlist(
                id = item.optString("id"),
                name = item.optString("name"),
                songCount = item.optInt("song_count"),
                builtin = item.optBoolean("builtin"),
            )
        }
    }

    suspend fun playlistSongs(id: String): List<Song> {
        val json = request("GET", "/api/playlists/${encode(id)}/songs?begin=0&count=50")
        return parseSongs(json)
    }

    suspend fun search(keyword: String): List<Song> {
        val json = request("GET", "/api/search?q=${encode(keyword)}&count=40")
        return parseSongs(json)
    }

    suspend fun songUrl(song: Song): String {
        val path = "/api/song-url?mid=${encode(song.mid)}&media_mid=${encode(song.mediaMid)}&quality=320"
        return request("GET", path).getString("url")
    }

    suspend fun lyric(mid: String): String {
        return request("GET", "/api/lyric?mid=${encode(mid)}").optString("lyric")
    }

    suspend fun createPlaylist(name: String) {
        request("POST", "/api/playlists", JSONObject().put("name", name))
    }

    suspend fun deletePlaylist(id: String) {
        request("DELETE", "/api/playlists/${encode(id)}")
    }

    suspend fun removeSong(playlistId: String, song: Song) {
        request(
            "DELETE",
            "/api/playlists/${encode(playlistId)}/songs",
            JSONObject()
                .put("song_id", song.songId)
                .put("song_type", song.songType),
        )
    }

    private fun parseSongs(json: JSONObject): List<Song> {
        val rows = json.getJSONArray("songs")
        return List(rows.length()) { index ->
            val item = rows.getJSONObject(index)
            Song(
                title = item.optString("title"),
                mid = item.optString("mid"),
                songId = item.optString("song_id"),
                songType = item.optInt("song_type"),
                singers = item.optString("singers"),
                album = item.optString("album"),
                durationText = item.optString("duration_text"),
                mediaMid = item.optString("media_mid"),
            )
        }
    }

    private suspend fun request(method: String, path: String, body: JSONObject? = null): JSONObject = withContext(Dispatchers.IO) {
        val root = baseUrl.trimEnd('/')
        val connection = (URL("$root$path").openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 15_000
            readTimeout = 30_000
            setRequestProperty("Accept", "application/json")
            if (body != null) {
                doOutput = true
                setRequestProperty("Content-Type", "application/json; charset=utf-8")
                outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
        }
        val stream = if (connection.responseCode in 200..299) connection.inputStream else connection.errorStream
        val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
        val json = JSONObject(text.ifBlank { "{}" })
        if (!json.optBoolean("ok", true)) {
            throw IOException(json.optString("error", "请求失败"))
        }
        json
    }

    private fun encode(value: String): String = URLEncoder.encode(value, "UTF-8")
}

class LocalJsonStore(private val context: Context) {
    private val settingsFile = File(context.filesDir, "qqmusic_settings.json")
    private val authFile = File(context.filesDir, "qqmusic_auth.json")

    fun loadSettings(): LocalSettings {
        ensureFile(settingsFile, "qqmusic_settings.json")
        return try {
            val json = JSONObject(settingsFile.readText(Charsets.UTF_8))
            LocalSettings(
                baseUrl = json.optString("base_url", "http://10.0.2.2:8765"),
                quality = json.optString("quality", "320"),
                initialPageSize = json.optInt("initial_page_size", 50),
                platform = json.optString("platform", "qqmusic"),
            )
        } catch (_: Exception) {
            LocalSettings()
        }
    }

    fun saveSettings(settings: LocalSettings) {
        val json = JSONObject()
            .put("base_url", settings.baseUrl)
            .put("quality", settings.quality)
            .put("initial_page_size", settings.initialPageSize)
            .put("platform", settings.platform)
        settingsFile.writeText(json.toString(2), Charsets.UTF_8)
    }

    fun saveAuth(account: String, provider: String) {
        val json = JSONObject()
            .put("account", account)
            .put("provider", provider)
        authFile.writeText(json.toString(2), Charsets.UTF_8)
    }

    private fun ensureFile(file: File, assetName: String) {
        if (file.exists()) return
        val text = runCatching {
            context.assets.open(assetName).bufferedReader(Charsets.UTF_8).use { it.readText() }
        }.getOrDefault("{}")
        file.writeText(text, Charsets.UTF_8)
    }
}

class AudioController {
    private var mediaPlayer: MediaPlayer? = null
    var isPlaying by mutableStateOf(false)
        private set

    fun play(url: String) {
        release()
        mediaPlayer = MediaPlayer().apply {
            setDataSource(url)
            setOnPreparedListener {
                it.start()
                isPlaying = true
            }
            setOnCompletionListener { isPlaying = false }
            prepareAsync()
        }
    }

    fun pause() {
        mediaPlayer?.pause()
        isPlaying = false
    }

    fun resume() {
        mediaPlayer?.start()
        isPlaying = true
    }

    fun release() {
        mediaPlayer?.release()
        mediaPlayer = null
        isPlaying = false
    }
}

object MobileAuthProvider {
    private const val QQ_OPEN_SDK_CONFIGURED = false
    private const val WECHAT_OPEN_SDK_CONFIGURED = false

    fun openQQAuth(context: Context): Boolean {
        if (!QQ_OPEN_SDK_CONFIGURED) return false
        return openExternal(context, "mqqapi://") || openExternal(context, "mqq://")
    }

    fun openWeChatAuth(context: Context): Boolean {
        if (!WECHAT_OPEN_SDK_CONFIGURED) return false
        return openExternal(context, "weixin://")
    }

    private fun openExternal(context: Context, url: String): Boolean {
        return try {
            val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            if (intent.resolveActivity(context.packageManager) == null) return false
            context.startActivity(intent)
            true
        } catch (_: Exception) {
            false
        }
    }
}
