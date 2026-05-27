# 为什么要开发这个应用程序呢

那当然是从腾讯的QQ音乐官网下载的 QQ音乐 for Linux在 linux Mint上启动会直接闪退 用不了一点

这里我要批评腾讯程序员了

你们都不用linux吗

这系统小众到你们都不维护了我真的很想哭

于是我想能不能开发一个全电脑平台通用的听歌软件呢

我真的很喜欢听歌

然后就有了基于腾讯的API开发的简单化QQ MUSIC

# Music Linux Client

一个给 Linux Mint 用的简化音乐客户端。它主要使用 QQ 音乐和网易云音乐网页接口做搜索、歌词、播放链接和歌单读取，然后把音频 URL 交给本机播放器播放。

## 说明

- 不绕过会员、DRM、地区限制、登录限制或付费限制。
- 当前版本主要使用 QQ 音乐网页接口；`QQMUSIC_API_BASE` 兼容参数仍保留，但核心的搜索、登录、歌单和播放链接不依赖第三方 API。
- 搜索栏旁边可以在 QQ 音乐和网易云音乐之间切换。网易云音乐当前支持搜索、歌词、播放链接、手机号验证码登录、网页登录 Cookie 导入、歌单读取、歌单详情，以及自建歌单的新建、重命名、删除、加入歌曲和移除歌曲。
- 如果安装了 `python-vlc` 和 VLC/libVLC，会优先用内嵌 VLC 播放，避免每首歌弹出一个 VLC 窗口，并支持拖动进度条快进快退；否则回退到本机播放器。
- 登录信息保存在本机 `.qqmusic_auth.json`，不要把这个文件分享给别人。

## 运行前准备

这个项目只有一个 Python 脚本，基础功能主要使用 Python 标准库。图形界面需要 `tkinter`，微信扫码登录需要 `Pillow` 显示二维码，内嵌播放器是可选的。

最低建议：

- Python 3.10 或更新版本。
- 图形界面：`tkinter`。Linux Mint/Ubuntu 上通常安装 `python3-tk`。
- 微信扫码登录：`Pillow`。
- 播放器：建议安装 `vlc`、`mpv` 或 `ffmpeg` 里的 `ffplay`。如果都没有，程序会尝试用系统默认方式打开播放链接。
- 可选内嵌播放：`python-vlc` 加 VLC/libVLC。

### 直接使用系统 Python

Linux Mint/Ubuntu：

```bash
sudo apt update
sudo apt install python3 python3-tk python3-pil vlc
```

如果想启用内嵌 VLC 播放：

```bash
sudo apt install python3-vlc
```

然后运行：

```bash
python3 qqmusic_client.py
```

