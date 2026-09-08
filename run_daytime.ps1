<#
.SYNOPSIS
    平日日中の枠で、重い計算をキューから1日1件ずつ進める（#618）。

.DESCRIPTION
    2026-09-07 に macro_beta を手動で回したところ、同一パネル・同一設定・同一コードなのに
    発散が 0 -> 344 回に増え、収束ゲートに落ちて隔離された。9/6 の run との差は、その7時間の
    裏で重いテストを並走させたことしか見当たらない。本番の推論経路にはスレッド固定が無く、
    XLA が使うコア数は実行時の混み具合で変わる。コア数が変われば浮動小数の加算順序が変わり、
    NUTS は初期のごく小さな差が軌道を分岐させるので、発散の有無まで動きうる。

    つまり「重い計算の裏で作業をしない」という運用条件が、結果の再現性に直結している。
    人が会社に居て PC を触らない平日 8:00-16:00 は、その条件が構造的に満たされる唯一の
    時間帯で、そこを専用の枠にした。

    実体は scripts/run_daytime.py にある（骨格は scripts/batch_common.py と共有）。
    ここが薄い起動口に徹しているのは run_monthly_beta.ps1 と同じ理由。

    ログは .logs ディレクトリに daytime_YYYYMMDD.log として残る。

.PARAMETER DryRun
    実行計画だけ表示して何もしない（キューは減らさない）。

.PARAMETER Queue
    キューの中身を表示する。

.PARAMETER Enqueue
    末尾へ積む。カンマ区切りで複数可（beta / tune:macro_gbdt / tune:macro_dlm）。

.PARAMETER ClearQueue
    キューを空にする。

.PARAMETER NoIssue
    失敗しても Issue を起票しない。

.PARAMETER Now
    平日8時の枠を待たずに、次の1件を今すぐ消化する（休暇などで平日昼に PC を触れる日用）。

    **このスクリプトを直に走らせるのではなく、登録済みタスクを Start-ScheduledTask で叩く。**
    直に走らせるとプロセスがこの端末の子孫になり、ターミナルや IDE を閉じた瞬間に死ぬ
    （#515 と同型）。タスク経由ならセッション0・実行上限8時間・二重起動防止がそのまま効く。

    次の1件が「並走で結果が変わりうる」仕事（beta / tune:* / gate:*）なら、-Force が無い
    かぎり起動せずに止まる。手動キックは人が PC を触っている時間帯に叩かれるのが前提で、
    それはこのバッチが避けるために作られた条件そのものだから。

.PARAMETER Force
    -Now で、並走に敏感な仕事でも起動する。**叩いたら PC を触らないこと**が条件。

.PARAMETER TaskName
    -Now が叩くタスク名。既定は install_daytime_task.ps1 の既定と同じ。

.EXAMPLE
    PS> ./run_daytime.ps1 -Queue
    PS> ./run_daytime.ps1 -Enqueue beta
    PS> ./run_daytime.ps1 -Enqueue "beta,tune:macro_dlm"
    PS> ./run_daytime.ps1 -DryRun
    PS> ./run_daytime.ps1 -Now
    PS> ./run_daytime.ps1 -Now -Force
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Queue,
    [string]$Enqueue,
    [switch]$ClearQueue,
    [switch]$NoIssue,
    [switch]$Now,
    [switch]$Force,
    [string]$TaskName = "financial_app-daytime"
)

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
Set-Location $root

$py = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "venv が見つかりません: $py" -ForegroundColor Red
    exit 1
}

# ログの日本語が cp932 で落ちないようにする（タスクスケジューラ経由でも同じ）。
$env:PYTHONIOENCODING = "utf-8"

# 正本はローカル。#503 以降は既定も local だが、タスクスケジューラは呼び出し元の環境を
# 引き継ぐことがあるので明示する。
$env:FINAPP_DB_TARGET = "local"

# ローカル読取は Supabase の Egress を1バイトも使わない（run_local.ps1 と同じ理由）。
$env:FINAPP_EGRESS_ENFORCE = "0"
$env:FINAPP_EGRESS_LEDGER  = "0"

