# One-time RVC inference environment setup
# Usage: powershell -ExecutionPolicy Bypass -File scripts/setup_voice_env.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VenvPath = Join-Path $ProjectRoot ".venv-rvc"
$FairseqWheel = Join-Path $ProjectRoot "fairseq-0.12.4-cp311-cp311-win_amd64.whl"
$FairseqUrl = "https://huggingface.co/Jmica/rvc/resolve/main/fairseq-0.12.4-cp311-cp311-win_amd64.whl?download=true"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Miku voice model setup (run once)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$Py311 = $null
foreach ($cmd in @("py -3.11", "python3.11", "python")) {
    try {
        $ver = Invoke-Expression "$cmd -c `"import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')`"" 2>$null
        if ($ver -eq "3.11") {
            $Py311 = $cmd
            break
        }
    } catch {}
}

if (-not $Py311) {
    Write-Host "Python 3.11 not found." -ForegroundColor Red
    Write-Host "Install: winget install Python.Python.3.11" -ForegroundColor Yellow
    exit 1
}

Write-Host "[1/4] Creating .venv-rvc ..." -ForegroundColor Green
Invoke-Expression "$Py311 -m venv `"$VenvPath`""

$Pip = Join-Path $VenvPath "Scripts\pip.exe"
$Python = Join-Path $VenvPath "Scripts\python.exe"

Write-Host "[2/4] Installing rvc-python ..." -ForegroundColor Green
& $Pip install --upgrade pip
& $Pip install "git+https://github.com/JarodMica/rvc-python"

Write-Host "[3/4] Installing fairseq wheel ..." -ForegroundColor Green
if (-not (Test-Path $FairseqWheel)) {
    Invoke-WebRequest -Uri $FairseqUrl -OutFile $FairseqWheel
}
& $Pip install $FairseqWheel

Write-Host "[4/4] Installing PyTorch ..." -ForegroundColor Green
$UseCuda = $false
try {
    $null = Get-Command nvidia-smi -ErrorAction Stop
    $smi = & nvidia-smi 2>$null
    if ($LASTEXITCODE -eq 0 -and $smi -match "NVIDIA") {
        $UseCuda = $true
    }
} catch {}

if ($UseCuda) {
    Write-Host "  NVIDIA GPU detected -> installing CUDA build (cu124)" -ForegroundColor Cyan
    & $Pip uninstall -y torch torchaudio 2>$null
    & $Pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
} else {
    Write-Host "  No NVIDIA GPU -> installing CPU build" -ForegroundColor Yellow
    & $Pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
}

Write-Host ""
Write-Host "Verifying ..." -ForegroundColor Green
& $Python -c "import torch; print('torch', torch.__version__); print('cuda', torch.cuda.is_available())"
& $Python -c "from rvc_python.infer import RVCInference; print('rvc_python OK')"
Write-Host ""
Write-Host "Done. Next:" -ForegroundColor Cyan
Write-Host "  py diagnose_rvc.py" -ForegroundColor White
Write-Host "  py test_rvc_conversion.py" -ForegroundColor White
Write-Host "  py main.py" -ForegroundColor White
