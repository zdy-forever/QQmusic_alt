import Foundation

struct ApiState: Decodable {
    let ok: Bool
    let loggedIn: Bool
    let account: String
    let displayName: String
    let platform: String
    let platformName: String

    enum CodingKeys: String, CodingKey {
        case ok
        case loggedIn = "logged_in"
        case account
        case displayName = "display_name"
        case platform
        case platformName = "platform_name"
    }
}

struct Playlist: Identifiable, Decodable, Hashable {
    let id: String
    let name: String
    let songCount: Int
    let builtin: Bool

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case songCount = "song_count"
        case builtin
    }
}

struct Song: Identifiable, Decodable, Hashable {
    let title: String
    let mid: String
    let songId: String
    let songType: Int
    let singers: String
    let album: String
    let durationText: String
    let mediaMid: String

    var id: String { "\(mid)-\(songId)" }

    enum CodingKeys: String, CodingKey {
        case title
        case mid
        case songId = "song_id"
        case songType = "song_type"
        case singers
        case album
        case durationText = "duration_text"
        case mediaMid = "media_mid"
    }
}

struct PlaylistsResponse: Decodable {
    let ok: Bool
    let playlists: [Playlist]
}

struct SongsResponse: Decodable {
    let ok: Bool
    let name: String?
    let songs: [Song]
    let total: Int?
}

struct SongUrlResponse: Decodable {
    let ok: Bool
    let url: String
}

struct LyricResponse: Decodable {
    let ok: Bool
    let lyric: String
}

struct EmptyResponse: Decodable {
    let ok: Bool
}

struct ApiErrorResponse: Decodable {
    let ok: Bool?
    let error: String?
}