# ── -Now: 平日8時の枠を待たずに次の1件を消化する ───────────────────────────────
# **このスクリプトを直に走らせない。** 直だとプロセスがこの端末の子孫になり、ターミナルや
# IDE を閉じた瞬間に死ぬ（#515 と同型）。登録済みタスクを叩けばセッション0で走り、
# ExecutionTimeLimit（8時間）と MultipleInstances=IgnoreNew もそのまま効く。
if ($Now) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "タスクが未登録です: $TaskName" -ForegroundColor Red
        Write-Host "  先に管理者権限で ./scripts/install_daytime_task.ps1 を実行してください" -ForegroundColor Cyan
        exit 1
    }

    # 走っている最中に叩くと、キューの2件目を巻き込んで重い計算が2本並ぶ。IgnoreNew は
    # 起動要求を無視するが、**無視されたことは戻り値に出ない**ので、ここで見て理由ごと出す。
    if ($task.State -eq "Running") {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "既に実行中です: $TaskName（開始 $($info.LastRunTime)）" -ForegroundColor Yellow
        Write-Host "  終わるまで待ってください。進捗: .logs\daytime_$(Get-Date -Format 'yyyyMMdd').log" -ForegroundColor Cyan
        exit 0
    }

    # 次の1件を見る（**キューは減らさない**＝見た結果として走らせないことがある）。
    # egress の要約行が前後に混じるので JSON 行だけ拾う。
    $peekRaw = & $py @("-m", "scripts.run_daytime", "--peek")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "キューを読めませんでした（exit=$LASTEXITCODE）" -ForegroundColor Red
        exit 1
    }
    $peekLine = $peekRaw | Where-Object { $_.TrimStart().StartsWith("{") } | Select-Object -First 1
    if (-not $peekLine) {
        Write-Host "キューの状態を読めませんでした（--peek が JSON を返していない）" -ForegroundColor Red
        exit 1
    }
    $peek = $peekLine | ConvertFrom-Json

    if (-not $peek.key) {
        Write-Host "日中枠のキューが空です。積むには ./run_daytime.ps1 -Enqueue <名前>" -ForegroundColor Yellow
        exit 0
    }

    $label = "$($peek.key)（実測 $($peek.measured_min)分・残り $($peek.remaining)件）"
    if ($peek.sensitive -and -not $Force) {
        Write-Host ""
        Write-Host "次の1件は並走で結果が変わりうる仕事です: $label" -ForegroundColor Yellow
        if (-not $peek.known) {
            Write-Host "  （JOBS に定義が無い名前なので、判断材料が無い側として扱いました）" -ForegroundColor Yellow
        }
        Write-Host ""
        Write-Host "  2026-09-07 に macro_beta を裏作業ありで回したところ、同一パネル・同一設定・" -ForegroundColor Gray
        Write-Host "  同一コードなのに発散が 0 -> 344 回に増え、収束ゲートに落ちて隔離されました。" -ForegroundColor Gray
        Write-Host "  所要が延びるのではなく、結論そのものが変わります（#618）。" -ForegroundColor Gray
        Write-Host ""
        Write-Host "  叩いたあと PC を触らないなら: ./run_daytime.ps1 -Now -Force" -ForegroundColor Cyan
        Write-Host "  触るなら、平日8時の自動枠に任せてください。" -ForegroundColor Cyan
        exit 1
    }

    Write-Host "今すぐ1件を消化します: $label" -ForegroundColor Green
    if ($peek.sensitive) {
        Write-Host "  並走に敏感な仕事です。終わるまで PC を触らないでください。" -ForegroundColor Yellow
    }
    Start-ScheduledTask -TaskName $TaskName

    # **起動できたことを確かめてから成功を出す。** Start-ScheduledTask は起動しなくても
    # 例外を投げないので、確認しないと「起動しました」と嘘をつく（installer と同じ理由）。
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Seconds 2
        $after = Get-ScheduledTask -TaskName $TaskName
    } while ($after.State -ne "Running" -and (Get-Date) -lt $deadline)

    if ($after.State -ne "Running") {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "起動を確認できませんでした（State=$($after.State) LastTaskResult=$($info.LastTaskResult)）" -ForegroundColor Red
        Write-Host "  キューは減っている可能性があります。./run_daytime.ps1 -Queue で確認してください" -ForegroundColor Cyan
        exit 1
    }
    Write-Host "  起動しました（セッション0・上限8時間）" -ForegroundColor Green
    Write-Host "  ログ: .logs\daytime_$(Get-Date -Format 'yyyyMMdd').log" -ForegroundColor Cyan
    exit 0
}

$cmd = @("-m", "scripts.run_daytime")
if ($Queue)      { $cmd += "--queue" }
if ($ClearQueue) { $cmd += "--clear-queue" }
if ($Enqueue)    { $cmd += @("--enqueue", $Enqueue) }
if ($DryRun)     { $cmd += "--dry-run" }
if ($NoIssue)    { $cmd += "--no-issue" }

& $py @cmd
exit $LASTEXITCODE
