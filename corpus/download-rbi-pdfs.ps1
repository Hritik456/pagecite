# Downloads the latest N RBI notification PDFs into a folder in one go.
# Run:  powershell -ExecutionPolicy Bypass -File .\download-rbi-pdfs.ps1
# Optional: -Count 60 -OutDir .\rbi-pdfs -StartId 13756
#
# v2: sends browser-like headers (Referer etc.), checks that every saved file
#     really is a PDF (starts with %PDF), and re-downloads broken ones.

param(
    [int]$Count   = 60,
    [string]$OutDir = ".\rbi-pdfs",
    [int]$StartId = 13756,   # latest notification Id seen on rbi.org.in (Nov 2026)
    [int]$MaxTries = 200      # how many Ids to walk back through at most
)

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'   # makes Invoke-WebRequest much faster on PS 5.1
$ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$badDir = Join-Path $OutDir "_not_pdf"

function Test-IsPdf([string]$path) {
    if (-not (Test-Path $path)) { return $false }
    $fs = [IO.File]::OpenRead((Resolve-Path $path).Path)
    try {
        $buf = New-Object byte[] 5
        $n = $fs.Read($buf, 0, 5)
        return ($n -ge 4 -and [Text.Encoding]::ASCII.GetString($buf, 0, 4) -eq '%PDF')
    } finally { $fs.Close() }
}

# A session keeps cookies from rbi.org.in, like a real browser would
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$session.UserAgent = $ua

$got = 0; $bad = 0
$id  = $StartId
$stopAt = $StartId - $MaxTries
$firstBadShown = $false

while ($got -lt $Count -and $id -gt $stopAt) {
    $page = "https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=$id&Mode=0"
    try {
        $html = (Invoke-WebRequest -Uri $page -WebSession $session -UseBasicParsing -TimeoutSec 30 `
                  -Headers @{ "Accept" = "text/html,application/xhtml+xml"; "Accept-Language" = "en-US,en;q=0.9" }).Content
        $m = [regex]::Match($html, 'https://rbidocs\.rbi\.org\.in/rdocs/notification/PDFs/[^"''\s>]+?\.pdf', 'IgnoreCase')
        if (-not $m.Success) { Write-Host "Id $id : no PDF link on page, skipping"; $id--; continue }

        $pdfUrl = $m.Value
        $file = Join-Path $OutDir ("{0}_{1}" -f $id, [IO.Path]::GetFileName($pdfUrl))

        if (-not (Test-IsPdf $file)) {
            Invoke-WebRequest -Uri $pdfUrl -WebSession $session -UseBasicParsing -OutFile $file -TimeoutSec 90 `
                -Headers @{ "Referer" = $page; "Accept" = "application/pdf,*/*"; "Accept-Language" = "en-US,en;q=0.9" }
        }

        if (Test-IsPdf $file) {
            $got++
            $kb = [math]::Round((Get-Item $file).Length / 1KB)
            Write-Host ("[{0}/{1}] Id {2} -> {3} ({4} KB)" -f $got, $Count, $id, (Split-Path $file -Leaf), $kb)
        } else {
            $bad++
            New-Item -ItemType Directory -Force -Path $badDir | Out-Null
            $dest = Join-Path $badDir ((Split-Path $file -Leaf) + ".html")
            Move-Item -Force $file $dest
            Write-Host "Id $id : server did NOT send a PDF (moved to _not_pdf)"
            if (-not $firstBadShown) {
                $firstBadShown = $true
                $peek = (Get-Content $dest -Raw -ErrorAction SilentlyContinue)
                if ($peek) { $peek = $peek.Substring(0, [Math]::Min(400, $peek.Length)) }
                Write-Host "----- what the server sent instead (first 400 chars) -----"
                Write-Host $peek
                Write-Host "-----------------------------------------------------------"
            }
        }
    } catch {
        Write-Host "Id $id : failed ($($_.Exception.Message)), skipping"
    }
    $id--
    Start-Sleep -Milliseconds 1200   # be polite to RBI's server
}

Write-Host ""
Write-Host "Done: $got real PDFs saved in $((Resolve-Path $OutDir).Path)"
if ($bad -gt 0) { Write-Host "$bad responses were not PDFs - they are in _not_pdf (safe to delete)" }
