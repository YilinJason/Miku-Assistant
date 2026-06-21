# Upgrade .venv-rvc PyTorch from CPU to CUDA (for NVIDIA GPUs)
# Usage: powershell -ExecutionPolicy Bypass -File scripts/upgrade_rvc_cuda.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $ProjectRoot ".venv-rvc\Scripts\python.exe"
$Pip = Join-Path $ProjectRoot ".venv-rvc\Scripts\pip.exe"

if (-not (Test-Path $Python)) {
    Write-Host "Missing .venv-rvc. Run scripts/setup_voice_env.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "Checking GPU ..." -ForegroundColor Cyan
try {
    & nvidia-smi | Out-Null
} catch {
    Write-Host "nvidia-smi not found. Cannot install CUDA PyTorch." -ForegroundColor Red
    exit 1
}

Write-Host "Installing PyTorch (CUDA cu124) ..." -ForegroundColor Green
& $Pip uninstall -y torch torchaudio 2>$null
& $Pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124

Write-Host ""
Write-Host "Verify:" -ForegroundColor Cyan
& $Python -c "import torch; print('torch', torch.__version__); print('cuda_available', torch.cuda.is_available()); print('device', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"

Write-Host ""
Write-Host "Done. Restart Miku assistant (py main.py)." -ForegroundColor Green
