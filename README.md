# 为什么要开发这个应用程序呢

那当然是从腾讯的QQ音乐官网下载的 QQ音乐 for Linux在 linux Mint上启动会直接闪退 用不了一点

这里我要批评腾讯程序员了

你们都不用linux吗

这系统小众到你们都不维护了我真的很想哭

于是我想能不能开发一个全平台通用的听歌软件呢

我真的很喜欢听歌

然后就有了基于腾讯的API开发的简单化QQ MUSIC

# QQ Music Linux Client

一个给 Linux Mint 用的简化 QQ 音乐客户端。它使用网络上的 QQ Music API 做搜索、歌词和播放链接获取，然后把音频 URL 交给本机播放器播放。

## 说明

- 不绕过会员、DRM、地区限制、登录限制或付费限制。
- 默认 API: `https://api.ygking.top`
- 可通过环境变量 `QQMUSIC_API_BASE` 换成你自己部署或信任的兼容 API。
- 如果安装了 `python-vlc`，会优先用内嵌 VLC 播放，避免每首歌弹出一个 VLC 窗口；否则回退到本机播放器。
- 登录信息保存在本机 `.qqmusic_auth.json`，不要把这个文件分享给别人。

## 图形界面

```bash
python3 qqmusic_client.py
```

打开后输入歌曲名或歌手，搜索，双击结果或点“播放”。底部播放器栏会显示当前歌曲、歌手和队列位置，并提供上一首、播放、停止、下一首控制。播放模式可切换为顺序播放、随机播放或单曲循环，并会保存到设置文件。

点击“设置”可以调整歌曲队列显示内容，例如是否显示歌手、专辑名、歌曲时长、MID，以及队列字体大小。设置会保存到 `.qqmusic_settings.json`。设置里也可以切换默认音质、下载目录和启动时是否自动同步歌单。

### 登录并同步歌单

在图形界面里点击“登录”，用手机 QQ 扫描弹出的二维码并确认。登录成功后，程序会把登录信息保存到当前目录的 `.qqmusic_auth.json`，下次启动会自动恢复登录并同步歌单。

之后点击“同步歌单”可以重新加载歌单。双击左侧歌单可以把歌单歌曲载入中间列表。需要切换账号时点击“退出登录”。

歌单区域支持新建、重命名和删除自建歌单。“我喜欢”和“已下载的歌曲”这类内置列表不能重命名或删除。重命名后程序会重新拉取歌单做校验，如果 QQ 音乐没有真正改名，会提示失败。

歌曲队列里选择一首歌后，可以加入左侧选中的自建歌单；如果左侧没有选中可加入的歌单，会弹出歌单选择窗口。搜索结果里的歌曲、已经打开的歌单里的歌曲，都可以加入另一个自建歌单。加入成功后会刷新歌单；如果正在查看目标歌单，会直接重新加载当前列表。

打开某个歌单后，可以把选中的歌曲从当前歌单移除。打开“已下载的歌曲”时，移除操作会删除本地下载文件。

播放时会根据歌词时间戳高亮当前歌词。`python-vlc` 后端能读取真实播放进度；外部播放器回退模式会用本地计时估算进度。

## 命令行

扫码登录：

```bash
python3 qqmusic_client.py login
```

同步并列出我的歌单：

```bash
python3 qqmusic_client.py sync-playlists
```

退出登录：

```bash
python3 qqmusic_client.py logout
```

搜索：

```bash
python3 qqmusic_client.py search 周杰伦
```

播放第一条搜索结果：

```bash
python3 qqmusic_client.py play 稻香 --wait
```

播放第二条搜索结果，音质用 128：

```bash
python3 qqmusic_client.py play 稻香 --index 2 --quality 128 --wait
```

显示歌词：

```bash
python3 qqmusic_client.py lyric 稻香
```

## 可选配置

安装内嵌 VLC 播放支持：

```bash
sudo apt install python3-vlc
```

指定播放器：

```bash
QQMUSIC_PLAYER=/usr/bin/vlc python3 qqmusic_client.py
```

指定 API：

```bash
QQMUSIC_API_BASE=https://api.ygking.top python3 qqmusic_client.py
```

如果播放失败但搜索正常，通常是歌曲本身有会员、版权、地区或登录限制。可以尝试把音质切到 `128`。
