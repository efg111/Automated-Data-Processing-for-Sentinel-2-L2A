# Sentinel-2 L2A 一键预处理

本工具以 Python + Rasterio/GDAL 完成 ENVI/ArcMap 的五步流程，不需要启动图形界面。中间结果为 ENVI `.dat + .hdr`，最终去云产品直接输出为 LZW 压缩的 GeoTIFF `.tif`；两者均保留投影、坐标系和参考波段的地理范围。每个 `.SAFE` 的每个 Granule 独立处理，因此适合整批下载目录。

## PowerShell 完整运行步骤（按顺序复制执行）

以下操作不需要管理员权限。请打开 **PowerShell**，而不是双击 `.py` 或 `.ps1` 文件。

### 1. 进入脚本目录

将目录改为实际存放本工具文件的目录；不要在 `C:\WINDOWS\system32` 中运行。

```powershell
Set-Location "C:\Users\user\Documents\Codex\2026-10-16\2-envi-1-envi-file-open-2\outputs"
```

用下面命令确认文件都在当前目录：

```powershell
Get-ChildItem .\sentinel_l2a_pipeline.py, .\config.example.json, .\requirements.txt
```

### 2. 确认可用的 Python

优先使用 Python Launcher 的 `py` 命令，它能避免 Windows Store 的空 `python.exe` 执行别名问题。

```powershell
py -0p
py --version
```

`py -0p` 应显示一个真实的 `python.exe` 路径。若没有输出或提示找不到 `py`，请安装正式版 Python，并在安装时勾选 **Add Python to PATH**，然后关闭并重新打开 PowerShell。若 `where.exe python` 只显示 `C:\Users\user\AppData\Local\Microsoft\WindowsApps\python.exe`，请在 Windows“设置 → 应用 → 高级应用设置 → 应用执行别名”中关闭 `python.exe` / `python3.exe` 的商店别名。

### 3. 创建并编辑配置文件

首次运行时复制示例配置；若已有 `config.json`，不要重复复制，以免覆盖已填写的路径。

```powershell
Copy-Item .\config.example.json .\config.json
notepad .\config.json
```

对于本项目的双层 SAFE 下载结构，推荐将 `input_paths` 指向**第二层**、即直接包含 `GRANULE` 的 SAFE；并设置一个单独的输出目录。路径使用 `/` 最简单：

```json
{
  "input_paths": [
    "F:/哨兵2号-L2A/S2B_MSIL2A_20250809T030529_N0511_R075_T50TMK_20250809T050830.SAFE/S2B_MSIL2A_20250809T030529_N0511_R075_T50TMK_20250809T050830.SAFE"
  ],
  "output_root": "F:/哨兵2号-L2A/processed",
  "reference_band": "B02",
  "source_crs_fallback": "EPSG:32650"
}
```

以上仅展示需要修改/确认的字段；其余字段保留示例配置内容。也可将 `input_paths` 写成包含多景 SAFE 的上级目录。**不要**分别填写 `R10m` 和 `R20m`：脚本会按 `band_source_resolution` 自动选择 R10m 的 B02/B03/B04/B08，以及 R20m 的 B05/B06/B07/B8A/B11/B12/SCL。

### 4. 安装依赖（首次运行一次）

Rasterio 的 Windows wheel 自带 GDAL、PROJ 和 JPEG2000 支持。请使用与后续运行完全相同的 `py` 安装：

```powershell
py -m ensurepip --upgrade
py -m pip install --upgrade pip
py -m pip install -r .\requirements.txt
py -c "import rasterio; print(rasterio.__version__)"
```

最后一条会打印一个 Rasterio 版本号。若报 `No module named rasterio`，说明依赖没有装进当前 Python；请重复本节命令，不要混用 `python`、`py`、Conda 和其它虚拟环境。

### 5. 处理前解决 PostgreSQL/PostGIS 的 PROJ 冲突（仅仍报错时执行）

脚本已自动处理这个问题。如果运行日志仍包含 `proj.db`、`DATABASE.LAYOUT.VERSION` 或 `PostgreSQL`，则在**同一个 PowerShell 窗口**执行：

```powershell
$proj = py -c "import rasterio,pathlib; print(pathlib.Path(rasterio.__file__).parent / 'proj_data')"
$env:PROJ_DATA = $proj
$env:PROJ_LIB = $proj
```

这只影响当前 PowerShell 窗口，会让 Rasterio 使用自身匹配的 PROJ 数据库，而非 PostGIS 的旧版数据库。

### 6. 正式运行

推荐直接运行 Python 主脚本，不受 PowerShell `.ps1` 执行策略影响：

