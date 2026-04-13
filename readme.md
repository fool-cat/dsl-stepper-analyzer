# DSL 步进电机曲线分析工具

本目录提供一套针对 DSLogic/DSView `.dsl` 抓包文件的步进电机分析工具，用于从 `DIR` / `STEP` 信号生成速度、加速度、加加速度和距离报告。

核心脚本是 `dsl_stepper_speed.py`，另外还提供了：

- Windows 浏览器工作台
- Linux `zenity` 图形封装
- Windows 单文件 `exe` 打包脚本

## 功能概览

- 解析 DSLogic / DSView 导出的 `.dsl` 抓包文件
- 支持选择 `STEP` 上升沿或下降沿作为有效计步边沿
- 支持按 `DIR` 电平确定正负方向，也支持禁用 `DIR` 并固定按正方向解析
- 默认使用自动稳健滤波，不再强制手工设置滤波窗口
- 生成自包含 HTML 报告，打开后可直接查看交互式曲线
- 命令行模式可选导出 CSV
- Windows 网页工作台支持多个 `.dsl` 同时对比，也支持同一文件配置多组信号

## 文件说明

- `dsl_stepper_speed.py`
  命令行主程序，负责解析 `.dsl` 并输出 HTML / CSV。
- `dsl_stepper_speed_gui_win.py`
  Windows 浏览器工作台，启动本地 `http://127.0.0.1:随机端口/` 页面进行批量配置和对比分析。
- `dsl_stepper_speed_gui_win.bat`
  Windows 启动脚本，支持双击运行，也支持把 `.dsl` 直接拖到脚本上。
- `dsl_stepper_speed_gui_win.spec`
  PyInstaller 打包配置。
- `build_exe_win.bat`
  Windows 打包脚本，用于生成单文件 `exe`。
- `dsl_stepper_speed_gui.py`
  Linux 桌面图形封装，依赖 `zenity` 弹窗完成参数选择。
- `dsl_stepper_speed_gui.sh`
  Linux 启动脚本。
- `dsl_stepper_speed_gui.desktop`
  Linux 桌面启动器示例。

## 环境要求

### 通用

- Python 3
- 运行分析功能本身不依赖第三方 Python 包

### Windows 网页工作台

- Windows
- 默认浏览器可正常打开本地 `http://127.0.0.1:端口/` 页面

### Linux 图形封装

- Linux 桌面环境
- `zenity`
- 建议安装 `xdg-open`，便于生成后直接打开 HTML

## 快速开始

### 1. 命令行模式

最常用示例：

```bash
python dsl_stepper_speed.py demo.dsl --dir-probe 0 --step-probe 1
```

不使用 `DIR`，全部按正方向解析：

```bash
python dsl_stepper_speed.py demo.dsl --no-dir --step-probe 0
```

指定输出文件，并额外导出 CSV：

```bash
python dsl_stepper_speed.py demo.dsl --dir-probe 0 --step-probe 1 --out-html demo_speed.html --out-csv demo_speed.csv
```

补充物理参数，用于统计信息中的 RPM / mm/s 换算：

```bash
python dsl_stepper_speed.py demo.dsl --dir-probe 0 --step-probe 1 --steps-per-rev 3200 --mm-per-rev 8
```

默认输出行为：

- 若未指定 `--out-html`，输出为与输入同目录下的 `原文件名_speed.html`
- 只有显式传入 `--out-csv` 时才会生成 CSV

### 2. Linux 图形模式

运行：

```bash
./dsl_stepper_speed_gui.sh
```

或者直接传入文件：

```bash
./dsl_stepper_speed_gui.sh sample.dsl
```

流程如下：

1. 选择 `.dsl` 文件，或者把文件路径作为参数传入
2. 选择 `DIR` 通道，可选“未使用DIR”
3. 选择 `STEP` 通道
4. 选择 `DIR` 正方向极性
5. 选择 `STEP` 计步边沿
6. 生成 `原文件名_speed.html`

如果系统安装了 `xdg-open`，生成完成后可直接弹出打开 HTML。

### 3. Windows 网页工作台

直接双击：

- `dsl_stepper_speed_gui_win.bat`

或者把一个或多个 `.dsl` 文件直接拖到：

