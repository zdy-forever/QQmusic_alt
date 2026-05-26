import Foundation

final class QQMusicAPI {
    var baseURL: URL

    init(baseURL: URL) {
        self.baseURL = baseURL
    }

    func state() async throws -> ApiState {
        try await request("/api/state")
    }

    func logout() async throws {
        let _: EmptyResponse = try await request("/api/logout", method: "POST")
    }

    func playlists() async throws -> [Playlist] {
        let response: PlaylistsResponse = try await request("/api/playlists")
        return response.playlists
    }

    func playlistSongs(_ playlistID: String, begin: Int = 0, count: Int = 50) async throws -> [Song] {
        let path = "/api/playlists/\(encode(playlistID))/songs?begin=\(begin)&count=\(count)"
        let response: SongsResponse = try await request(path)
        return response.songs
    }

    func search(_ keyword: String) async throws -> [Song] {
        let response: SongsResponse = try await request("/api/search?q=\(encode(keyword))&count=40")
        return response.songs
    }

    func songURL(for song: Song) async throws -> URL {
        let path = "/api/song-url?mid=\(encode(song.mid))&media_mid=\(encode(song.mediaMid))&quality=320"
        let response: SongUrlResponse = try await request(path)
        guard let url = URL(string: response.url) else {
            throw QQMusicAPIError.message("播放地址无效")
        }
        return url
    }

    func lyric(mid: String) async throws -> String {
        let response: LyricResponse = try await request("/api/lyric?mid=\(encode(mid))")
        return response.lyric
    }

    func createPlaylist(name: String) async throws {
        let body = ["name": name]
        let _: EmptyResponse = try await request("/api/playlists", method: "POST", body: body)
    }

    func deletePlaylist(_ playlistID: String) async throws {
        let _: EmptyResponse = try await request("/api/playlists/\(encode(playlistID))", method: "DELETE")
    }

    func removeSong(_ song: Song, from playlistID: String) async throws {
        let body: [String: Any] = [
            "song_id": song.songId,
            "song_type": song.songType,
        ]
        let _: EmptyResponse = try await request("/api/playlists/\(encode(playlistID))/songs", method: "DELETE", body: body)
    }

    private func request<T: Decodable>(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> T {
        guard let url = URL(string: path, relativeTo: baseURL) else {
            throw QQMusicAPIError.message("接口地址无效")
        }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.setValue("application/json; charset=utf-8", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw QQMusicAPIError.message("后端无响应")
        }
        guard (200...299).contains(http.statusCode) else {
            let error = try? JSONDecoder().decode(ApiErrorResponse.self, from: data)
            throw QQMusicAPIError.message(error?.error ?? "请求失败：\(http.statusCode)")
        }
        let decoded = try JSONDecoder().decode(T.self, from: data)
        if let error = decoded as? ApiErrorResponse, error.ok == false {
            throw QQMusicAPIError.message(error.error ?? "请求失败")
        }
        return decoded
    }

    private func encode(_ value: String) -> String {
        var allowed = CharacterSet.urlQueryAllowed
        allowed.remove(charactersIn: "&+=?")
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }
}

enum QQMusicAPIError: LocalizedError {
    case message(String)

    var errorDescription: String? {
        switch self {
        case let .message(value):
            return value
        }
    }
}
