import Foundation

struct LocalSettings: Codable {
    var baseURL: String
    var quality: String
    var initialPageSize: Int

    enum CodingKeys: String, CodingKey {
        case baseURL = "base_url"
        case quality
        case initialPageSize = "initial_page_size"
    }

    static let fallback = LocalSettings(baseURL: "http://127.0.0.1:8765", quality: "320", initialPageSize: 50)
}

struct LocalAuth: Codable {
    var account: String
    var provider: String
}

final class LocalJsonStore {
    private let directory: URL

    init() {
        directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
    }

    var settingsURL: URL {
        directory.appendingPathComponent("qqmusic_settings.json")
    }

    var authURL: URL {
        directory.appendingPathComponent("qqmusic_auth.json")
    }

    func loadSettings() -> LocalSettings {
        ensureDefaults()
        guard let data = try? Data(contentsOf: settingsURL),
              let settings = try? JSONDecoder().decode(LocalSettings.self, from: data) else {
            return .fallback
        }
        return settings
    }

    func saveSettings(_ settings: LocalSettings) {
        if let data = try? JSONEncoder.pretty.encode(settings) {
            try? data.write(to: settingsURL, options: .atomic)
        }
    }

    func saveAuth(account: String, provider: String) {
        let auth = LocalAuth(account: account, provider: provider)
        if let data = try? JSONEncoder.pretty.encode(auth) {
            try? data.write(to: authURL, options: .atomic)
        }
    }

    private func ensureDefaults() {
        if !FileManager.default.fileExists(atPath: settingsURL.path) {
            saveSettings(.fallback)
        }
        if !FileManager.default.fileExists(atPath: authURL.path) {
            saveAuth(account: "", provider: "")
        }
    }
}

private extension JSONEncoder {
    static var pretty: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }
}
