# 手动接流与单场续录

## 复用已有任务

先读工作区 `work/stop-live-watch`、`live-watch-status.json`、`live-watch-events.jsonl` 和现有脚本。用户明确开始或恢复录制后，核对遗留 FFmpeg 的目标文件，再移除停止标记并手动启动。仅查看状态、处理视频或创建技能时不删除标记。

本技能只录用户手动启动的当前一场。启动时查一次直播状态，未开播或启动查询失败就退出；该场结束后退出。没有等待开播、定时查询开播或自动录下一场的逻辑，不创建或恢复候播定时任务。

不要同时运行技能内脚本和旧脚本；更换程序前核对进程身份和存活录制，受控停止或明确接管。独占锁只覆盖同一工作区，另建工作区会绕开单实例保护，因此同一房间不另建重复录制。旧脚本若含候播逻辑，不沿用其启动入口。

## 新建录制

需要 macOS / POSIX、Python 3.9+、FFmpeg、FFprobe。本机 `caffeinate` 可防空闲休眠，不能让关机、手动休眠或断网的电脑持续录制。当前技能不使用 launchd；该案例曾因文稿目录权限失败，不能盲目重装服务。

在目标工作区创建 `work/live-watch-config.json`，值取自用户目标和实际工具路径。示例 UID / 房间号设为 0，必须填写目标直播间的真实值后才能启动：

```json
{
  "uid": 0,
  "room_id": 0,
  "anchor": "",
  "connection_check_seconds": 30,
  "min_free_gib": 15,
  "stall_seconds": 120,
  "timezone": "UTC",
  "ffmpeg": "ffmpeg",
  "ffprobe": "ffprobe"
}
```

从当前技能目录解析脚本绝对路径，用参数传递工作区，不要把用户工作区硬编码进技能：

```sh
python3 "$SKILL_DIR/scripts/live_watch.py" --workspace "$WORKSPACE" --probe
python3 "$SKILL_DIR/scripts/live_watch.py" --workspace "$WORKSPACE" --start
python3 "$SKILL_DIR/scripts/live_watch.py" --workspace "$WORKSPACE"
python3 "$SKILL_DIR/scripts/live_watch.py" --workspace "$WORKSPACE" --stop
```

`--probe` 是用户主动发起的单次查询，不录制。`--start` 手动启动脱离终端的单场录制，`--stop` 写停止标记并受控停止。无操作参数时只读本地状态。新工作区必须配置 UID / 房间号；没有默认主播。`--start` 输出 started 仅表示派生了进程，随后须检查日志、状态及文件增长。`--ensure` 候播入口已移除。

## 接流方法与限制

本地案例在 2026-10-10 实测：公开 `room/v1/Room/getRoomInfoOld?mid=UID` 的响应包含 `roomid`、`liveStatus`、`title`、`link`；从 link 查询串提取 `playurl_h264` 或 `h264_session`。脚本确认房间身份、状态类型和 `*.bilivideo.com` 地址后传给 FFmpeg。此接口及返回结构可能变化；无地址、风控、权限或网络错误均记录为错误，不能冒充下播或尝试绕过平台访问限制。必要时用当前官方页面和允许的浏览器操作核对。

直接保存原直播音视频，核心参数：`-rw_timeout 20000000`、正确 Referer、`-map 0:v:0 -map 0:a:0? -c copy`，fragmented MP4 使用 `+frag_keyframe+empty_moov+default_base_moof`。不需要屏幕录制弹幕和浏览器 UI。不要输出带签名的完整流 URL；FFmpeg 原始日志可能含流地址，分享日志前脱敏。

录制中每 30 秒只检查本地 FFmpeg、文件增长和磁盘空间，不定期访问直播状态接口。流进程退出或文件超过 120 秒不增长时，受控结束旧进程，再查询一次当前状态并取得新流地址，续写新 `part-NNN.mp4`，不覆盖旧片段。此时明确下播就结束；接口异常按当前场重连失败处理，不当作下播，连续失败 5 次后保留文件并退出。连续 5 次拉流未产生增长文件也退出。空间低于 15 GiB 时停止并退出，不自动删文件或等待恢复录制。

## 状态与结束交付

恢复当前录制时，先查真实进程、文件增长、日志和权限。旧 FFmpeg 仍在写时优先接管，不能另起录制。PID 可能复用，不能只凭 `kill -0`；核对完整命令和输出文件，身份不明先停在诊断。

报告开始前，两次相隔几秒核对文件大小增长，且 PID 命令确为该文件的 FFmpeg。`capture_started` 事件本身不是开始录到媒体的证明；同一 PID / 文件的接管不重复通知，续录片段属于当前场。

结束时读取 `live-watch-status.json` 和 `live-watch-events.jsonl`，按当前场交付文件链接、媒体时长、大小、断流/探测警告及是否做完整解码。正常录制不反复发消息，不宣称无缺失。

用户要求停止时写停止标记并核对相关进程退出。机器重启或这场结束后，需要用户再次明确开始录制；不会自动候播。
