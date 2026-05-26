# QQmusic

这个仓库现在包含四个客户端方向：

- `qqmusic_for_pc/`：原 Tkinter PC 客户端。
- `qqmusic_for_web_browser/`：本地 Web 后端 + 浏览器前端。
- `qqmusic_for_andriod/`：Kotlin + Jetpack Compose Android 客户端。
- `qqmusic_for_ios/`：SwiftUI + AVPlayer iOS 客户端源码。

## 本地后端

移动端和浏览器端都复用同一个本地后端：

```bash
cd qqmusic_for_web_browser
python3 server.py
```

默认地址是 `http://127.0.0.1:8765`。如果手机真机访问，需要把后端监听地址改成局域网可访问地址，例如：

```bash
QQMUSIC_WEB_HOST=0.0.0.0 python3 server.py
```

然后在 App 内把后端地址改为电脑的局域网 IP，例如 `http://192.168.1.10:8765`。

## Android

目录：`qqmusic_for_andriod/`

- 用 Android Studio 打开该目录。
- 模拟器默认后端地址：`http://10.0.2.2:8765`。
- 真机使用电脑局域网 IP。
- 当前支持同步登录状态、歌单、分页加载前 50 首、搜索、播放、歌词、创建/删除歌单、从歌单移除歌曲。

## iOS

目录：`qqmusic_for_ios/`

- 在 Xcode 新建 iOS SwiftUI App。
- 把 `QQMusicIOS/` 里的 Swift 文件加入项目。
- 模拟器默认后端地址：`http://127.0.0.1:8765`。
- 真机使用电脑局域网 IP。
- `QQMusicIOS/Info.plist` 提供了本地 HTTP 和 QQ/微信 Scheme 查询配置参考。

## QQ/微信手机登录

移动端代码已经把 QQ/微信登录入口放在 `MobileAuthProvider`：

- Android：`qqmusic_for_andriod/app/src/main/java/com/zdy/qqmusic/MainActivity.kt`
- iOS：`qqmusic_for_ios/QQMusicIOS/MobileAuthProvider.swift`

真正跳转到手机 QQ/微信完成认证，需要在 QQ/微信开放平台创建移动应用，并配置 AppID、包名/Bundle ID、签名、URL Scheme、Universal Links 和 SDK 回调。没有这些官方配置时，当前实现会回退到本地网页登录。

## 登录和设置文件

各端使用自己的 JSON 文件，不再混用 `qqmusic_for_pc/` 的登录态：

- PC：`qqmusic_for_pc/.qqmusic_auth.json`、`qqmusic_for_pc/.qqmusic_settings.json`
- Web：`qqmusic_for_web_browser/.qqmusic_auth.json`、`qqmusic_for_web_browser/.qqmusic_settings.json`
- Android：App 沙盒 `filesDir/qqmusic_auth.json`、`filesDir/qqmusic_settings.json`
- iOS：App Documents `qqmusic_auth.json`、`qqmusic_settings.json`
