<#
.SYNOPSIS
    バッチ鮮度 watchdog（run_watchdog.ps1）を Windows タスクスケジューラへ登録する。

.DESCRIPTION
    夜間・月次バッチが**走らなかったこと**を検知する見張り（#515 手順3）。監視対象と
    別のタスクにするのが要点——run_nightly の中に置くと、nightly 自身が起動しなかった日は
    見張りも動かず、まさに検知したい事象だけが素通りする。

    既定の起動時刻は JST 20:00。**閾値が「cadence + 窓」の導出なので、この時刻は判定に
    影響しない**（夜間バッチが実行中でも窓の項がそれを吸収する）。選ぶ基準は
    「その時刻に PC が点いている確率」だけ——走らない監視は監視ではない。個人機では
    深夜より 20:00 の方が確度が高い。実測でも夜間バッチの起動が名目 17:20 に対し
    17:51〜19:01 へずれており、この時間帯は PC が起きている。

    **見張りを見張る役は作らない**（無限後退を打ち切る）。ここが読むのは app_settings の
    数行と gh の1〜2回だけで数秒で終わる。監視対象のバッチが外部 API を数時間叩くのに対し、
    こちらには死に方の面積が無い。

    **管理者権限（昇格）が必要。** LogonType S4U で登録するため、非昇格だと
    Register-ScheduledTask が HRESULT 0x80070005（Access is denied）で落ちる（#515）。

.PARAMETER Time
    起動時刻（既定 20:00）。

.PARAMETER TaskName
    タスク名（既定 financial_app-watchdog）。

.PARAMETER Minutes
    上限（既定 15分）。`check_batch_freshness.SELF_WINDOW_MIN` と一致すること（CI が照合）。

.PARAMETER Unregister
    登録を削除する。

.EXAMPLE
    PS> ./scripts/install_watchdog_task.ps1
    PS> ./scripts/install_watchdog_task.ps1 -Time 21:00
    PS> ./scripts/install_watchdog_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Time = "20:00",
    [string]$TaskName = "financial_app-watchdog",
    [int]$Minutes = 15,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "run_watchdog.ps1"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "削除しました: $TaskName" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $script)) {
    Write-Host "run_watchdog.ps1 が見つかりません: $script" -ForegroundColor Red
    exit 1
}

# S4U での登録には昇格が要る。非昇格だと Register-ScheduledTask が
# HRESULT 0x80070005（Access is denied）で落ちる（2026-08-25 実測）。ここで止めないと
# CIM の生エラーが出るだけで「何が足りないのか」が読めない。
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

$trigger = New-ScheduledTaskTrigger -Daily -At $Time

# S4U（Service For User）… 既定の InteractiveToken だと対話セッションに紐づくため、
# 対話コンソール側の CTRL_C 相当に巻き込まれて 0xC000013A（STATUS_CONTROL_C_EXIT）で
# 即死しうる（#515・2026-08-21 に夜間バッチで実測）。見張りが同じ理由で消えては話に
# ならないので、監視対象と同じ形で登録する。
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited

# StartWhenAvailable: 見逃した回を次の起動後に実行する（停止していた日を追いつく）
# ExecutionTimeLimit: 実体は app_settings 数行の読取と gh の1〜2回＝数秒。15分は
#   「ネットワークが詰まる余地」だけを見た値で、**24時間より十分小さいことが要点**。
#   MultipleInstances IgnoreNew の下では、固まった1本が翌日ぶんを抑止するため
# MultipleInstances IgnoreNew: 追いつき起動が前回と重ならないようにする
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $Minutes) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "financial_app バッチ鮮度 watchdog（#515・足跡の停止を検知して起票）" `
    -Force | Out-Null

# **登録できたことを確かめてから成功を出す。** ScheduledTasks の cmdlet は失敗しても
# 非終了エラーで返すため、確認せずに Write-Host すると「登録しました」と嘘をつく
# （install_monthly_task.ps1 の実装で実際に嘘をついた）。**見張りが登録できていないのに
# できたと表示する**のは、この仕組み全体の意味を消す嘘なので必ず読み戻す。
$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
[xml]$check = Export-ScheduledTask -TaskName $TaskName
$logon = $check.Task.Principals.Principal.LogonType
$swa   = $check.Task.Settings.StartWhenAvailable
$limit = $check.Task.Settings.ExecutionTimeLimit
# RunLevel は既定（LeastPrivilege）のとき Windows が XML から要素ごと省略する＝**空が正常**。
# -ne "LeastPrivilege" で見ると正しい登録を弾くので、昇格側だけを弾く。
$runlevel = $check.Task.Principals.Principal.RunLevel
if ($null -eq $info.NextRunTime)      { Write-Host "登録されたが次回実行時刻が無い（トリガ不正）" -ForegroundColor Red; exit 1 }
if ($logon -ne "S4U")                 { Write-Host "LogonType が $logon（期待 S4U）＝対話コンソールに巻き込まれる形のまま" -ForegroundColor Red; exit 1 }
if ($swa -ne "true")                  { Write-Host "StartWhenAvailable が乗っていない＝見逃した日を追いつけない" -ForegroundColor Red; exit 1 }
if ($limit -ne "PT${Minutes}M")       { Write-Host "ExecutionTimeLimit が $limit（期待 PT${Minutes}M）＝固まった見張りが翌日を抑止しうる" -ForegroundColor Red; exit 1 }
if ($runlevel -eq "HighestAvailable") { Write-Host "RunLevel が HighestAvailable＝watchdog が管理者権限で走る形になっている" -ForegroundColor Red; exit 1 }

Write-Host "登録しました: $TaskName（毎日 $Time・LogonType=S4U）" -ForegroundColor Green
Write-Host "  次回  : $($info.NextRunTime)" -ForegroundColor Cyan
Write-Host "  確認  : Get-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "  即実行: Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "  手動  : ./run_watchdog.ps1 -WarnOnly" -ForegroundColor Cyan
