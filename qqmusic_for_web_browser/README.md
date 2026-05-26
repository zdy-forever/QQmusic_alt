# QQ Music Web Browser

浏览器版 QQ 音乐客户端。前端运行在浏览器里，后端是本机 Python 服务，复用 `qqmusic_for_pc/qqmusic_client.py` 的 QQ 音乐接口实现。

## 启动

```bash
cd qqmusic_for_web_browser
python3 server.py
```

然后打开：

```text
http://127.0.0.1:8765
```

可选端口：

```bash
QQMUSIC_WEB_PORT=8787 python3 server.py
```

## 功能

- QQ / 微信扫码登录。
- 搜索歌曲、打开歌单、分页加载大歌单。
- 浏览器内播放、进度条拖动、上一首、下一首、停止。
- 顺序播放、随机播放、单曲循环。
- 歌词同步滚动和高亮。
- 新建、重命名、删除歌单。
- 加入歌单、从歌单移除。
- 下载当前歌曲。

## 说明

- 登录信息仍保存在 `qqmusic_for_pc/.qqmusic_auth.json`，Web 版和 PC 版共用。
- 浏览器不能可靠直连 QQ 音乐接口，所以本地后端负责请求 QQ 音乐、保存登录态和代理下载。
- 不绕过会员、DRM、地区限制、登录限制或付费限制。
