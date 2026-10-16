param(
  [Parameter(Mandatory = $true)]
  [string]$Config,
  [string]$Python = "python"
)

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pipeline = Join-Path $scriptRoot "sentinel_l2a_pipeline.py"

if (-not (Test-Path -LiteralPath $Config)) {
  throw "找不到配置文件: $Config"
}

& $Python $pipeline --config $Config --verbose
if ($LASTEXITCODE -ne 0) {
  throw "处理失败，退出代码: $LASTEXITCODE。请查看 output_root\\logs 中的日志。"
}
