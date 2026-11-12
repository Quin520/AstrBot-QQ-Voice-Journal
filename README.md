# AstrBot QQ Voice Journal

在 Windows 上记录 QQ 群语音的 AstrBot 插件：手动采集、离线转写、试听确认说话人，再用已配置的模型生成通话复盘。支持本机录音，也支持另一台电脑采集后上传。

## 功能

- 手动采集电脑播放声音，或导入已有音频，按分段转写和整理。
- 试听声音组、关联 QQ 身份，积累可更正、撤销的声纹样本。
- 生成和导出复盘；可用独立 B 端采集器分别记录播放声音与麦克风。

## 快速开始

需要 Windows、AstrBot `>=4.29.1,<5`、已连接的 NapCat 和可用的总结模型。语音引擎使用独立 Python 环境，建议 Python 3.12，并预留数 GB 模型空间。

1. 在 AstrBot 插件管理中通过仓库链接安装：

   ```text
   https://github.com/Quin520/AstrBot-QQ-Voice-Journal
   ```

2. 填写管理者 QQ、目标群、QQ 平台实例 ID 和总结模型提供商 ID。
3. 在插件目录创建独立语音环境，将输出的 `engine_python` 和 `engine_cache` 填入配置：

   ```powershell
   python setup_runtime.py C:\BotRuntime\VoiceJournal
   ```

4. 重启 AstrBot，管理者私聊 `/语音手记` 获取面板入口。面板默认在 `http://127.0.0.1:7844/`，需要管理口令。
5. 在电脑 QQ 中加入语音，确认参与者知悉记录，选择 QQ 使用的输出设备并开始采集。结束后试听声音组、确认身份，再生成或更新复盘。

## 使用说明

| 方式 | 操作 |
| --- | --- |
| 本机采集 | 在运行 AstrBot 的电脑上播放通话声音，通过面板控制录音 |
| 两台电脑 | A 运行 AstrBot 和模型，B 用自己的 QQ 参与语音，通过采集器上传双音轨 |
| 导入音频 | 上传 WAV、MP3、M4A、FLAC、OGG、AAC 或 WEBM，最大 256 MB |

两台电脑使用时，通过 Tailscale 互通；A 启用 B 端接收，在面板导出连接文件；B 下载 [Windows 采集器](https://github.com/Quin520/AstrBot-QQ-Voice-Journal/releases/latest/download/VoiceJournal-B-Windows-x64.zip)，导入后测试连接。B 无需安装 AstrBot、Python 或模型，完整步骤见 [B 端说明](client/README.md)。

默认每 10 分钟处理一段，停止时处理余下音频，最长 120 分钟自动停录。B 端会保留录音副本和补传队列，成功同步后也需自行清理副本。

工具不会自动发起、加入或接听 QQ 通话，也不提供实时字幕。采集的是所选输出设备的声音，不限于 QQ；仅在手机上参加通话时，电脑无法直接录到声音。

说话人匹配只给候选，只有人工确认且质量合格的样本才进入声纹库。复盘默认留在面板，不自动发群或修改人物记忆；总结文本会发送给所选模型提供商，音频和声纹保留在录音及处理电脑上。管理口令、连接文件和录音不要公开。

## 开发

在仓库根目录执行：

```powershell
python -m pip install -r requirements.txt pytest ruff
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

语音引擎依赖通过 `setup_runtime.py` 安装到独立环境，不要装入 AstrBot 主环境。

## 文档

- [使用与配置](docs/usage.md)：双机连接、身份审核、数据管理和实现说明。
- [B 端采集器](client/README.md)：连接、补传、故障处理与打包。
- [配置项](_conf_schema.json)：录音、转写、匹配和接收服务设置。

源码采用 [MIT](LICENSE)；依赖和模型权重遵循各自许可证。
