$ErrorActionPreference = 'SilentlyContinue'
$status   = git status --porcelain 2>$null
$unpushed = git log '@{u}..' --oneline 2>$null
if ($status -or $unpushed) {
    Write-Host ''
    Write-Host '━━━━━━━━━━━━━━━━━━━━' -ForegroundColor Yellow
    Write-Host '  未コミット・未プッシュの変更があります' -ForegroundColor Yellow
    if ($status)   { Write-Host '  [未コミット]' -ForegroundColor Red;  $status   | ForEach-Object { Write-Host "    $_" } }
    if ($unpushed) { Write-Host '  [未プッシュ]' -ForegroundColor Cyan; $unpushed | ForEach-Object { Write-Host "    $_" } }
    Write-Host '  → git add / commit / push を忘れずに' -ForegroundColor Yellow
    Write-Host '━━━━━━━━━━━━━━━━━━━━' -ForegroundColor Yellow
}