如果系统仓库里的 `python3-vlc` 不可用，也可以用虚拟环境安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install pillow python-vlc
python qqmusic_client.py
```

### 使用 uv

```bash
uv venv
source .venv/bin/activate
uv pip install pillow python-vlc
python qqmusic_client.py
```

如果只想使用外部播放器，不需要安装 `python-vlc`：

```bash
uv venv
source .venv/bin/activate
uv pip install pillow
python qqmusic_client.py
```

系统层面仍建议安装 `python3-tk` 和一个播放器：

```bash
sudo apt install python3-tk vlc
```

如果使用已有的 uv 虚拟环境 `common`，直接这样运行：

```bash
/home/zdy/.venvs/common/bin/python qqmusic_client.py
```

网易云手机号验证码登录依赖系统 `openssl` 命令做网页登录加密，不需要在 `common` 里额外安装 `cryptography`。

### 使用 Miniconda/Conda

```bash
conda create -n qqmusic-alt python=3.12 tk
conda activate qqmusic-alt
pip install pillow python-vlc
python qqmusic_client.py
```

如果 Conda 环境里没有 VLC/libVLC，可以在系统里安装 VLC：

```bash
sudo apt install vlc
```

## 图形界面

```bash
python3 qqmusic_client.py
```

登录后输入歌曲名或歌手，搜索，双击结果或点“播放”。底部播放器栏会显示当前歌曲、歌手、队列位置和播放进度，并提供上一首、播放、停止、下一首控制。播放模式可切换为顺序播放、随机播放或单曲循环，并会保存到设置文件。

在搜索栏旁边可以切换音乐平台，切换后搜索、歌词、播放、登录和歌单都会走对应平台。点击“设置”可以调整歌曲队列显示内容，例如是否显示歌手、专辑名、歌曲时长、MID，以及队列字体大小。设置会保存到 `.qqmusic_settings.json`，默认音质、下载目录和启动时是否自动同步歌单也会一起保存。

### 登录并同步歌单

在图形界面里的“设置”中点击“登录”或“重新登录”，程序会先让你选择 QQ 登录或微信登录。选择后用对应手机 App 扫描二维码并确认。登录成功后，程序会把登录信息保存到当前目录的 `.qqmusic_auth.json`，下次启动会自动恢复登录并同步歌单。

切换到网易云音乐后，在“设置”中点击“登录”会先让你选择登录方式：手机号验证码、导入网页登录 Cookie。手机号验证码可能会被网易云风控拦截。登录信息会保存到当前目录的 `.netease_auth.json`。

如果网易云提示“当前登录存在安全风险”，可以用“导入Cookie”登录：

1. 用浏览器打开 `https://music.163.com` 并登录。
2. 按 `F12` 打开开发者工具。
3. 点击 `Network` 或“网络”。
4. 刷新网页。
5. 点击任意 `music.163.com` 请求。
6. 在 `Request Headers` 或“请求标头”里找到 `Cookie`。
7. 复制 `Cookie:` 后面的整段内容，粘贴到客户端“导入Cookie”窗口。

复制出来的内容通常很长，里面至少要包含 `MUSIC_U`。

如果你想用微信或 QQ 登录网易云，请先在官方网页版选择微信/QQ登录，登录成功后按上面的步骤导入 Cookie。

之后点击“同步歌单”可以重新加载歌单。双击左侧歌单可以把歌单歌曲载入中间列表。需要切换账号时点击“退出登录”。

歌单区域支持新建、重命名和删除自建歌单。“我喜欢”和“已下载的歌曲”这类内置列表不能重命名或删除。重命名后程序会重新拉取歌单做校验，如果 QQ 音乐没有真正改名，会提示失败。

歌曲队列里选择一首歌后，可以加入左侧选中的 QQ 音乐歌单，包括“我喜欢”；如果左侧没有选中可加入的歌单，会弹出歌单选择窗口。搜索结果里的歌曲、已经打开的歌单里的歌曲，都可以加入另一个 QQ 音乐歌单。加入成功后会刷新歌单；如果正在查看目标歌单，会直接重新加载当前列表。

打开某个歌单后，可以把选中的歌曲从当前歌单移除。打开“已下载的歌曲”时，移除操作会删除本地下载文件。

播放时会根据歌词时间戳高亮当前歌词。`python-vlc` 后端能读取真实播放进度，并支持拖动进度条快进快退；外部播放器回退模式会用本地计时估算进度，但不能可靠控制外部播放器跳转。

## 命令行

扫码登录：

```bash
python3 qqmusic_client.py login
```

网易云发送手机验证码：

```bash
/home/zdy/.venvs/common/bin/python qqmusic_client.py --platform netease login --send-captcha --phone 13800138000
```

网易云手机号验证码登录：

```bash
/home/zdy/.venvs/common/bin/python qqmusic_client.py --platform netease login --phone 13800138000 --captcha 123456
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

指定播放器：

```bash
QQMUSIC_PLAYER=/usr/bin/vlc python3 qqmusic_client.py
```

保留的兼容 API 参数：

```bash
QQMUSIC_API_BASE=https://api.ygking.top python3 qqmusic_client.py
```

当前核心功能主要走 QQ 音乐网页接口；这个参数只建议在你明确知道自己需要兼容 API 时使用。

如果播放失败但搜索正常，通常是歌曲本身有会员、版权、地区或登录限制。可以尝试把音质切到 `128`。
