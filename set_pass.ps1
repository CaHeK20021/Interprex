[System.Environment]::SetEnvironmentVariable('TAURI_SIGNING_PRIVATE_KEY_PASSWORD', '', 'User')
$p = [System.Environment]::GetEnvironmentVariable('TAURI_SIGNING_PRIVATE_KEY_PASSWORD', 'User')
if ($null -ne $p) { Write-Host "PASS_SET: '$p' ($($p.Length) chars)" } else { Write-Host "STILL_MISSING" }