```powershell
py .\sentinel_l2a_pipeline.py --config .\config.json --verbose
```

如果希望使用包装脚本，并看到“禁止运行脚本”的错误，先只对当前窗口临时放行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\run_pipeline.ps1 -Config .\config.json -Python py
```

`-Scope Process` 不会修改系统或用户的永久执行策略。无论使用哪种方式，**必须**传入 `--config .\config.json`（或 `-Config .\config.json`）；缺少该参数会出现 `the following arguments are required: --config`。

### 7. 检查结果

处理结束后查看配置中 `output_root` 指定的目录：

```powershell
Get-Content "F:/哨兵2号-L2A/processed/processing_manifest.json"
Get-ChildItem "F:/哨兵2号-L2A/processed" -Recurse -Filter "calibrated_cloudmasked.tif"
```

每景日志位于 `<output_root>/logs/`。单景失败不会中断其它景；处理清单会分别列出 `success` 和 `failed`。

## 独立批量处理多个 SAFE 产品

`batch_sentinel_l2a.py` 是独立的批量入口。它会递归查找批量根目录下所有**直接包含 `GRANULE` 的 `.SAFE` 目录**，因而可以正确处理 `SAFE/SAFE/GRANULE` 的双层解压结构；不会把外层和内层 SAFE 重复处理。每景仍调用与单景完全相同的重采样、合成、标定和 SCL 去云流程，按顺序运行以避免同时处理多景造成内存或磁盘空间不足。

### 批量运行前的配置

`batch_sentinel_l2a.py` 默认读取同目录的 `config.json`，其中的 `input_paths` 会被命令行 `--input-root` 覆盖。请保留波段、`band_source_resolution`、NoData 等处理参数。若批次包含不同 UTM 分区或南北半球的影像，请将：

```json
"source_crs_fallback": "AUTO"
```

不要为跨区域批次固定写成 `EPSG:32650`；脚本会优先读取每景 `MTD_TL.xml`，必要时再从每景 MGRS 瓦片编号恢复坐标系。

### 批量运行命令

先进入脚本目录：

```powershell
Set-Location "C:\Users\user\Documents\Codex\2026-10-16\2-envi-1-envi-file-open-2\Automated Processing of Sentinel-2 Data"
```

例如所有下载产品均放在 `F:/Sentinel2/L2A_batch` 下，成果放在短路径 `F:/S2OUT`：

```powershell
py .\batch_sentinel_l2a.py --input-root "F:/Sentinel2/L2A_batch" --output-root "F:/S2OUT" --config .\config.json --verbose
```

中途中断后，如不希望重写已完成成果，可增加 `--skip-existing`：

```powershell
py .\batch_sentinel_l2a.py --input-root "F:/Sentinel2/L2A_batch" --output-root "F:/S2OUT" --config .\config.json --skip-existing --verbose
```

批量清单写入 `F:/S2OUT/batch_processing_manifest.json`，日志写入 `F:/S2OUT/logs/batch_sentinel_l2a.log`。`success`、`skipped` 和 `failed` 三个字段分别记录完成、跳过和失败的景；单景失败不会停止其余产品。

## 输出与可追溯性

每景输出目录按以下顺序命名；文件名均带有景标识、波段和处理后缀。

| 目录 | 结果 |
| --- | --- |
| `01_resampled` | 原始值的统一 10 m 网格波段，如 `B02_10m_rs.dat` |
| `02_stack` | 坐标系 + 范围 + 像元大小定义的多波段合成，`stack.dat` |
| `03_calibrated` | 每个波段 `B02_bm.dat`，以及 `calibrated_stack.dat` |
| `04_mask` | `cloud_binary.dat` 和可直接用于掩膜的 `valid_mask.dat` |
| `05_final` | 仅有效像元的最终 GeoTIFF，`calibrated_cloudmasked.tif` |

`keep_intermediate=false` 时会删除 `01_resampled`、`02_stack`、`03_calibrated`，只保留掩膜和最终产品；清单会仍列出当次产物路径。

每景输出目录使用短景标识，例如 `S2B_T50TMK_20250809T030529`。这是为了避免把完整 SAFE 名称同时放入目录名和文件名，导致 Windows/ENVI 无法处理超过传统路径长度的文件。景标识、完整输入路径和每步输出路径仍完整记录在 `processing_manifest.json` 与日志中。

## 与手工参数的对应关系

- 脚本按后缀自动识别 10 m/20 m/30 m/其它分辨率；以第一个存在的配置波段（默认 B02）作为参考范围，并按 10 m 输出格网。10 m 保持 1:1，20 m 以最近邻重采样为 10 m（等效 ENVI Pixel Scale 0.5, 0.5）；30 m 以 1/3 比例处理；标准 Sentinel-2 的 60 m 波段同样会自动处理。所有波段写到同一坐标系统、范围、像元大小后再合成。
- 合成采用参考波段的 CRS、extent、pixel size，且重采样固定为 **Nearest Neighbor**。
- 标定逐波段严格实现：`raw==0 -> -1`；其余为 `clip(raw * 0.0001 - 0.1, 0, 1)`。这等价于提供的 Band Math 表达式 `((b1 ne 0)*(b1*0.0001-0.1))>0<1+(b1 eq 0)*(-1)`。
- SCL 使用未标定的、最近邻重采样后的原始类别值来生成掩膜（分类值不能用于反射率公式后再判断云）。默认 `[0,1,3,8,9,10]` 是无效值，即 NoData；其他类别为有效值。这正是给出的 ArcMap `Con` 中标为“云=1”的那些类别，随后用 SetNull/Extract by Mask 仅保留非云区域的等价实现。

## 关键配置

- `bands`：按需增删波段；`SCL` 必填。示例默认使用 R10m 的 B02/B03/B04/B08 与 R20m 的 B05/B06/B07/B8A/B11/B12/SCL；如加入 B10，L2A 通常没有该波段，脚本会记录缺失而继续。
- `band_source_resolution`：指定同名波段出现于多个分辨率目录时使用哪一份。示例适用于标准 R10m/R20m 处理：B02/B03/B04/B08 取 10 m，B05/B06/B07/B8A/B11/B12/SCL 取 20 m。这一项避免同名波段被错误地选成 R20m 或 R10m 的副本。
- `target_resolution_m`：统一输出分辨率，默认 10；修改时所有波段仍统一到同一参考范围。
- `reference_band`：定义“坐标系 + 范围 + 像元大小”的来源，默认 `B02`；这应与人工波段合成时选作 From Dataset 的参考影像一致。若该景缺少此波段，脚本记录该景失败而不悄悄改变地理范围。
- `source_crs_fallback`：默认 `AUTO`。若某些 GDAL/JP2 组合读取到像元坐标但没有读到 CRS，脚本优先解析 `MTD_TL.xml` 中的 `HORIZONTAL_CS_CODE`，随后从 Sentinel MGRS 瓦片号恢复 UTM CRS。也可显式设为如 `EPSG:32650`；T50TMK 对应的就是该值。
- `invalid_scl_classes`：云、云影、边界/坏像元类别。默认规则直接对应问题中的 ArcMap 条件。
- `final_nodata`：最终影像内云区写入值；默认 `-9999`，ENVI 头文件同时声明为 NoData。
- `keep_intermediate`、`log_file`：控制中间文件及日志名。

## 重要说明

题述 ArcMap `Con(...)` 本身输出的是“云=1、非云=0”，两种值都不是 NoData，不能单独拿去 Extract by Mask。脚本同时输出该诊断层 `cloud_binary`，以及真正可提取的 `valid_mask`（有效区=1、云区=NoData）；最终结果以 `valid_mask` 为准，符合“云、云影及缓冲区为 NoData，仅保留有效区”的目标。

“完全一致”应理解为数值公式、最近邻规则、格网和掩膜规则一致。ENVI/ArcMap 与 GDAL 写出的 `.hdr` 元数据排列可能不逐字节相同；首次投入生产前，建议选一景用 `gdalinfo -stats` 或 GIS 的栅格统计对比每个波段、投影、范围、分辨率和 NoData。

若机器安装了 PostgreSQL/PostGIS，可能有全局 `PROJ_LIB` 环境变量指向旧版 `proj.db`，导致 EPSG 查询报 `DATABASE.LAYOUT.VERSION` 错误。脚本会自动优先使用当前 Rasterio 包内的 `proj_data`；如运行的是旧版脚本，可在 PowerShell 中先执行：`$proj = py -c "import rasterio,pathlib; print(pathlib.Path(rasterio.__file__).parent / 'proj_data')"; $env:PROJ_DATA=$proj; $env:PROJ_LIB=$proj`。

若 ENVI 报 `Wildcard characters not allowed in host or share segments of Windows UNC file path`，并在路径前显示 `\\?\`，表示文件路径过长，GDAL 已使用 Windows 长路径前缀，而 ENVI 不支持该前缀。请将 `output_root` 设为很短的本地目录（例如 `F:/S2OUT`），使用本脚本的新版短输出文件名重新处理，然后在 ENVI 打开新生成的 `05_final/calibrated_cloudmasked.tif`。
