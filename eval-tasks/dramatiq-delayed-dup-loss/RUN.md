# RUN.md — the 3 commands you type

The A/B folders are already BUILT and verified (`~/dramatiq-eval-task-A`, `-B`).
Setup is done — `make-eval-dirs.sh` handled it (own source, own venv, own Redis
db A=14/B=15, bug confirmed live, no answer files leaked). You only need these:

--------------------------------------------------------------------------------
## 1. Hide the answer, then run each model
--------------------------------------------------------------------------------
```bash
mv ~/backend-stress-eval /tmp/HARNESS-HIDDEN     # grader/rubric out of reach
```
Then run each model in its own folder, separate session, and note the time each
takes:
```bash
cd ~/dramatiq-eval-task-A     # start model A here
cd ~/dramatiq-eval-task-B     # start model B here (separate session)
```

--------------------------------------------------------------------------------
## 2. Grade each result
--------------------------------------------------------------------------------
```bash
mv /tmp/HARNESS-HIDDEN ~/backend-stress-eval     # put the grader back

~/dramatiq-eval-task-A/.venv/bin/python \
  ~/backend-stress-eval/investigations/dramatiq-431-delayed-dup/grade.py \
  --pkg ~/dramatiq-eval-task-A/src --db 14

~/dramatiq-eval-task-B/.venv/bin/python \
  ~/backend-stress-eval/investigations/dramatiq-431-delayed-dup/grade.py \
  --pkg ~/dramatiq-eval-task-B/src --db 15
```
PASS = both `DUP_gate` and `LOSS_gate` say PASS (exit code 0). Then hand-write
results.md yourself (pass/fail + time-to-fix + your comparison).

--------------------------------------------------------------------------------
## 3. Restore (clean the machine)
--------------------------------------------------------------------------------
```bash
[ -d /tmp/HARNESS-HIDDEN ] && mv /tmp/HARNESS-HIDDEN ~/backend-stress-eval || true
rm -rf ~/dramatiq-eval-task-A ~/dramatiq-eval-task-B
for n in 14 15; do redis-cli -h 127.0.0.1 -n "$n" flushdb; done
```
Only this task's folders and its own Redis dbs (14/15) are removed. Nothing else
is touched.

--------------------------------------------------------------------------------
## Rebuild later (if you removed the folders)
--------------------------------------------------------------------------------
```bash
cd ~/backend-stress-eval/eval-tasks/dramatiq-delayed-dup-loss
rm -rf ~/dramatiq-eval-task-A ~/dramatiq-eval-task-B
./make-eval-dirs.sh A B
```
