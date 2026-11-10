$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
$py = 'C:\py312\python.exe'
$idx = 'https://pypi.tuna.tsinghua.edu.cn/simple'
& $py -m pip install --upgrade pip -q -i $idx 2>&1 | Select-Object -Last 2
& $py -m pip install pyarrow huggingface_hub -q -i $idx 2>&1 | Select-Object -Last 5
& $py -c "import pyarrow, huggingface_hub as h; print('pyarrow', pyarrow.__version__, 'hub', h.__version__)"
"DISK_FREE_C_GB=" + [Math]::Round((Get-PSDrive C).Free / 1GB, 1)
