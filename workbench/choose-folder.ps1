$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
try {
    $dialog.Description = $env:H3_FOLDER_TITLE
    $dialog.SelectedPath = $env:H3_OUTPUT_ROOT
    $dialog.ShowNewFolderButton = $true
    $selected = $null
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        $selected = $dialog.SelectedPath
    }
    @{ path = $selected } | ConvertTo-Json -Compress
} finally {
    $dialog.Dispose()
}
