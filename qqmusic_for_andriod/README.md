# Music Android

Kotlin + Jetpack Compose 版本，复用 `qqmusic_for_web_browser/server.py` 提供的本地音乐 API。

## 运行

1. 先启动后端：

   ```bash
   cd ../qqmusic_for_web_browser
   python3 server.py
   ```

2. 用 Android Studio 打开 `qqmusic_for_andriod`。
3. 模拟器默认后端地址是 `http://10.0.2.2:8765`；真机请改成电脑局域网 IP，例如 `http://192.168.1.10:8765`。

## 本地 JSON

Android 运行时会在 App 自己的 `filesDir` 下维护：

- `qqmusic_settings.json`
- `qqmusic_auth.json`

项目里的默认模板放在 `app/src/main/assets/`，第一次启动时会复制到 App 沙盒目录，不会读取 `qqmusic_for_pc/` 的文件。

## 登录

当前代码提供了 `MobileAuthProvider`：

- 搜索栏旁边可以切换 QQ 音乐 / 网易云音乐，切换会同步到本地后端。
- QQ 音乐已预留 QQ/微信手机 SDK 登录入口。
- 网易云音乐不提供扫码登录；点击“网易云登录”会打开本地 Web 页，用手机号验证码或 Cookie 登录。
- 没有开放平台 AppID、包名、签名和回调配置时，会退回到本地网页登录/二维码登录。
- 真正上线时需要接入 Tencent Open SDK 和 WeChat Open SDK，并在 `MobileAuthProvider` 里把授权结果换成后端可保存的音乐平台 Cookie。
