@echo off
REM Capture reference-Pi ground truth AS ROOT. Run from repo root:
REM     scalpel\scripts\capture_root.cmd
REM Prompts for pi's SSH password (twice), then sudo password once.
REM Takes 5-15 min. Output: scalpel\tests\ground_truth_root.jsonl.
REM
REM Two SSH sessions: the capture pipes the script over stdin, which
REM prevents a TTY, which breaks sudo's password prompt. So we prime
REM sudo's credential cache over an interactive session first, then run
REM the capture non-interactively (sudo -n) within the 15-min cache window.
ssh -t pi@10.4.27.33 "sudo -v" || exit /b 1
ssh pi@10.4.27.33 "sudo -n -i python3 -" < scalpel\scripts\capture_truth.py > scalpel\tests\ground_truth_root.jsonl
