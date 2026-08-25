<#
.SYNOPSIS
    ローカル月次バッチ（run_monthly.ps1）を Windows タスクスケジューラへ登録する。

.DESCRIPTION
    #503 で GHA の cron を止めたあと、月次3本（tune / macro-beta / factor-premia）だけが
    どこからも回されない状態で残った（#504）。ここがその登録口。手順を人の記憶に置かない
    ためのスクリプトで、**再現可能にしておかないと PC を入れ替えた時点で黙って消える**——
    そして無実行は failure を出さないので誰も気づかない。

    既定の起動は「毎月1日 JST 01:00」。日次バッチ（JST 17:20 開始・上限6時間＝最悪 23:20 終了）
    の後ろで、翌日の日次 17:20 までに16時間の窓が空く。

    ExecutionTimeLimit は既定16時間＝その窓の幅そのもの。GHA 時代の timeout を直列に足すと
    最大22時間（tune 300+355+300 ＋ macro-beta 340 ＋ premia 20 分）だが、GHA ランナーは
    実質2コアでローカルは6コアあるため実測はこれより短くなる見込み。**打ち切られても
    軽い順に並べてあるので前方のステップは当月分が揃う**（factor_premia → macro_beta →
    tune は M-1 から）。実測が出たら窓・上限とも見直すこと。

    打ち切りは「失敗」としては現れない（タスクスケジューラがプロセスを止めるだけで、
    Issue も起票されない）。検知できるのは app_settings の monthly_last_success が
    進まないことだけなので、初回は必ずログ（.logs/monthly_YYYYMMDD.log）を見る。

.PARAMETER Time
    起動時刻（既定 01:00）。

.PARAMETER Day
    起動する日（既定 1＝毎月1日）。

.PARAMETER TaskName
    タスク名（既定 financial_app-monthly）。

.PARAMETER Hours
    ExecutionTimeLimit の時間数（既定 16）。

.PARAMETER Unregister
    登録を削除する。

.EXAMPLE
    PS> ./scripts/install_monthly_task.ps1
    PS> ./scripts/install_monthly_task.ps1 -Time 02:00 -Day 5
    PS> ./scripts/install_monthly_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Time = "01:00",
    [int]$Day = 1,
    [string]$TaskName = "financial_app-monthly",
    [int]$Hours = 16,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "run_monthly.ps1"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "削除しました: $TaskName" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $script)) {
    Write-Host "run_monthly.ps1 が見つかりません: $script" -ForegroundColor Red
    exit 1
}

if ($Day -lt 1 -or $Day -gt 28) {
    # 29-31 は無い月があり、その月だけ黙って走らない。28 までに制限する。
    Write-Host "Day は 1-28 を指定してください（29-31 は無い月がある）: $Day" -ForegroundColor Red
    exit 1
}

# -NoProfile … プロファイル読込で環境が変わるのを避ける（再現性）
# -ExecutionPolicy Bypass … 署名していないローカルスクリプトを走らせる
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $root

# 月次トリガの作り方について（全部試して分かったこと・次に触る人向け）:
#   - New-ScheduledTaskTrigger に -Monthly は無い（Daily/Weekly/Once/AtLogOn/AtStartup のみ）
#   - CIM の MSFT_TaskMonthlyTrigger を自分で組んで Register-ScheduledTask へ渡すと
#     "The parameter is incorrect"（プロパティは MonthOfYear の単数形だが、それでも通らない）
#   - schtasks で作った月次トリガを Get-ScheduledTask で取り出して渡し直しても同じ
#   - schtasks で作ってから Set-ScheduledTask で設定を乗せる、も同じく弾かれる
#     （しかも**非終了エラーなので $ErrorActionPreference=Stop でも止まらず**、
#       設定が乗らないまま「登録しました」と出た。2026-08-21 に実測）
# 残るのは **タスク定義 XML を直接渡す**道。CalendarTrigger/ScheduleByMonth なら月次を
# そのまま表現でき、schtasks では書けない StartWhenAvailable / ExecutionTimeLimit /
# MultipleInstancesPolicy も同じ XML に収まる＝1回の登録で完結する。
$parsed = [datetime]::ParseExact($Time, "HH:mm", $null)
$start = Get-Date -Day $Day -Hour $parsed.Hour -Minute $parsed.Minute -Second 0 -Millisecond 0
if ($start -lt (Get-Date)) { $start = $start.AddMonths(1) }

