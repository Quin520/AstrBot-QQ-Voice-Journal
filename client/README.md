# B 端采集器

在参加 QQ 语音的 Windows 电脑上录音，将群聊播放与麦克风分别传给运行 AstrBot 的 A 电脑。不会登录 QQ、操控 QQ 或自动开始录音。

## 连接步骤

1. 两台电脑安装 Tailscale，登录同一个私有网络，保持在线。在 Tailscale 设备列表找到 **A** 的 `100.x.x.x` 地址。
2. **A**：启用语音手记插件的「B 端录音接收」后重启 AstrBot；打开本机语音手记面板，填写 `https://100.x.x.x:7845`（换成 A 的真实地址），导出 `A-连接.voicejournal`。将这个文件私下传给 B。
3. **B**：完整解压 Windows 采集器压缩包，双击 `VoiceJournalB.exe`，导入连接文件并测试连接，选择 QQ 使用的播放设备和麦克风。
4. B 用自己的 QQ 加入语音，戴好耳机，勾选录音确认，点击「开始录音并自动同步」。结束时点击「停止录音」，等待同步完成。在 A 审核每个声音组属于谁。

B 不用安装 Python 或任何模型。机器人账号留在 A，既不用加入语音，也不用在 B 再登录。

## 同步与故障处理

- 不是即时流式字幕。默认每 10 分钟上传两条音轨，停止时立即保存剩余片段；A 处理完成后显示结果。
- A 离线或网络中断：继续保存在 B，连接恢复或再次打开程序后补传。可以点击「立即补传」。关闭后不会后台偷偷录音，也不会自动恢复录音。
- 本地目录是 `%LOCALAPPDATA%\AstrBotVoiceJournalB`，其中包含连接凭据和录音，不能上传公共仓库。点击「打开本地录音」查看；成功同步后副本仍保留，请在停止录音、同步完成后自行删除不需要的记录文件夹。
- 同一分段重复上传只计一次。两条音轨共用一次会话和相对时间，麦克风声音也要审核，不会自动当作某人的身份事实。
- 麦克风或输出设备断开时会停止两条音轨并保留已采集部分。设备变化后刷新并重新选择。进程被强制终止时，已封口分段可继续补传；未封口的 `.tmp` 文件保留在本机供检查。
- 连接超时：确认 A 开机、AstrBot 正在运行、两台 Tailscale 均已连接、连接文件使用 A 的 Tailscale 地址。必要时在 A 的管理员 PowerShell 执行插件内 `tools/allow_tailscale_receiver.ps1`。
- 凭据无效：A 可能撤销过旧连接，重新导出并导入即可。证书指纹不匹配：先核实确实是自己的 A，再重新导出连接文件；不要随意关闭校验。
- 建议戴耳机。如果群聊播放又被麦克风收到，两条轨道里会出现重复声音；当前不声称具备通用声学回声消除能力。
- 连接文件只授权上传，不能在 B 上访问 A 的管理员面板、修改声纹归属或控制电脑。审核在 A 的本机面板进行。

## 从源码运行或打包

仅开发者需要安装 Python 3.12 x64。在仓库根目录执行：

```powershell
python -m venv .venv-client
.venv-client\Scripts\python -m pip install -r client\requirements.txt
.venv-client\Scripts\python client_entry.py
```

打包不需要 AstrBot、Torch 或 FunASR：

```powershell
.venv-client\Scripts\python -m pip install pyinstaller
.venv-client\Scripts\python -m PyInstaller --noconfirm --onedir --windowed --name VoiceJournalB --collect-all pyaudiowpatch client_entry.py
```

分发 `dist/VoiceJournalB` 整个目录，不要只复制 EXE。构建后可运行 `VoiceJournalB.exe --self-check check.json`，它只检查设备枚举和界面依赖，不启动录音。
