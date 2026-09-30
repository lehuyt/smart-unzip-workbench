# 智能解压工作台

当前版本：v1.2

面向 Windows 的批量解压工具，使用 PySide6 构建界面，可将压缩文件或文件夹拖入任务列表，并调用本机安装的 WinRAR 或 7-Zip 处理。

## 功能

- 支持拖放文件/文件夹、多个任务排队、暂停后继续处理。
- 支持常见压缩格式、分卷、伪装后缀和嵌套压缩包。
- 支持临时密码本优先匹配、普通密码本兜底，以及来源信息展示。
- 可在设置窗口指定 WinRAR、7-Zip 和密码本路径。
- 可在设置窗口检查 GitHub 最新正式版本；发现新版本后打开对应发布页。
- 工具栏“工具”窗口可拖入文件，快速修改/添加后缀或清除扩展名中的“删”字。
- “清除选中”和“清空列表”使用带边框的次级按钮样式。

## 使用

1. 从 GitHub 的 **Releases** 下载 Windows 发布 ZIP，并解压整个“智能解压工作台”目录。
2. 在电脑上安装 WinRAR 或 7-Zip；它们不包含在本程序中。
3. 双击“智能解压工作台.exe”，将待处理文件或文件夹拖入任务列表。
4. 点击“开始处理”。如果未自动发现解压引擎，在“设置”中指定其可执行文件路径。

公开 Git 仓库不包含本机密码本原件、个人配置或运行日志。公开 Release 会完整包含发布者的 `密码本.txt`，其中所有内容都可被下载者看到；仅在确认愿意公开时执行发布。程序仍可在“设置”中指向本机密码本文件，请勿将其提交到 Git 仓库。

## 从源码运行

需要 Windows、Python 3.14+ 和 PySide6。安装依赖后运行：

```powershell
python -m pip install PySide6
python "智能解压V1.0.py"
```

## 本地构建

```powershell
python -m pip install PyInstaller
python -m PyInstaller --noconfirm --distpath "dist_v1.2" --workpath "build_v1.2" "智能解压V1.0.spec"
$release = "dist_v1.2\智能解压工作台"
Copy-Item "package_resources\config.json" "$release\config.json"
Copy-Item "package_resources\使用说明.txt" "$release\使用说明.txt"
Copy-Item "密码本.txt" "$release\密码本.txt"
@'
###
文件名：xxx.zip
解压码：1234
原网页链接：https://www.xxx.work/xxx
网盘链接：https://pan.baidu.com/s/xxxxx
来源：【3D】xxx 20xx.x月 xxxxxxxx
保存：20xx-xx-xx xx:xx:xx · xx网盘
'@ | Set-Content -LiteralPath "$release\临时密码本.txt" -Encoding utf8
```

## 测试

```powershell
python -m unittest discover -s tests
```

本仓库不提供个人密码本。若需要密码本功能，请在本地建立并配置自己的文件；不要将它们加入版本控制。

## 设计规范

新增或调整页面前，请先阅读 [design.md](design.md)，并保持界面风格统一。
