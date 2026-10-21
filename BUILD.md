# 打包与发布说明（SoundCapture v1.0.0）

这份文档面向「要把当前这版源码打成 exe、并发布到 GitHub」的人。
使用者看 `README.md` 和 `使用说明.txt` 就够了，不用读这里。

---

## 0. 打包产物长什么样

`SoundCapture.spec` 用的是 **onedir** 模式：

```
dist/
└─ SoundCapture/
   ├─ SoundCapture.exe       # 入口，双击就跑（无控制台窗口）
   └─ _internal/              # Python 运行时 + 依赖，不要拆开
```

整目录压成 zip 发出去（26 MB，压缩后约 11 MB）。**不要把 exe 单独拷出来**，
`_internal` 不在旁边它起不来。

---

## 1. 准备打包环境

打包用的 Python 必须同时具备 `tkinter` 和 `PyInstaller`。
建议单独开一个虚拟环境，别污染日常环境（下面以 Python 3.11 为例）：

```bat
cd 内录工作台
python -m venv .venv-build
.venv-build\Scripts\activate
pip install -r requirements-build.txt
```

`requirements-build.txt` = `PyAudioWPatch` + `pyinstaller` + `imageio-ffmpeg`（可选）。

> 本机实测：Python 3.11.5 + PyAudioWPatch 0.2.12.8 + PyInstaller 6.22.3 打包正常。
> 注意 Python 3.13 系列的官方包常常**不带 tkinter**，打包前先 `python -c "import tkinter"` 确认。

---

## 2. 打 exe

```bat
pyinstaller --noconfirm --clean SoundCapture.spec
```

打包前先让开旧产物，否则 PyInstaller 覆盖写旧目录可能失败：

```bat
if exist dist\SoundCapture move dist\SoundCapture dist\_old_SoundCapture
if exist build rmdir /s /q build
```

打完确认没问题再 `rmdir /s /q dist\_old_SoundCapture`。

---

## 3. 自检（发布前必跑）

打包环境最容易出问题的就是音频设备打不开，所以程序内置了自检：

```bat
dist\SoundCapture\SoundCapture.exe --selftest 3
```

它在软件根目录写 `_selftest.txt`，内容形如：

```
root=D:\...\内录工作台
devices=2 loopback=1 mic=1
sources=2
elapsed=3.00
err=None
track0 扬声器/听筒 (Realtek ...) rate=48000 ch=2 chunks=548 dur=3.00
track1 麦克风阵列 (...)          rate=48000 ch=2 chunks=310 dur=3.01
RESULT=OK
```

判定标准：

| 字段 | 期望 |
|---|---|
| `RESULT` | `OK`（`CHECK` / `FAIL` 都别发） |
| `loopback` | ≥ 1（没有回环设备就录不了内部声音） |
| `elapsed` | 接近你传的秒数（这里是 3.00，误差 1 秒内） |
| `err` | `None` |
| 每轨 `dur` | 都 ≈ 目标时长，说明两路都真的采到数据了 |

跑完把 `_selftest.txt` 和临时 wav 删掉（已在 `.gitignore` 里）。

---

## 4. ffmpeg：发不发、怎么发

程序只在**保存之后**用到 ffmpeg：转 MP3、模式③ 的合成成品、工具页全部功能。
录音本身（WAV）不需要它。

查找顺序写在源码第 90 行附近：先试一个写死的绝对路径，不存在就退回 PATH 里的 `ffmpeg`。
所以换机器时两条路：

