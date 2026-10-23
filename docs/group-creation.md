# 在 /link 中新建群聊并绑定

本功能基于 `topic-group-per-type`。新建的是**独立、非话题的私有超级群**，不是在已有群里创建话题。原有绑定、手动绑定和 `topic_group` 按类型分流保留。

## 安装与配置

在本分支仓库目录、运行 EFB 的同一 Python 环境安装：

```bash
python -m pip install -e '.[group-creation]'
```

在 ETM 的 `config.yaml` 增加以下**顶层配置**（与 `token`、`admins` 同级，不放进 `flags`）：

```yaml
group_creation:
  enabled: true
  api_id: 123456
  api_hash: "替换成你自己的 api_hash"
  owner_id: 123456789  # 创建群的 Telegram 用户 ID，也必须在 admins 中
  # 不填 owner_id 时，默认使用 admins 中的第一个用户
  # 可选代理，与 Bot 的代理配置分开：
  # proxy:
  #   type: socks5    # socks5、socks4 或 http
  #   host: 127.0.0.1
  #   port: 1080
  #   username: ""
  #   password: ""
```

从 https://my.telegram.org 获取 `api_id` 和 `api_hash`。建群使用用户账号，不能只配置 Bot Token。

## 首次登录

停止 EFB 后，在终端执行以下命令。替换实际路径；`--data-dir` 必须与 EFB 实际使用的 **blueset.telegram 频道数据目录**一致。默认 profile 通常如下，自定义 profile、实例或 `EFB_DATA_PATH` 请使用对应目录：

```bash
python -m efb_telegram_master.group_creator \
  --config "$HOME/.ehforwarderbot/profiles/default/blueset.telegram/config.yaml" \
  --data-dir "$HOME/.ehforwarderbot/profiles/default/blueset.telegram" \
  --login
```

按终端提示输入手机号、验证码及两步验证密码；必须登录 `owner_id` 对应的用户，不能输入 Bot Token。session 保存在 `group-creator.session`，Bot 回调不会弹出登录提示。授权失效后，停止 EFB，再执行此命令。

保管好 config 和 session，不要提交到 Git 或发送给他人。一个 session 只供一个 EFB 实例使用。

## 使用

1. 启动 EFB，由 `owner_id` 对应的管理员在 Bot **私聊**发送 `/link`。
2. 选中一个远端用户或群。
3. 点击「新建群聊并绑定」。已有绑定时显示「新建群聊并重新绑定」，点击会切换转发目标。
4. 后台使用远端聊天名称建群、邀请主 Bot、设置管理员权限，再执行原有绑定和历史处理流程。新群内的成功消息表示绑定完成。

默认关闭，只有 `enabled: true` 时显示按钮。其他管理员或群内的点击不会使用你的账号建群。Bot 必须允许加入群，Telegram 建群数量限制与 FloodWait 仍然适用。辅助 Bot 不会自动加入新群。

独立群绑定优先于默认话题群；未单独绑定的会话继续按 `topic_group` 分流。首次绑定沿用原有历史补发，重新绑定沿用原有历史链接策略。

## 失败与恢复

- 重复点击不会启动并发任务。
- 已取得群 ID 后，邀请 Bot、设置管理员或绑定失败：重试复用原群，重新发送 `/link` 或重启后也能读取记录。
- Telegram 明确拒绝建群（如 FloodWait）：等待后重试。
- 建群请求断网或进程退出，无法判断是否建好：暂停自动重建，防止重复。报错会显示远端标识。
- 已绑定但通知失败：提示检查新群，不会当作需要再次建群的失败。

遇到「结果不确定」，先检查 Telegram 是否已建好群。已建好时可将 Bot 加入并通过原有「手动绑定」完成绑定。确认不存在重复建群风险后，**停止 EFB**，使用报错中的完整远端标识清除本地记录：

```bash
python -m efb_telegram_master.group_creator \
  --config /absolute/path/to/config.yaml \
  --data-dir /absolute/path/to/blueset.telegram \
  --forget-pending '远端标识'
```

命令需要输入 `YES`，只清除本地建群记录，不删除 Telegram 群或 EFB 绑定。未确认前不要清除，否则下次点击可能再次建群。

`group-creation.sqlite3` 保存任务状态与群 ID，请与频道数据一起备份。本功能不会自动删除已建的群。
