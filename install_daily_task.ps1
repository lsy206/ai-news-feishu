param(
    [string]$TaskName = "DailyAiNewsFeishu",
    [string]$Time = "09:00"
)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = (Get-Command python -ErrorAction Stop).Source
$Script = Join-Path $ProjectDir "ai_news_feishu.py"

if (-not (Test-Path $Script)) {
    throw "Cannot find ai_news_feishu.py in $ProjectDir"
}

$Webhook = [Environment]::GetEnvironmentVariable("FEISHU_WEBHOOK_URL", "User")
$Secret = [Environment]::GetEnvironmentVariable("FEISHU_WEBHOOK_SECRET", "User")

if ([string]::IsNullOrWhiteSpace($Webhook)) {
    Write-Warning "User environment variable FEISHU_WEBHOOK_URL is empty. Set it before the scheduled task runs."
}

$Bootstrap = Join-Path $ProjectDir ".run_ai_news_feishu.ps1"
$BootstrapContent = @"
`$ErrorActionPreference = "Stop"
Set-Location "$ProjectDir"
`$env:FEISHU_WEBHOOK_URL = [Environment]::GetEnvironmentVariable("FEISHU_WEBHOOK_URL", "User")
`$env:FEISHU_WEBHOOK_SECRET = [Environment]::GetEnvironmentVariable("FEISHU_WEBHOOK_SECRET", "User")
& "$Python" "$Script"
"@

Set-Content -Path $Bootstrap -Value $BootstrapContent -Encoding UTF8

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Bootstrap`""
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Push daily AI news digest to Feishu." `
    -Force | Out-Null

Write-Host "Scheduled task '$TaskName' installed. It will run daily at $Time."
Write-Host "Project: $ProjectDir"
