# Trivial fixture — write "fixed" to a file

Create a file named `answer.txt` in the current directory containing exactly the string `fixed` (no newline, no quotes, no other content).

This fixture exists to smoke-test `bse difficulty-check` against a real headless `claude` session. It is deliberately trivial — a real model should complete it in seconds. The gate should REJECT (median well under 60 min), and that's the point: we're testing the *driver*, not measuring difficulty.
