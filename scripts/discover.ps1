<#
.SYNOPSIS
  AION 2 Global endpoint discovery / capture tool (Phase 0).

.DESCRIPTION
  Resolves a character by name, then captures every public endpoint for it:
  info, equipment, one item detail per equipped slot, and each Daevanion board.
  Writes raw JSON (BOM-free) to an output directory for use as test fixtures.

  Read-only. Public data only. No auth. Rate limited.

.EXAMPLE
  .\discover.ps1 -Name Mycharacter -Region eu

.EXAMPLE
  .\discover.ps1 -Name Mycharacter -Region eu -ServerId 1312 -OutDir ..\tests\fixtures\mine
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)] [string] $Name,
  [Parameter(Mandatory = $true)] [ValidateSet('nae','naw','eu','la','as')] [string] $Region,
  [int]    $ServerId,
  [string] $OutDir      = (Join-Path $PSScriptRoot '..\captures'),
  [int]    $DelaySeconds = 4
)

$ErrorActionPreference = 'Stop'
$UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
$NoBom = New-Object System.Text.UTF8Encoding $false

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path

# ---------------------------------------------------------------- helpers

function Invoke-Api {
  <# Raw HttpWebRequest so a pre-encoded characterId (%3D) is NOT double-encoded. #>
  param([string] $Url)

  $req = [System.Net.HttpWebRequest]::Create([System.Uri]::new($Url, [System.UriKind]::Absolute))
  $req.Method    = 'GET'
  $req.UserAgent = $UA
  $req.Accept    = 'application/json, text/plain, */*'
  $req.Referer   = 'https://aion2.plaync.com/'
  $req.Timeout   = 30000

  $resp = $req.GetResponse()
  try {
    $sr = [System.IO.StreamReader]::new($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)
    try { return $sr.ReadToEnd() } finally { $sr.Dispose() }
  } finally { $resp.Dispose() }
}

function Save-Capture {
  param([string] $FileName, [string] $Content)
  $path = Join-Path $OutDir $FileName
  [System.IO.File]::WriteAllText($path, $Content, $NoBom)
  Write-Host ("    saved {0,-34} {1,7} bytes" -f $FileName, $Content.Length) -ForegroundColor DarkGray
}

function Get-Capture {
  param([string] $Label, [string] $FileName, [string] $Url)
  Write-Host "  $Label" -ForegroundColor Cyan
  try {
    $body = Invoke-Api -Url $Url
    Save-Capture -FileName $FileName -Content $body
    Start-Sleep -Seconds $DelaySeconds
    return $body | ConvertFrom-Json
  }
  catch {
    $code = ''
    if ($_.Exception.InnerException -and $_.Exception.InnerException.Response) { $code = [int]$_.Exception.InnerException.Response.StatusCode }
    elseif ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
    Write-Host "    FAILED $code" -ForegroundColor Red
    Start-Sleep -Seconds $DelaySeconds
    return $null
  }
}

# ---------------------------------------------------------------- 1. search

Write-Host ""
Write-Host "AION 2 Global discovery - name='$Name' region='$Region'" -ForegroundColor Yellow
Write-Host "Output: $OutDir"
Write-Host ""

$searchUrl = "https://api-search.plaync.com/aion2global/search/v2/character" +
             "?keyword=$([System.Uri]::EscapeDataString($Name))&region=$Region&localeInfo=en-US&page=1&size=20"
if ($PSBoundParameters.ContainsKey('ServerId')) { $searchUrl += "&serverId=$ServerId" }

$search = Get-Capture -Label "search '$Name'" -FileName '00-search.json' -Url $searchUrl
if (-not $search) { throw "Search request failed." }

if ($search.list.Count -eq 0) {
  Write-Host ""
  Write-Host "No characters matched '$Name' in region '$Region'." -ForegroundColor Red
  Write-Host "Matching is case-insensitive substring. Check the region - serverId ranges differ per region:" -ForegroundColor Yellow
  Write-Host "  nae 11xx/21xx | naw 12xx/22xx | eu 13xx/23xx | la 14xx/24xx | as 15xx/25xx"
  return
}

# Strip the <strong> highlight tags the search API wraps around matches.
$hits = $search.list | ForEach-Object {
  [pscustomobject]@{
    CleanName   = ($_.name -replace '<[^>]+>', '')
    Level       = $_.level
    ServerName  = $_.serverName
    ServerId    = $_.serverId
    Region      = $_.region
    CharacterId = $_.characterId      # already percent-encoded - keep verbatim
  }
}

