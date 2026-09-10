# 微信聊天归档部署

PostgreSQL 和 MinIO 在 Docker 中运行；微信监听程序必须运行在已登录微信的 Windows 宿主机。

```powershell
Copy-Item deploy\.env.example deploy\.env
# 编辑 deploy\.env，替换两个示例密码
pip install -e ".[storage,ai]"
.\deploy\start.ps1
.\deploy\run-listener.ps1
```

在 `deploy\.env` 中配置 `DASHSCOPE_API_KEY` 后，监听器会自动执行：

- 私聊文字或语音转写中包含 `robot` 时回复；
- 群聊只有微信 `msgsource/atuserlist` 确认真实 @ 当前账号时才回复，
  手动输入普通文本 `@robot` 不触发；
- 微信 SILK 语音转成 WAV 后保存到 MinIO，并通过
  `qwen3-asr-flash` 的 Base64 同步接口转写。

触发后还支持自然语言待办：

- `robot 后天晚上七点半和客户甲开会，提前20分钟提醒我`
- `robot 今晚八点看剧，提前10分钟提醒；今晚十点打游戏，提前5分钟提醒`
- `@robot 我想知道今天需要做哪些事情`
- `robot 删除后天和客户甲开会的待办`
- 在目标群真实 `@robot @李工 后天上午9点提醒他去工地铺线`，目标成员 wxid 直接取自真实 @
- 提醒其他成员必须真实 @；未额外 @成员时一律提醒创建者，“我”不会按成员名称匹配

一条消息可以创建多个待办。同一用户在同一时间重复发送相同事项时，按
最新的提醒时间更新；同一时间但事项不同，则保留原待办并等待用户确认。
替换确认按“用户 + 当前会话”保存 15 分钟：私聊回复 `robot 是`，群聊需
再次真实 @ 当前账号并回复“是”；回复“取消”则保留原待办。

删除不会立即执行：机器人会返回候选及编号，用户必须在 15 分钟内再次
发送 `robot 确认删除 #编号`；群聊中的命令需要真实 @。群聊用户只能查询、
删除自己的待办。群待办可指定当前群内的其他成员；创建时保存成员 wxid，
到期时按 wxid 重新读取其当前昵称并在原群真实 @，未指定时仍 @ 创建者。

临时停用模型调用可给监听命令追加 `--no-ai`。自动回复发送失败会记录为
`failed`，不会自动重复发送，以免微信界面状态不确定时造成重复消息。

## 飞书多维表格联调

在 `deploy/.env` 填写重置后的 `FEISHU_APP_SECRET`，并配置应用与目标
多维表格的编辑权限。初始化客户、项目表并验证关联记录：

```powershell
pip install -e ".[feishu]"
python -m wechatauto.feishu_setup --smoke
```

`--smoke` 会创建一个临时客户和一个关联项目，验证成功后立即删除；不会
留下测试记录。重复运行初始化不会重复创建同名业务表。

MinIO 控制台默认地址为 <http://127.0.0.1:9001>。停止基础设施：

```powershell
.\deploy\stop.ps1
```

`stop.ps1` 不删除 Docker 数据卷。迁移到另一台电脑时复制项目和重新创建 `.env`；如果还要迁移已有数据，需要另外备份 PostgreSQL 数据库和 MinIO Bucket。

当前 Compose 使用最后一代 MinIO Community 镜像，并将所有端口限定为本机访问。不要把端口绑定改为 `0.0.0.0` 后直接暴露到局域网或公网；需要网络化生产部署时应改用受支持的 MinIO AIStor 或其他仍在维护的 S3 服务。
