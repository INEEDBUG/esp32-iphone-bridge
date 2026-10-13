# PhoneLab

PhoneLab 是一个面向 Windows 的实验项目，用 USB 读取 iPhone 屏幕截图，并研究一个独立 ESP32 BLE HID 固件对合成测试页面的有限输入。它不提供通用手机自动化，也不判断设备当前是否已解锁。

当前代码默认只启动本机服务，输入后端保持关闭；普通启动不会打开串口或发送键盘、鼠标报告。USB 截图需要通过明确的 `-CaptureUsb` 选项启动，只采集画面，不发送设备输入。实验 BLE 输入是独立、一次性且受限的功能，只有在显式调用相应实验工具时才会打开串口。

项目仍处于实验阶段。当前公开进展只覆盖少量合成页面目标和受限输入检查；完整 25 个目标矩阵、不同设备/系统的兼容性和通用自动化能力均未完成。通用控制器在锁状态为 `UNKNOWN` 时拒绝输入。独立合成页实验需要现场解锁确认和持续有效的真实页面画面，仍不把锁状态标成 `UNLOCKED`。

## 已验证边界

当前开发记录来自 Windows 和一台 iPhone 11 / iOS 17.5.1 的受限测试；这不是广泛兼容性声明。固件曾用于实验，首次使用前必须单独核验并留存完整板载备份，并单独批准任何写入。备份恢复尚未通过真实恢复测试。普通构建脚本只编译，不上传、擦除或打开串口。

输入只面向当前被绑定的合成 PhoneLab 页面。报告 `executed` 只表示固件/协议路径接受了请求，不证明 iOS 应用已按预期响应。每次实验都应查看前后截图及浏览器报告事件；事件记录不是硬件来源证明。鼠标坐标标定、文本输入、拖动和完整页面矩阵仍有未验证部分。

## Windows 本机运行

需要 Windows、Python 3.13、PowerShell 和项目锁定的 Python 依赖。Windows OCR 使用系统提供的 OCR API。建议把运行数据放在仓库之外，并在提交前检查本地配置与忽略规则。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
$env:PHONELAB_DATA_ROOT = Join-Path $env:LOCALAPPDATA 'PhoneLab\data'
pwsh -NoProfile -File .\Start-PhoneLab.ps1
```

默认服务仅绑定本机回环地址。它不会自动启用 LAN 代理。若需只读 USB 截图，另行显式启动：

```powershell
pwsh -NoProfile -File .\Start-PhoneLab.ps1 -CaptureUsb
```

可用 `PHONELAB_PWSH` 指定本机 PowerShell 路径。也可在被忽略的 `.phonelab-local.json` 中设置非秘密路径（`data_root`、`pwsh`、`python_source`）；环境变量优先于该文件。不要把本机路径配置、截图、日志、配对缓存、令牌、序列号或备份提交到 Git。

USB 采集还需要本机已有的 Apple 驱动、有效信任记录和经确认的私密目标绑定。代码目前固定核验实验机型与系统版本，缺少绑定时会拒绝采集；本仓库不包含可直接用于其他设备的身份或部署配置。Windows OCR 使用 Windows PowerShell 5 的系统 OCR API，PowerShell 7 仍用于其他脚本。

正常退出服务或需要停止当前操作时，运行：

```powershell
.\.venv\Scripts\python.exe -m phonelab stop
```

该命令请求项目运行时停止，并非对手机端释放状态的独立证明。遇到不确定的输入结果时，不要自动重试；先关闭当前实验并重新观察。

## 固件构建

WROOM-32 的实验目标使用 BLE HID over GATT 与本机串口 JSON 行协议，不启动 Wi-Fi 或 TCP。S3 上游固件保存在独立目录，两个固件目标不能混为一谈。

```powershell
pwsh -NoProfile -File .\scripts\Build-Firmware.ps1
```

脚本只做受版本和源文件摘要约束的编译。刷写、擦除及恢复是不同的高风险操作，不是安装或构建步骤。不要在没有可核验的完整板备份和针对本次写入的明确授权时执行。

## 测试与许可

运行项目测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

项目自有代码采用 GPL-3.0-or-later，见 [`LICENSE`](LICENSE) 与 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。仓库保留的 MIT 组件继续适用各自许可；Python 与固件工具依赖也有各自条件。不要将整个仓库描述成 MIT 或假设依赖许可相同。

实测范围和剩余工作记录在 [进展记录](docs/PROGRESS.md)。
