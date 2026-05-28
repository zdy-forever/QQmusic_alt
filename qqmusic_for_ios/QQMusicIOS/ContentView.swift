import SwiftUI

@MainActor
final class MusicViewModel: ObservableObject {
    @Published var baseURLText = "http://127.0.0.1:8765" {
        didSet {
            var settings = store.loadSettings()
            settings.baseURL = baseURLText
            store.saveSettings(settings)
        }
    }
    @Published var account = ""
    @Published var displayName = ""
    @Published var status = "准备就绪"
    @Published var playlists: [Playlist] = []
    @Published var songs: [Song] = []
    @Published var selectedPlaylist: Playlist?
    @Published var selectedSong: Song?
    @Published var query = ""
    @Published var lyric = ""
    @Published var platform = "qqmusic"
    @Published var platformName = "QQ 音乐"

    let player = AudioPlayer()
    let store = LocalJsonStore()
    private var api: MusicAPI {
        MusicAPI(baseURL: URL(string: baseURLText.trimmingCharacters(in: .whitespacesAndNewlines)) ?? URL(string: "http://127.0.0.1:8765")!)
    }

    init() {
        let settings = store.loadSettings()
        baseURLText = settings.baseURL
        platform = settings.platform
        platformName = settings.platform == "netease" ? "网易云音乐" : "QQ 音乐"
    }

    func refreshAll() async {
        do {
            let state = try await api.state()
            account = state.account
            displayName = state.displayName.isEmpty ? state.account : state.displayName
            platform = state.platform
            platformName = state.platformName
            store.saveAuth(account: state.account, provider: state.loggedIn ? "backend" : "")
            guard state.loggedIn else {
                playlists = []
                songs = []
                selectedPlaylist = nil
                selectedSong = nil
                displayName = ""
                status = "未登录"
                return
            }
            playlists = try await api.playlists()
            selectedPlaylist = playlists.first
            if let selectedPlaylist {
                songs = try await api.playlistSongs(selectedPlaylist.id)
            }
            status = "已同步"
        } catch {
            status = error.localizedDescription
        }
    }

    func switchPlatform(_ nextPlatform: String) async {
        do {
            try await api.updatePlatform(nextPlatform)
            var settings = store.loadSettings()
            settings.platform = nextPlatform
            store.saveSettings(settings)
            platform = nextPlatform
            platformName = nextPlatform == "netease" ? "网易云音乐" : "QQ 音乐"
            account = ""
            displayName = ""
            playlists = []
            songs = []
            selectedPlaylist = nil
            selectedSong = nil
            player.stop()
            await refreshAll()
        } catch {
            status = error.localizedDescription
        }
    }

    func open(_ playlist: Playlist) async {
        do {
            selectedPlaylist = playlist
            songs = try await api.playlistSongs(playlist.id)
            selectedSong = songs.first
            status = "已加载 \(songs.count) 首"
        } catch {
            status = error.localizedDescription
        }
    }

    func search() async {
        do {
            songs = try await api.search(query)
            selectedPlaylist = nil
            selectedSong = songs.first
            status = "搜索到 \(songs.count) 首"
        } catch {
            status = error.localizedDescription
        }
    }

    func play(_ song: Song) async {
        do {
            selectedSong = song
            async let url = api.songURL(for: song)
            async let lyricText = api.lyric(mid: song.mid)
            player.play(url: try await url)
            lyric = try await lyricText
            status = "正在播放：\(song.title)"
        } catch {
            status = error.localizedDescription
        }
    }

    func next() async {
        guard let selectedSong else { return }
        let nextIndex = (songs.firstIndex(of: selectedSong) ?? -1) + 1
        guard let nextSong = songs.indices.contains(nextIndex) ? songs[nextIndex] : songs.first else { return }
        await play(nextSong)
    }

    func logout() async {
        do {
            try await api.logout()
            store.saveAuth(account: "", provider: "")
            await refreshAll()
        } catch {
            status = error.localizedDescription
        }
    }

    func createPlaylist() async {
        do {
            try await api.createPlaylist(name: "新建歌单")
            await refreshAll()
        } catch {
            status = error.localizedDescription
        }
    }

    func deleteSelectedPlaylist() async {
        guard let selectedPlaylist else { return }
        do {
            try await api.deletePlaylist(selectedPlaylist.id)
            await refreshAll()
        } catch {
            status = error.localizedDescription
        }
    }

    func removeSelectedSong() async {
        guard let selectedPlaylist, let selectedSong else { return }
        do {
            try await api.removeSong(selectedSong, from: selectedPlaylist.id)
            await open(selectedPlaylist)
        } catch {
            status = error.localizedDescription
        }
    }
}

struct ContentView: View {
    @StateObject private var model = MusicViewModel()

    var body: some View {
        NavigationSplitView {
            Sidebar(model: model)
        } detail: {
            MainPanel(model: model)
        }
        .task {
            await model.refreshAll()
        }
    }
}

