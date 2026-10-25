$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root 'neko-lab\upstream'
if (Test-Path -LiteralPath $target) { throw 'neko-lab/upstream already exists.' }
git clone https://github.com/Project-N-E-K-O/N.E.K.O.git $target
git -C $target checkout 435a033983152e13520b1f6cb9912ea0e8c9dc75
$actual = (git -C $target rev-parse HEAD).Trim()
if ($actual -ne '435a033983152e13520b1f6cb9912ea0e8c9dc75') { throw 'Pinned commit verification failed.' }
