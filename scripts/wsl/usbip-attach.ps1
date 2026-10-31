# Attach a USB device (Bluetooth dongle, ESP32) to WSL with usbipd-win. Run in an
# elevated PowerShell on Windows. Used from Phase 7; nothing in Phases 1-5 needs it.
#   .\usbip-attach.ps1            -> lists devices
#   .\usbip-attach.ps1 -BusId 2-3 -> binds (once) and attaches to WSL
param([string]$BusId)

if (-not $BusId) {
    usbipd list
    Write-Host "`nRe-run with -BusId <id> to attach a device."
    exit 0
}
usbipd bind --busid $BusId
usbipd attach --wsl --busid $BusId
