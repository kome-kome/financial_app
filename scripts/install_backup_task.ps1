<#
.SYNOPSIS
    週次バックアップ（run_backup.ps1）を Windows タスクスケジューラへ登録する。

.DESCRIPTION
    手順を人の記憶に置かないための登録スクリプト（#606）。**再現可能にしておかないと、
    PC を入れ替えた時点でバックアップが黙って消える**——そして失敗が出ないので誰も気づかない。
    それは自動化する前の状態（手で思い出したときだけ取る）へ戻ることを意味する。

    既定は毎週日曜 JST 21:00。根拠:
      夜間バッチ 17:20 開始・実測約70分 → 18:30 頃に終わる（衝突しない）
      21:00 は実測の電源オン窓（~17:40〜翌 ~08:00）の内側＝当日中に走る

    実効 RPO の上限を 7日で確定させるのが目的なので、曜日は何曜でもよい。日曜にしたのは
    月次バッチ（毎月1・2・3日の深夜）と重なる確率が最も低い曜日ではないが、
    -MultipleInstances IgnoreNew と別タスクなので重なっても互いを止めない。

    **登録ロジックを install_monthly_task.ps1 へ委譲できない。** あちらは月次専用の
    タスク XML を直接組み立てており（New-ScheduledTaskTrigger に -Monthly が無いため）、
    -Day を 1..28 に制限している。週次は cmdlet で表現できる（-Weekly -DaysOfWeek）ので、
    install_nightly_task.ps1 の形を写してトリガだけ差し替えた。

    **nightly 側をパラメータ化して共有する案は採っていない。** あちらはトリガ種別も
    実行時間上限も引数を持たない作りで、共有化には毎晩動いている本番経路そのものの
    書き換えが要る。バックアップを1本足すために稼働中の夜間バッチの登録経路を触るのは
    リスクの釣り合いが取れない。代わりに **登録後の検証4項目が nightly と同じであること**を
    tests/test_run_backup.py が照合し、片方だけ緩むのを防ぐ。

    **管理者権限（昇格）が必要。** LogonType S4U で登録するため、非昇格だと
    Register-ScheduledTask が HRESULT 0x80070005（Access is denied）で落ちる（#515）。

.PARAMETER Time
    起動時刻（既定 21:00）。

.PARAMETER DayOfWeek
    起動曜日（既定 Sunday）。

.PARAMETER TaskName
    タスク名（既定 financial_app-backup）。

.PARAMETER Hours
    ExecutionTimeLimit（時間・既定 2）。scripts/run_backup.py の WINDOW_MIN と対で、
    tests/test_run_backup.py が両者を突き合わせる。

.PARAMETER Unregister
    登録を削除する。

.EXAMPLE
    PS> ./scripts/install_backup_task.ps1
    PS> ./scripts/install_backup_task.ps1 -DayOfWeek Saturday
    PS> ./scripts/install_backup_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Time = "21:00",
    [string]$DayOfWeek = "Sunday",
    [string]$TaskName = "financial_app-backup",
    [int]$Hours = 2,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "run_backup.ps1"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "削除しました: $TaskName" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $script)) {
    Write-Host "run_backup.ps1 が見つかりません: $script" -ForegroundColor Red
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

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $Time

# S4U（Service For User）… 既定の InteractiveToken だと対話セッションに紐づくため、
# 対話コンソール側の CTRL_C 相当に巻き込まれて 0xC000013A（STATUS_CONTROL_C_EXIT）で
# 即死しうる（#515・2026-08-21 にログ0バイトで実測）。S4U はセッション0で走り、
# パスワードも保存しない。**副作用として環境が対話セッションと変わる**ので、
# 初回は .logs のログ先頭で venv・作業ディレクトリ・DB 接続先を必ず確認すること。
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited

# StartWhenAvailable: 見逃した回を次の起動後に実行する。21:00 は電源オン窓の内側なので
#   通常は素直に走るが、外出した日曜を落とさないために付ける。
# DontStopIfGoingOnBatteries / AllowStartIfOnBatteries: ノートでも走らせる
# ExecutionTimeLimit: 実測は数分規模だが、窓は run_backup.WINDOW_MIN と対で決める
#   （ADR-0040・実測から逆算しない）。ハングした回が居座っても次の実行まで7日ある。
# MultipleInstances IgnoreNew: 前回ぶんが走っている間に次が重ならないようにする
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours $Hours) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "financial_app 週次バックアップ（#606・正本=ローカルPG → Supabase Storage）" `
    -Force | Out-Null

# **登録できたことを確かめてから成功を出す。** ScheduledTasks の cmdlet は失敗しても
# 非終了エラーで返すため、確認せずに Write-Host すると「登録しました」と嘘をつく
# （install_monthly_task.ps1 の実装で実際に嘘をついた）。DB 書き込みを直接クエリで
# 検証するのと同じ話。S4U は「バッチ ジョブとしてログオン」権限を要求するので、
# ここで LogonType を見ないと権限不足に登録時点で気づけない。
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
if ($swa -ne "true")                  { Write-Host "StartWhenAvailable が乗っていない＝見逃した回を追いつけない" -ForegroundColor Red; exit 1 }
if ($runlevel -eq "HighestAvailable") { Write-Host "RunLevel が HighestAvailable＝バックアップが管理者権限で走る形になっている" -ForegroundColor Red; exit 1 }
if ($limit -ne "PT${Hours}H")         { Write-Host "ExecutionTimeLimit が $limit（期待 PT${Hours}H）＝窓とステップ予算が食い違う" -ForegroundColor Red; exit 1 }

Write-Host "登録しました: $TaskName（毎週 $DayOfWeek $Time・LogonType=S4U・上限 ${Hours}h）" -ForegroundColor Green
Write-Host "  次回  : $($info.NextRunTime)" -ForegroundColor Cyan
Write-Host "  確認  : Get-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "  即実行: Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "  ログ  : .logs ディレクトリの backup_YYYYMMDD.log" -ForegroundColor Cyan
Write-Host ""
Write-Host "  **登録しただけでは足跡が入りません。** 続けて1回手動実行してください（#584）:" -ForegroundColor Yellow
Write-Host "    ./run_backup.ps1" -ForegroundColor Yellow
