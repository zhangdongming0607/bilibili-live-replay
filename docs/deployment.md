# 部署与恢复

## 环境

原工作流使用 Linux 云主机、biliup-cli 1.3.4、Python 3、FFmpeg，以及 Apple Silicon Mac。开源整理时远端主程序与本地源程序哈希一致；这不代表修改后的开源版已经在另一台云主机完成验收。发布验证记录见 [validation.md](validation.md)。

云端 Python 程序仅用标准库，要求 Python 3.9+；Mac 歌曲识别另需 Codex CLI、FFmpeg、`numpy`、`mlx-whisper` 和已下载的模型。非 Apple Silicon 可替换识别执行器，文件协议无需改变。

## 1. 配好 biliup

按 [biliup 官方项目](https://github.com/biliup/biliup) 安装对应平台二进制或 `@biliup/cli`。先记录 `biliup --version`，不要把 Python 旧版配置直接套到当前版本。

推荐工作目录 `/srv/biliup`。用 `biliup server --auth` 启动，只监听本机；通过 SSH 转发访问管理页：

```sh
ssh -L 19159:127.0.0.1:19159 replay-server
```

浏览器访问 `http://127.0.0.1:19159`，设置管理员密码，扫码登录投稿账号；创建投稿模板，填写转载来源、标题、分区、标签，并明确设置**仅自己可见**。添加你自己的直播间并绑定模板。

录制必须保留本地文件供后处理使用；上传后处理里移除自动删除原片的 `rm`。不要使用只有流式上传、没有落盘副本的配置。本仓库不自动删除录像，需自己设容量和保留策略。

`server/biliup.service` 是 systemd 模板：核对实际二进制位置与工作目录后再安装启用。它按原部署使用系统服务；首次安装和 `systemd-run` 需要对应权限。不要对正在录制或上传的现有服务直接重启。

## 2. 云端处理程序

把 `replay.py`、`workflow.py` 放到 `/srv/replay/`；只传这两个代码文件即可。把 `examples/server.env.example` 复制为 `/srv/replay/.env`，填写实际值并执行 `chmod 600 /srv/replay/.env`。

- `DASHSCOPE_API_KEY`：你的百炼 Key。会将抽出的整场音频上传到百炼临时存储并调用收费转写；不是全离线方案。
- `BILI_COOKIE_FILE`：biliup 生成的账号 JSON 的实际路径；示例名称不代表真实文件。
- `FEISHU_WEBHOOK` / `FEISHU_SECRET`：自定义群机器人地址与可选签名密钥。Webhook 只能通知，不能读取回复。
- `SUBTITLE_LAN=zh`：原工作流已遇到 `zh-Hans` 被拒；平台语言枚举仍需以实际返回为准。
- `REPLAY_HOME` 若不用 `/srv/replay`，需在调用程序前设置；配置文件位于该目录下。

自行执行 `python3 /srv/replay/replay.py notify-test` 验证机器人。这会真实发送消息。

## 3. 接入三个钩子

在 biliup 目标直播间的“处理流程”中配置 `run`：

| 阶段 | 命令 |
|---|---|
| 下载前处理 | `python3 /srv/replay/replay.py hook-start` |
| 下载后处理 | `python3 /srv/replay/replay.py hook-downloaded` |
| 上传后处理 | `python3 /srv/replay/replay.py hook-uploaded` |

上传后钩子通过标准输入接收本次视频绝对路径，每行一条；只有视频文件，不要传 XML 弹幕、封面或尚在写入的片段。按实际 biliup 版本先用短测试录像确认此输入格式。钩子建立新会话并启动独立 systemd 单元，重启 biliup 不会直接杀掉处理程序。

处理顺序是 `check → audio → asr → subtitles → axis_task → find_archive → subtitle_upload`。每步成功写入会话 `state.json`；歌曲轴登记后无需等 Mac，字幕分支继续。`check` 记录解码错误，不把“执行过检查”当作检查通过；有警告时应在最终公开前处理。

首次先用自己有权处理的短视频试到排队：

```sh
python3 /srv/replay/replay.py new-session /absolute/path/test.mp4
python3 /srv/replay/replay.py process /srv/replay/sessions/会话名 --until axis_task
python3 /srv/replay/replay.py axis-pending
```

重复运行带 `--until axis_task` 的命令不会越过该边界。转写会产生费用；离线检查请用仓库的 demo。

## 4. 绑定稿件并上传字幕

确认上传稿件的 BVID、每个分 P 与本地文件顺序。开源版不再根据模糊标题自动选择稿件：

```sh
python3 /srv/replay/replay.py bind-archive /srv/replay/sessions/会话名 你的BVID
python3 /srv/replay/replay.py start /srv/replay/sessions/会话名
```

绑定会读取稿件详情并检查分 P 数量；数量相同仍需要人工确认顺序和内容。首次绑定是在你自己的服务器上完成，不要在 issue 里贴 Cookie。字幕等待稿件通过审核后提交；超过 48 小时或明确拒稿会停止。仅自己可见的 `state=-50` 还要看 `had_passed`，不能只按 state 正负判断。

## 5. Mac 工作端

克隆本仓库，复制 `examples/mac.env.example` 为 `mac/.env` 并 `chmod 600`。配置 SSH 别名、飞书群、审核人的 sender ID、已有模型目录。首次 SSH 连接由你核对主机指纹并配置登录密钥。

安装并登录 [Codex CLI](https://learn.chatgpt.com/docs/non-interactive-mode)；安装 `numpy` 和 `mlx-whisper` 到自己的 Python 环境，提前下载所选模型。工作端不会代为下载大模型。未指定 `LIVE_REPLAY_CODEX_MODEL` 时沿用用户模型配置。

默认 `workspace-write`。某些 Apple Silicon/GPU 环境可能需要额外权限；可在隔离工作账户中自行明确配置 `danger-full-access`，但它没有文件沙箱，任务提示词不是权限隔离。工作端会继承当前 Codex 配置，应检查已连接工具的权限。

[飞书 CLI](https://github.com/larksuite/cli) 原适配版本为 1.0.16：先按其文档初始化，执行 `lark-cli auth login --domain im` 获得读取目标群所需权限。通过群消息 JSON 的 `sender.id` 设置 `LIVE_REPLAY_REVIEWERS`，不按显示名判断。没配群或审核人时，不处理回复。

```sh
python3 mac/worker.py --list
python3 mac/worker.py
```

任务在 `~/Library/Application Support/live-replay/jobs/`；包含输入音频、转写、生成过程和证据，属于私人数据。worker 会把仓库里的技能复制到任务目录，不依赖某人的全局技能路径。

手动完成一场后，可生成自己的 launchd 文件：

```sh
python3 tools/launchd.py --output work/org.bilibili-live-replay.worker.plist
```

它只生成文件，不自动安装。检查后复制到 `~/Library/LaunchAgents/` 并按 launchd 规范加载。生成文件包含你自己的绝对路径，不能提交 Git。默认 10 分钟执行一次，同一工作目录有锁。

## 6. 飞书审核

机器人发歌曲轴和确认码。指定审核人发送：

- `修改 确认码 第 5 条歌名改为……`
- `确认 确认码`

只有当前版本的确认码有效；普通“好”“确认”不会触发发布。修改和确认同时出现时优先修改，新版需要再次确认。确认只授权歌曲轴评论/置顶，不改变稿件可见性。

评论发送前持久化状态，每段成功记录评论 ID。网络中断导致结果不明时标记 `needs_review` 或保留 `sending`，不自动重发；先人工查看评论区再处理状态。不要直接删状态然后盲目重跑。

## 恢复与首次部署验收

- 查看会话 `process.log`、`state.json` 和 systemd 单元 `replay-会话名`；解决错误后重跑 `process`/`start`。
- 会话已完成的步骤会跳过；不要换了媒体还沿用旧会话。重剪、重投或改变分 P 后建新会话。
- Mac 失败最多自动尝试两次；失联任务四小时后可重领。多台 Mac 同时认领尚无租约协调，本版只支持一个 worker。
- 字幕提交成功不是字幕已经显示；评论接口成功也需到页面查看。最终公开验收见 [publishing.md](publishing.md)。
- 首次部署至少核对一场：自动开录、文件增长、私密稿件、BVID 绑定、字幕可见、Mac 结果、飞书修改、正确评论、人工转公开。仓库测试不替你完成这些账号相关步骤。
