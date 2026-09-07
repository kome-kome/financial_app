<#
.SYNOPSIS
    平日日中バッチ（#618）を Windows タスクスケジューラへ登録する。

.DESCRIPTION
    平日 8:00 起動・上限8時間（16:00 まで）。夜間バッチ（17:20）まで 80分空ける。
    窓を 9時間に広げると余裕が 20分になり、日中枠が長引いた日に夜間とメモリを取り合う。
    それは 2026-09-07 に macro_beta の発散を 0 -> 344 回へ増やした条件そのものなので広げない。

    トリガは -Weekly -DaysOfWeek Monday..Friday（install_backup_task.ps1 と同じ形）。
    New-ScheduledTaskTrigger に -Monthly が無いため月次側は XML を直接組んでいるが、
    週次は cmdlet で表現できる。

    なぜ日中なのか: 本番の推論経路にはスレッド固定が無く、XLA が使うコア数は実行時の
    混み具合で変わる。人が PC を触らない時間帯に回すことが、結果の再現性そのものになる。

    管理者権限（昇格）が必要。LogonType S4U で登録するため、非昇格だと
    Register-ScheduledTask が HRESULT 0x80070005 で落ちる。

    **登録しただけでは足跡が入らない**（batch_freshness は足跡が無いと「走っていない」と
    判定する）。登録後に1回手動実行すること。キューが空でも足跡だけは入る。

.EXAMPLE
    PS> ./scripts/install_daytime_task.ps1
    PS> ./scripts/install_daytime_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Time = "08:00",
    [string[]]$DaysOfWeek = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"),
    [string]$TaskName = "financial_app-daytime",
    [int]$Hours = 8,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "run_daytime.ps1"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "削除しました: $TaskName" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $script)) {
    Write-Host "run_daytime.ps1 が見つかりません: $script" -ForegroundColor Red
    exit 1
}

# S4U での登録には昇格が要る。非昇格だと Register-ScheduledTask が
# HRESULT 0x80070005（Access is denied）で落ちる。ここで止めないと CIM の生エラーが
# 出るだけで「何が足りないのか」が読めない。
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "管理者権限で実行してください（LogonType S4U の登録には昇格が必要）" -ForegroundColor Red
    Write-Host "  例: Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-File','$PSCommandPath'" -ForegroundColor Cyan
    exit 1
}

# -NoProfile … プロファイル読込で環境が変わるのを避ける（再現性）
# -ExecutionPolicy Bypass … 署名していないローカルスクリプトを走らせる
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DaysOfWeek -At $Time

# S4U（Service For User）… 既定の InteractiveToken だと対話セッションに紐づくため、
# 対話コンソール側の CTRL_C 相当に巻き込まれて 0xC000013A で即死しうる（#515）。
# S4U はセッション0で走り、パスワードも保存しない。
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited

# StartWhenAvailable: 見逃した回を次の起動後に実行する。**日中枠では諸刃**——PC が
#   落ちていた日の回が、人が帰宅して電源を入れた夜に走ると「触らない時間帯」の前提が
#   崩れる。それでも付けるのは、外出が流れた日に丸1日ぶん進まないほうが痛いため。
#   夜に走り出したことはログの開始時刻で分かる。
# ExecutionTimeLimit: 窓は run_daytime.WINDOW_MIN と対で決める（ADR-0040）。
# MultipleInstances IgnoreNew: 前日ぶんが走っている間に次が重ならないようにする。
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours $Hours) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "financial_app 平日日中バッチ（#618・キューから1日1件）" `
    -Force | Out-Null

# **登録できたことを確かめてから成功を出す。** ScheduledTasks の cmdlet は失敗しても
# 非終了エラーで返すため、確認せずに Write-Host すると「登録しました」と嘘をつく。
$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
[xml]$check = Export-ScheduledTask -TaskName $TaskName
$logon = $check.Task.Principals.Principal.LogonType
$swa   = $check.Task.Settings.StartWhenAvailable
$limit = $check.Task.Settings.ExecutionTimeLimit
# RunLevel は既定（LeastPrivilege）のとき Windows が XML から要素ごと省略する＝空が正常。
$runlevel = $check.Task.Principals.Principal.RunLevel
if ($null -eq $info.NextRunTime)      { Write-Host "登録されたが次回実行時刻が無い（トリガ不正）" -ForegroundColor Red; exit 1 }
if ($logon -ne "S4U")                 { Write-Host "LogonType が $logon（期待 S4U）＝対話コンソールに巻き込まれる形のまま" -ForegroundColor Red; exit 1 }
if ($swa -ne "true")                  { Write-Host "StartWhenAvailable が乗っていない＝見逃した回を追いつけない" -ForegroundColor Red; exit 1 }
if ($runlevel -eq "HighestAvailable") { Write-Host "RunLevel が HighestAvailable＝日中バッチが管理者権限で走る形になっている" -ForegroundColor Red; exit 1 }
if ($limit -ne "PT${Hours}H")         { Write-Host "ExecutionTimeLimit が $limit（期待 PT${Hours}H）＝窓とステップ予算が食い違う" -ForegroundColor Red; exit 1 }

Write-Host "登録しました: $TaskName（平日 $Time・LogonType=S4U・上限 ${Hours}h）" -ForegroundColor Green
Write-Host "  次回  : $($info.NextRunTime)" -ForegroundColor Cyan
Write-Host "  ログ  : .logs ディレクトリの daytime_YYYYMMDD.log" -ForegroundColor Cyan
Write-Host "  キュー: ./run_daytime.ps1 -Queue" -ForegroundColor Cyan
Write-Host ""
Write-Host "  **登録しただけでは足跡が入りません。** 続けて1回手動実行してください:" -ForegroundColor Yellow
Write-Host "    ./run_daytime.ps1 -DryRun" -ForegroundColor Yellow