private struct Sidebar: View {
    @ObservedObject var model: MusicViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                Logo()
                VStack(alignment: .leading) {
                    Text(model.platformName).font(.title2.bold())
                    Text(model.account.isEmpty ? "未登录" : "已登录：\(model.displayName.isEmpty ? model.account : model.displayName)")
                        .foregroundStyle(.secondary)
                }
            }

            TextField("后端地址", text: $model.baseURLText)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .textFieldStyle(.roundedBorder)

            HStack {
                Button("同步") {
                    Task { await model.refreshAll() }
                }
                if model.account.isEmpty {
                    if model.platform == "netease" {
                        Button("网易云登录") {
                            if let url = URL(string: model.baseURLText) {
                                MobileAuthProvider.openBackendLogin(baseURL: url)
                            }
                        }
                    } else {
                        Button("QQ 登录") {
                            guard !MobileAuthProvider.openQQAuth() else { return }
                            if let url = URL(string: model.baseURLText) {
                                MobileAuthProvider.openBackendLogin(baseURL: url)
                            }
                        }
                        Button("微信登录") {
                            guard !MobileAuthProvider.openWeChatAuth() else { return }
                            if let url = URL(string: model.baseURLText) {
                                MobileAuthProvider.openBackendLogin(baseURL: url)
                            }
                        }
                    }
                } else {
                    Button("退出登录", role: .destructive) {
                        Task { await model.logout() }
                    }
                }
            }

            HStack {
                Text("我的歌单").font(.headline)
                Spacer()
                Text("\(model.playlists.count)")
                    .foregroundStyle(.secondary)
            }

            HStack {
                Button("新建") { Task { await model.createPlaylist() } }
                Button("删除", role: .destructive) { Task { await model.deleteSelectedPlaylist() } }
            }

            List(model.playlists, selection: Binding(
                get: { model.selectedPlaylist },
                set: { playlist in
                    guard let playlist else { return }
                    Task { await model.open(playlist) }
                }
            )) { playlist in
                HStack(spacing: 10) {
                    Cover()
                    VStack(alignment: .leading) {
                        Text(playlist.name).font(.headline)
                        Text("\(playlist.songCount) 首").foregroundStyle(.secondary)
                    }
                }
                .tag(playlist)
            }
        }
        .padding()
        .background(Color(uiColor: .systemGroupedBackground))
    }
}

private struct MainPanel: View {
    @ObservedObject var model: MusicViewModel

    var body: some View {
        VStack(spacing: 12) {
            HStack {
                TextField("搜索歌曲、歌手、专辑", text: $model.query)
                    .textFieldStyle(.roundedBorder)
                Button(model.platform == "netease" ? "网易云" : "QQ 音乐") {
                    Task { await model.switchPlatform(model.platform == "netease" ? "qqmusic" : "netease") }
                }
                Button("搜索") { Task { await model.search() } }
            }

            NowPlaying(song: model.selectedSong)

            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading) {
                    HStack {
                        VStack(alignment: .leading) {
                            Text(model.selectedPlaylist?.name ?? "歌曲队列").font(.title3.bold())
                            Text("\(model.songs.count) 首").foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("移除", role: .destructive) { Task { await model.removeSelectedSong() } }
                    }
                    List(model.songs, selection: Binding(
                        get: { model.selectedSong },
                        set: { song in
                            guard let song else { return }
                            Task { await model.play(song) }
                        }
                    )) { song in
                        HStack(spacing: 10) {
                            Cover()
                            VStack(alignment: .leading) {
                                Text(song.title).font(.headline)
                                Text("\(song.singers) \(song.durationText)")
                                    .lineLimit(1)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .tag(song)
                    }
                }

                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("歌曲信息").font(.title3.bold())
                        if let song = model.selectedSong {
                            Text(song.mid).foregroundStyle(.secondary)
                            Divider()
                            InfoRow("歌曲", song.title)
                            InfoRow("歌手", song.singers)
                            InfoRow("专辑", song.album)
                            InfoRow("时长", song.durationText)
                            Text("歌词").font(.headline)
                            Text(model.lyric.isEmpty ? "暂无歌词" : model.lyric)
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        } else {
                            Text("请选择歌曲").foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding()
                }
                .frame(minWidth: 280)
                .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
            }

            PlayerBar(model: model)
        }
        .padding()
        .background(Color(uiColor: .systemBackground))
    }
}

private struct NowPlaying: View {
    let song: Song?

    var body: some View {
        HStack(spacing: 16) {
            Logo().frame(width: 86, height: 86)
            VStack(alignment: .leading, spacing: 6) {
                Text(song?.title ?? "请选择歌曲")
                    .font(.largeTitle.bold())
                    .lineLimit(1)
                Text(song.map { "\($0.singers) \($0.durationText)" } ?? "")
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding()
        .background(.linearGradient(colors: [.green.opacity(0.22), .blue.opacity(0.16)], startPoint: .leading, endPoint: .trailing), in: RoundedRectangle(cornerRadius: 14))
    }
}

private struct PlayerBar: View {
    @ObservedObject var model: MusicViewModel

    var body: some View {
        HStack(spacing: 12) {
            Button(model.player.isPlaying ? "暂停" : "播放") {
                if model.player.isPlaying {
                    model.player.pause()
                } else {
                    model.player.resume()
                }
            }
            Button("下一首") {
                Task { await model.next() }
            }
            Text(model.status)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer()
        }
        .padding()
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 14))
    }
}

private struct Logo: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 14)
                .fill(.linearGradient(colors: [.green, .cyan], startPoint: .topLeading, endPoint: .bottomTrailing))
            Text("Q")
                .font(.system(size: 34, weight: .black))
                .foregroundStyle(.black.opacity(0.78))
        }
        .frame(width: 58, height: 58)
    }
}

private struct Cover: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 9)
                .fill(.linearGradient(colors: [.green, .cyan], startPoint: .topLeading, endPoint: .bottomTrailing))
            Text("Q").font(.headline.bold()).foregroundStyle(.black.opacity(0.78))
        }
        .frame(width: 48, height: 48)
    }
}

private func InfoRow(_ title: String, _ value: String) -> some View {
    VStack(alignment: .leading, spacing: 4) {
        Text(title).font(.caption.bold()).foregroundStyle(.secondary)
        Text(value)
    }
}
