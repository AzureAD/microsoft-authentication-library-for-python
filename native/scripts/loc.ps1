$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Count-RustProductionLines {
    $Total = 0
    $Crates = @("cng-core", "openssl-provider-sys", "cng-provider", "mtls-bridge")
    foreach ($Crate in $Crates) {
        foreach ($File in Get-ChildItem (Join-Path $RepoRoot "crates\$Crate\src") -Filter "*.rs" -Recurse) {
            $Lines = Get-Content -LiteralPath $File.FullName
            $TestStart = 0
            for ($Index = 0; $Index -lt $Lines.Count; $Index++) {
                if ($Lines[$Index] -match '^\s*#\[cfg\(test\)\]') {
                    $TestStart = $Index
                    break
                }
            }
            if ($TestStart -eq 0) {
                $Total += $Lines.Count
            } else {
                $Total += $TestStart
            }
        }
    }
    return $Total
}

$RustLines = Count-RustProductionLines

[PSCustomObject]@{
    RustProductionPhysicalLines = $RustLines
} | Format-List
