import SwiftUI
import UIKit

enum MobileAuthProvider {
    private static let qqOpenSDKConfigured = false
    private static let weChatOpenSDKConfigured = false

    static func openQQAuth() -> Bool {
        guard qqOpenSDKConfigured else { return false }
        open("mqqapi://") || open("mqq://")
    }

    static func openWeChatAuth() -> Bool {
        guard weChatOpenSDKConfigured else { return false }
        open("weixin://")
    }

    static func openBackendLogin(baseURL: URL) {
        UIApplication.shared.open(baseURL)
    }

    private static func open(_ value: String) -> Bool {
        guard let url = URL(string: value), UIApplication.shared.canOpenURL(url) else {
            return false
        }
        UIApplication.shared.open(url)
        return true
    }
}
