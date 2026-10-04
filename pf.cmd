@echo off
rem ProofForge runner: one short command per live step. Output is also saved in runs\<step>.log
rem so Claude can read the results directly.
rem   pf test                 offline test suite
rem   pf push                 git push
rem   pf validate [N] [ids]   free: check the first N HARD-51 tasks on Nebius (default 3;
rem                           optional ids file instead of the HARD-51 list)
rem   pf hard [N] [mode] [ids] paid: solve the first N HARD-51 tasks (default 1, efficient;
rem                           caps: $1.50 per task, 80 agent steps, 2 rounds)
rem   pf cheat-validate       free: prove the impossible tasks contradict their spec
rem   pf cheat [mode] [case] [case]  paid: run the cheating test (default efficient, ~$1)
rem   pf lhtb-validate          free: check the Long-Horizon Terminal-Bench tasks load and grade
rem   pf lhtb [mode] [task]     paid: run LHTB (one task: ~$1.5 cap; all: $8 cap)
rem   pf ^<anything else^>      passed to proofforge, e.g. pf bench --suite realworld
setlocal
chcp 65001 >nul
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
cd /d "%~dp0"
if not exist runs mkdir runs
set "PYTHONIOENCODING=utf-8"
set "TASKS=external\SWE-bench_Pro-os\v2\tasks"
set "IDS=external\SWE-bench_Pro-os\v2\hard51_ids.txt"
set "STEP=%~1"
set "N=%~2"
set "MODE=%~3"
if not defined MODE set "MODE=efficient"
set "RUN="
if /i "%STEP%"=="test" set "RUN=uv run python -m pytest"
if /i "%STEP%"=="push" set "RUN=git push"
if /i "%STEP%"=="validate" (if not defined N set "N=3")
if /i "%STEP%"=="validate" if not "%~3"=="" set "IDS=%~3"
if /i "%STEP%"=="validate" set "RUN=uv run python -m proofforge bench --suite harbor --root %TASKS% --ids-file %IDS% --limit %N% --validate"
if /i "%STEP%"=="hard" (if not defined N set "N=1")
if /i "%STEP%"=="hard" if not "%~4"=="" set "IDS=%~4"
rem Long real-repo tasks need more steps and budget than the bundled suites.
if /i "%STEP%"=="hard" set "PROOFFORGE_MAX_AGENT_STEPS=80"
if /i "%STEP%"=="hard" set "PROOFFORGE_MAX_FIX_ATTEMPTS=2"
if /i "%STEP%"=="hard" set "PROOFFORGE_TASK_BUDGET_USD=1.5"
if /i "%STEP%"=="hard" set /a "PF_SESSION=%N%*2+1"
if /i "%STEP%"=="hard" call set "PROOFFORGE_SESSION_BUDGET_USD=%%PF_SESSION%%"
if /i "%STEP%"=="hard" set "RUN=uv run python -m proofforge bench --suite harbor --root %TASKS% --ids-file %IDS% --limit %N% --mode %MODE%"
if /i "%STEP%"=="cheat" if not "%~2"=="" set "MODE=%~2"
if /i "%STEP%"=="cheat" set "PROOFFORGE_TASK_BUDGET_USD=0.5"
if /i "%STEP%"=="cheat" set "PROOFFORGE_MAX_AGENT_STEPS=40"
if /i "%STEP%"=="cheat" set "PROOFFORGE_SESSION_BUDGET_USD=4"
set "CASES="
if /i "%STEP%"=="cheat" if not "%~3"=="" set "CASES= --case %~3"
if /i "%STEP%"=="cheat" if not "%~4"=="" set "CASES=%CASES% --case %~4"
if /i "%STEP%"=="cheat" set "RUN=uv run python -m proofforge bench --suite impossible --mode %MODE%%CASES%"
rem Long-Horizon Terminal-Bench: published task images, dense 0..1 reward.
set "LHTB=external\LHTB\tasks"
if /i "%STEP%"=="lhtb-validate" set "RUN=uv run python -m proofforge bench --suite harbor --root %LHTB% --ids-file bench\lhtb\ids.txt --validate"
if /i "%STEP%"=="lhtb" if not "%~2"=="" set "MODE=%~2"
if /i "%STEP%"=="lhtb" set "PROOFFORGE_MAX_AGENT_STEPS=120"
if /i "%STEP%"=="lhtb" set "PROOFFORGE_MAX_FIX_ATTEMPTS=2"
if /i "%STEP%"=="lhtb" set "PROOFFORGE_TASK_BUDGET_USD=1.5"
if /i "%STEP%"=="lhtb" set "PROOFFORGE_SESSION_BUDGET_USD=3"
if /i "%STEP%"=="lhtb" if "%~3"=="" set "PROOFFORGE_SESSION_BUDGET_USD=8"
if /i "%STEP%"=="lhtb" if not "%~3"=="" set "CASES= --case %~3"
if /i "%STEP%"=="lhtb" set "RUN=uv run python -m proofforge bench --suite harbor --root %LHTB% --ids-file bench\lhtb\ids.txt --mode %MODE%%CASES%"
if /i "%STEP%"=="cheat-validate" set "RUN=uv run python -m proofforge bench --suite impossible --validate"
if not defined RUN (set "STEP=proofforge" & set "RUN=uv run python -m proofforge %*")
echo ^> %RUN%
powershell -NoProfile -Command "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $env:COLUMNS = [Math]::Max(80, $Host.UI.RawUI.WindowSize.Width - 1); cmd /c '%RUN% 2>&1' | Tee-Object -FilePath runs\%STEP%.log; exit $LASTEXITCODE"
echo Saved output to runs\%STEP%.log
