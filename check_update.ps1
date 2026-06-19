$url = 'https://github.com/CaHeK20021/interprex/releases/latest/download/latest.json'
try {
    $wc = New-Object System.Net.WebClient
    $res = $wc.DownloadString($url)
    Write-Host "OK:"
    Write-Host $res
} catch {
    Write-Host "FAIL: $($_.Exception.Message)"
}
