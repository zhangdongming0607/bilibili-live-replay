# 架构与接入自己的应用

`replay.py` 是云端编排器，`workflow.py` 提供字段验证、歌曲轴渲染、版本摘要与日志脱敏。`mac/worker.py` 通过 SSH 获取任务；主程序仅用 Python 标准库。录制和视频上传交给 biliup，模型仅负责分析本地任务材料。

## 文件协议

会话目录：`state.json`、`audio-N.ogg`、`asr-N.json`、`subtitle-N.srt`、`axis/axis.json`、`axis/歌曲轴.txt`、`axis/method.md`。

- `state.json.files`：最终视频绝对路径，按分 P 排列；`done` 是步骤执行成功，不是媒体完整性证明。
- `state.json.streamer`：主播名，建会话时从录像文件名开头取（biliup 默认“主播名 + 开播时间”）；认不出时为空。只用于选曲库和通知文字，不用于定位稿件。
- `axis-pending`：JSON 任务数组，包含 session、created_at、streamer、parts（part/duration/audio/asr）。
- `axis-start / axis-done / axis-fail`：工作端状态回传；axis-done 校验枚举、时长、顺序和证据，并从 JSON 重建公开文字。
- `review-pending`：当前版本摘要 `review_token`；确认必须匹配。摘要不是账号认证，审核人身份仍由配置的 sender ID 控制。
- `comment-request SESSION TOKEN`：显式请求评论，服务器再次核对当前内容与稿件审核状态。

歌曲轴 JSON 示例见 `examples/axis.json`。`part` 为整数，`start` 为该 P 的秒数；confidence 与听审状态不能混用。App 可以生成同样的 JSON 和 method.md，再走现有校验、审核流程。

## 状态与故障

进程锁防止同一会话流水线并行，更新 state 时加锁重读并原子替换。ASR 任务保存 task_id，可续查；太旧而且没有保存结果时重新提交会产生新费用。worker 保存本地完成标记，上传失败时复用结果。

远程上传结果目前不是事务式多文件提交，服务器读到不完整结果会校验失败，须重试；多个 Mac worker 不受全局租约保护，本版只运行一个。已确认或已发送评论的歌曲轴不应再由 worker 覆盖；修改已发布稿件是后续人工任务。

`comment` 的 sending / needs_review 表示发送结果可能不明，阻止自动重复发送。不要为了恢复把整份 state 删除。平台字幕接口也无法提供端到端 exactly-once 保证，首次使用需核对提交结果。

## 扩展点

- 换 ASR：提供同样的句/逐字时间，或替换 `step_asr` 与 `sentences_of`。
- 换 Agent：读取 parts，输出 axis.json/method.md，不需要赋予账号权限。
- 换通知：替换 `notify` 和审核输入适配器，保留版本码、审核人和重复发送保护。
- 换投稿平台：替换 archive、subtitle、comment 适配；录制端支持不等于本项目已经支持该平台发布。
- 做 App：为会话、证据、候选切点、字幕预览和发布闸口提供界面，不能把 CLI 当前状态当成“所有工作验收完成”。