- `dsl_stepper_speed_gui_win.bat`
- `dsl_stepper_speed_gui_win.exe`（如果已经打包）

程序会：

1. 启动本地浏览器工作台
2. 自动打开浏览器页面
3. 预加载拖入的 `.dsl` 文件，或等待你在页面中继续添加文件

页面中可完成以下操作：

- 添加一个或多个 `.dsl` 文件
- 为每个文件配置一组或多组信号
- 每组单独选择 `DIR`、`STEP`
- `DIR` 可选“未使用DIR（默认正向）”
- 每组可独立设置 `DIR` 正方向极性和 `STEP` 计步边沿
- 点击“生成/刷新对比视图”后直接查看多个报告

说明：

- Windows 网页工作台默认只生成 HTML，不生成 CSV
- 默认使用自动稳健滤波，不再要求输入滤波窗口
- 同一 `.dsl` 文件可以添加多组信号，适合同文件多电机或多通道对比
- 浏览器工作台只监听本机 `127.0.0.1`
- 无操作约 15 分钟后，工作台会自动退出并清理临时文件

输出位置规则：

- 通过本地路径载入的 `.dsl`，会在原文件目录输出 `原文件名_组名_speed.html`
- 通过网页上传方式载入的 `.dsl`，会在程序临时目录生成 HTML，并可在页面中直接打开

## 命令行参数说明

- `--dir-probe`
  `DIR` 所在通道号，默认 `0`。
- `--step-probe`
  `STEP` 所在通道号，默认 `1`。
- `--no-dir`
  不采样 `DIR`，全部按正方向处理。
- `--dir-low-positive`
  把 `DIR=0` 视为正方向；默认是 `DIR=1` 为正方向。
- `--dir-sample-offset`
  指定在 `STEP` 边沿附近哪个采样点读取 `DIR`，默认 `-1` 表示在有效 `STEP` 边沿前一个采样点读取。
- `--step-edge {rising,falling}`
  选择使用 `STEP` 上升沿或下降沿计步。
- `--smooth`
  旧版手工滑动平均窗口。`<=1` 时会走当前默认的自动稳健滤波。
- `--steps-per-rev`
  设置后会在统计信息中增加 RPM 换算。
- `--mm-per-rev`
  与 `--steps-per-rev` 配合使用，在统计信息中增加 mm/s 换算。
- `--out-html`
  指定 HTML 输出路径。
- `--out-csv`
  指定 CSV 输出路径。

## 输出内容

生成的 HTML 报告为单文件、自包含页面，不依赖外部 JS 资源。页面中可查看：

- 速度曲线
- 加速度曲线
- 加加速度曲线
- 距离曲线
- 关键统计信息

如果使用命令行并显式开启 CSV 输出，CSV 中会包含：

- 时间 `t_s`
- 速度 `speed_steps_per_s`
- 加速度 `accel_steps_per_s2`
- 加加速度 `jerk_steps_per_s3`
- 距离 `distance_steps`
- 方向位 `dir_bit`

## 生成 Windows EXE

如果需要分发给没有 Python 环境的 Windows 用户，可以生成单文件 `exe`。

步骤：

1. 安装 Windows 版 Python 3
2. 建议勾选“Add Python to PATH”
3. 双击或命令行运行 `build_exe_win.bat`

脚本会自动：

- 查找可用的 Python
- 检查并安装 `PyInstaller`
- 清理旧版本输出
- 生成单文件可执行程序
- 复制一份到 `release` 目录

默认输出位置：

- `dist\dsl_stepper_speed_gui_win.exe`
- `release\dsl_stepper_speed_gui_win.exe`

补充说明：

- 这是单文件绿色版，首次运行时 PyInstaller 会把运行环境解压到系统临时目录，这属于正常现象
- 某些杀毒软件可能对自打包 `exe` 误报，必要时可加入白名单

## 注意事项

- `DIR` 和 `STEP` 不能选择同一个通道
- `dsl_stepper_speed_gui.py` 依赖 `zenity`，未安装时请直接使用命令行脚本
- Windows 网页工作台适合批量对比；如果你只想做单文件脚本化处理，优先使用命令行模式更直接