$months = @("January","February","March","April","May","June",
            "July","August","September","October","November","December")
$monthsXml = ($months | ForEach-Object { "          <$_ />" }) -join "`n"

# StartWhenAvailable: 見逃した回を次の起動後に実行する（スリープ・停止していた月の追いつき）。
#   月次は1か月に1度しか機会が無いので、ここが無いと丸ごと1か月固着する。
# ExecutionTimeLimit: 日次の開始（翌 17:20）に食い込まないための窓（既定16時間）。
# MultipleInstancesPolicy IgnoreNew: 前回ぶんが走っている間に次が重ならないようにする。
# LogonType S4U: 既定の InteractiveToken だと対話セッションに紐づくため、対話コンソール側の
#   CTRL_C 相当に巻き込まれて 0xC000013A（STATUS_CONTROL_C_EXIT）で即死しうる（#515）。
#   S4U はセッション0で走りパスワードも保存しない。**環境が対話セッションと変わる**ので、
#   初回は .logs のログ先頭で venv・作業ディレクトリ・DB 接続先を必ず確認すること。
$xml = @"
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>financial_app ローカル月次バッチ（#504・正本=ローカルPG）</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$($start.ToString("yyyy-MM-ddTHH:mm:ss"))</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByMonth>
        <DaysOfMonth>
          <Day>$Day</Day>
        </DaysOfMonth>
        <Months>
$monthsXml
        </Months>
      </ScheduleByMonth>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$env:USERDOMAIN\$env:USERNAME</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT${Hours}H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -File "$script"</Arguments>
      <WorkingDirectory>$root</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null

# **登録できたことを確かめてから成功を出す。** ScheduledTasks の cmdlet は失敗しても
# 非終了エラーで返すため、確認せずに Write-Host すると「登録しました」と嘘をつく
# （実際にこの手前の実装で嘘をついた）。DB 書き込みを直接クエリで検証するのと同じ話。
$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
[xml]$check = Export-ScheduledTask -TaskName $TaskName
$limit = $check.Task.Settings.ExecutionTimeLimit
$swa   = $check.Task.Settings.StartWhenAvailable
$days  = $check.Task.Triggers.CalendarTrigger.ScheduleByMonth.DaysOfMonth.Day
$logon = $check.Task.Principals.Principal.LogonType
if ($null -eq $info.NextRunTime) { Write-Host "登録されたが次回実行時刻が無い（トリガ不正）" -ForegroundColor Red; exit 1 }
if ("$days" -ne "$Day")          { Write-Host "月次トリガの日が $days になっている（期待 $Day）" -ForegroundColor Red; exit 1 }
if ($limit -ne "PT${Hours}H")    { Write-Host "ExecutionTimeLimit が $limit（期待 PT${Hours}H）" -ForegroundColor Red; exit 1 }
if ($swa -ne "true")             { Write-Host "StartWhenAvailable が乗っていない＝見逃した月を追いつけない" -ForegroundColor Red; exit 1 }
if ($logon -ne "S4U")            { Write-Host "LogonType が $logon（期待 S4U）＝対話コンソールに巻き込まれる形のまま" -ForegroundColor Red; exit 1 }

Write-Host "登録しました: $TaskName（毎月 $Day 日 $Time・上限 $Hours 時間・LogonType=S4U）" -ForegroundColor Green
Write-Host "  次回  : $($info.NextRunTime)" -ForegroundColor Cyan
Write-Host "  確認  : Get-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "  即実行: Start-ScheduledTask -TaskName $TaskName" -ForegroundColor Cyan
Write-Host "  ログ  : .logs ディレクトリの monthly_YYYYMMDD.log" -ForegroundColor Cyan