Write-Host ""
Write-Host "Matches:" -ForegroundColor Yellow
$i = 0
$hits | ForEach-Object {
  Write-Host ("  [{0}] {1,-18} lvl {2,-3} {3} ({4})" -f $i, $_.CleanName, $_.Level, $_.ServerName, $_.ServerId)
  $i++
}

# Prefer an exact name match, highest level, to avoid picking up substring hits.
$target = $hits | Where-Object { $_.CleanName -ieq $Name } | Sort-Object Level -Descending | Select-Object -First 1
if (-not $target) { $target = $hits | Sort-Object Level -Descending | Select-Object -First 1 }

Write-Host ""
Write-Host ("Selected: {0} lvl {1} on {2} ({3}) region {4}" -f `
  $target.CleanName, $target.Level, $target.ServerName, $target.ServerId, $target.Region) -ForegroundColor Green
Write-Host ("characterId: {0}" -f $target.CharacterId) -ForegroundColor DarkGray
Write-Host ""

$cid  = $target.CharacterId
$sid  = $target.ServerId
$base = "region=$Region&lang=en-US&characterId=$cid&serverId=$sid"

# ---------------------------------------------------------------- 2. info + equipment

$info = Get-Capture -Label 'character/info' -FileName '10-info.json' `
          -Url "https://aion2.plaync.com/api/character/info?$base"

$equip = Get-Capture -Label 'character/equipment' -FileName '12-equipment.json' `
          -Url "https://aion2.plaync.com/api/character/equipment?$base"

# ---------------------------------------------------------------- 3. per-slot item detail

if ($equip -and $equip.equipment.equipmentList) {
  $items = $equip.equipment.equipmentList
  Write-Host ""
  Write-Host "Item detail for $($items.Count) equipped slots" -ForegroundColor Yellow
  foreach ($it in $items) {
    $fn = "20-item-{0:d2}-{1}.json" -f $it.slotPos, ($it.slotPosName -replace '[^A-Za-z0-9]', '')
    Get-Capture -Label ("  slot {0,-2} {1,-10} {2} (+{3})" -f $it.slotPos, $it.slotPosName, $it.name, $it.enchantLevel) `
      -FileName $fn `
      -Url "https://aion2.plaync.com/api/character/equipment/item?$base&id=$($it.id)&enchantLevel=$($it.enchantLevel)&slotPos=$($it.slotPos)" | Out-Null
  }
}

# ---------------------------------------------------------------- 4. daevanion boards

if ($info -and $info.daevanion.boardList) {
  $boards = $info.daevanion.boardList | Where-Object { $_.open -eq 1 }
  Write-Host ""
  Write-Host "Daevanion detail for $($boards.Count) opened boards" -ForegroundColor Yellow
  foreach ($b in $boards) {
    Get-Capture -Label ("  board {0,-3} {1,-12} {2}/{3} nodes" -f $b.id, $b.name, $b.openNodeCount, $b.totalNodeCount) `
      -FileName ("22-daevanion-{0}-{1}.json" -f $b.id, ($b.name -replace '[^A-Za-z0-9]', '')) `
      -Url "https://aion2.plaync.com/api/character/daevanion/detail?$base&boardId=$($b.id)" | Out-Null
  }
}

# ---------------------------------------------------------------- 5. summary

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Yellow
if ($info) {
  $p = $info.profile
  Write-Host ("  {0}  {1}  level {2}  {3}" -f $p.characterName, $p.className, $p.characterLevel, $p.raceName)
  Write-Host ("  Combat Power : {0:N0}" -f $p.combatPower)
  $il = $info.stat.statList | Where-Object { $_.type -eq 'ItemLevel' }
  if ($il) { Write-Host ("  Item Level   : {0:N0}" -f $il.value) }
  Write-Host ""
  Write-Host "  Base stats (key on 'type', the 'name' field is localized):"
  $info.stat.statList | Where-Object { $_.type -in 'STR','DEX','INT','CON','AGI','WIS' } | ForEach-Object {
    Write-Host ("    {0,-4} {1,-14} {2}" -f $_.type, $_.name, $_.value)
  }
}
Write-Host ""
Write-Host "Config values for .env:" -ForegroundColor Green
Write-Host "  AION2_REGION=$Region"
Write-Host "  AION2_SERVER_ID=$sid"
Write-Host "  AION2_CHARACTER_ID=$cid"
Write-Host ""
Write-Host "Captured $((Get-ChildItem $OutDir -Filter *.json).Count) files to $OutDir"
