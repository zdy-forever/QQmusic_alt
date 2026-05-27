# Music iOS

SwiftUI + AVPlayer 版本，复用 `qqmusic_for_web_browser/server.py` 提供的本地音乐 API。

## 运行

1. 先启动后端：

   ```bash
   cd ../qqmusic_for_web_browser
   python3 server.py
   ```

2. 在 Xcode 新建 iOS App 项目，语言选择 Swift，界面选择 SwiftUI。
3. 把 `QQMusicIOS/` 里的 Swift 文件加入项目。
4. iOS 模拟器默认可用 `http://127.0.0.1:8765`；真机请在界面里改为电脑局域网 IP，例如 `http://192.168.1.10:8765`。

## 登录

当前代码提供了 `MobileAuthProvider`：

- 搜索栏旁边可以切换 QQ 音乐 / 网易云音乐，切换会同步到本地后端。
- QQ 音乐已预留 QQ/微信手机 SDK 登录入口。
- 网易云音乐不提供扫码登录；点击“网易云登录”会打开本地 Web 页，用手机号验证码或 Cookie 登录。
- 没有开放平台 AppID、Bundle ID、Universal Links、URL Scheme 和回调配置时，会退回到本地网页登录/二维码登录。
- 真正上线时需要接入 Tencent Open SDK 和 WeChat Open SDK，并在授权完成后把凭据交给后端换取可保存的音乐平台 Cookie。

## 本地 JSON

iOS 运行时会在 App 自己的 Documents 目录下维护：

- `qqmusic_settings.json`
- `qqmusic_auth.json`

项目里的默认模板放在 `QQMusicIOS/`，代码不会读取 `qqmusic_for_pc/` 的登录或设置文件。

## 注意

如果使用 HTTP 局域网地址，Xcode 项目需要在 `Info.plist` 配置 App Transport Security 允许本地明文请求。