1. **不随包（推荐）**：让使用者自己装 ffmpeg 并加进 PATH。包最小，许可上也没负担。
2. **随包**：把 `ffmpeg.exe` 复制进 `dist\SoundCapture\`，并把常量改成按 exe 自身定位：
   ```python
   FFMPEG = str(Path(sys.executable).with_name("ffmpeg.exe"))
   ```
   代价是多约 70 MB，且要注意 ffmpeg 的许可（随附二进制属于分发行为）。

没有 ffmpeg 时程序不会崩，只是相关功能产不出文件——README 里已经写明。

---

## 5. 源码运行时要注意的两处路径

- `SoundCapture.py` 的 `FFMPEG` 常量：换机器改成你自己的 ffmpeg（或确保 PATH 里有）。
- `启动.bat` 里的 `PY=` 一行：改成你自己的 `pythonw.exe`，否则双击会去一个不存在的路径。

---

## 6. 发布到 GitHub

### 6.1 仓库里放什么

> 不想手动敲命令的话，直接看 [`发布步骤.md`](发布步骤.md) 并双击 `首次提交.bat`，
> 它会替你问清楚用户名 / 邮箱 / 仓库名，然后完成下面的提交和打 tag。

源码 + 文档 + spec，二进制不进仓库（`.gitignore` 已排除 `dist/`、`build/`、`录音输出/`、
`_设备黑名单.json`、`_selftest*.txt`）。

```bat
git init
git config user.name  "<你的名字>"        :: 只在当前仓库生效，不动全局配置
git config user.email "<你的邮箱>"
git add .
git status                               :: 确认 dist/、录音输出/ 没被加进来
rem 提交信息用英文：Windows 下中文 commit message 在 GitHub 上容易乱码
git commit -m "feat: SoundCapture v1.0.0 - internal audio / mic / dual-track recording + floating bar"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

`git add .` 之后务必看一眼 `git status`：只应有源码、文档、spec、requirements 和 .gitignore；
出现 `dist/`、`build/`、`录音输出/` 说明 .gitignore 没生效，先排查再提交。

### 6.2 打 tag 并上传 exe

```bat
git tag -a v1.0.0 -m "v1.0.0"
git push origin v1.0.0
```

然后到 GitHub 仓库页面 → Releases → **Create a new release**：

- Tag：`v1.0.0`
- 标题：`内录工作台 SoundCapture v1.0.0`
- 附件：把 `dist\SoundCapture` 整目录压成 `SoundCapture-v1.0.0-win64.zip` 拖进去
- 说明正文可直接用下面这段：

```markdown
## 内录工作台 SoundCapture v1.0.0

Windows 电脑内部声音 / 麦克风录制工具。

### 这一版有什么
- 三种录制模式：仅内部声音、仅麦克风、内部+麦克风同时录（双轨 + 自动合成成品）
- 录制中可暂停 / 继续，暂停不计时长、不进文件
- 最小化后收成始终置顶的悬浮条：状态、计时、迷你电平、开始/暂停/停止都在上面
- 录音库管理 + 工具页：格式转换、裁剪淡入淡出、响度归一化、拼接、音频信息

### 安装
1. 下载下面的 zip，解压到任意文件夹
2. 双击 `SoundCapture.exe`
3. 需要 MP3 / 合成成品 / 工具页功能的话，本机还需要 ffmpeg（加入 PATH）

### 环境
Windows 10/11，无需安装 Python。
```

### 6.3 发布前 checklist

- [ ] `--selftest 3` 得到 `RESULT=OK`
- [ ] 双击 exe 能起来，最小化能变悬浮条，悬浮条上「▶ 开始」可点、能录能停
- [ ] 一次真实录制（三种模式各一次）文件都落到了 `录音输出/`
- [ ] 删掉打包目录里残留的 `_设备黑名单.json`、`_selftest*.txt`、测试录音
- [ ] LICENSE 版权行已换成你自己的名字
- [ ] README 里的下载链接指向你的仓库 Release 地址

---

## 7. 常见问题

**PyInstaller 报找不到 tkinter**
换带 tkinter 的 Python（3.11 官方安装器记得勾 tcl/tk），别用精简/嵌入式版本。

**打包完 exe 一点就闪退**
先用 `console=True` 重打一版看报错，常见原因是依赖没进包（把缺失模块写进 `spec` 的 `hiddenimports`）。

**杀软报毒**
PyInstaller 的 bootloader 是误报重灾区。可以在 spec 里把 `upx=True` 改成 `False` 重打，
通常能显著降低误报（代价是包大一点）。

**换电脑后录不到内部声音**
确认选的是回环（Loopback）设备，且该设备没被静音/禁用；
实在打不开的设备会被写进 `_设备黑名单.json`，删掉这个文件可恢复。
