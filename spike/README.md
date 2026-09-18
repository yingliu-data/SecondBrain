# PW-4 — the two-day experiment

Throwaway. Delete this directory when the week is up.

## Why it exists

`WORK_PLAN.md` objection 1: ~16 ideal days of infrastructure currently precede
the first user-visible capability, and Phase 3's own note calls that capability
"the phase that proves the thesis." This is the thesis, with the infrastructure
removed — so you find out in week one instead of week four.

## Run it

```bash
cp spike/config.example.json spike/config.json      # fill in, it is gitignored
uv run spike/train_push.py --dry-run                # prove the Darwin call works
uv run spike/train_push.py                          # prove the push arrives
```

Then schedule it for your actual commute and leave it alone for a week:

```
45 7 * * 1-5  cd /path/to/SecondBrain && /usr/local/bin/uv run spike/train_push.py
```

## The only question that matters

After one week, read `spike/diary.jsonl` and answer:

> **Did the 07:45 notification change anything I did?**

- **Yes** → Phases 1–3 are justified. Lift `fetch_departures()` into P2-1's
  Darwin MCP server and `render()` into P3-6's template path; both are already
  the right shape.
- **No** → stop. Objection 2 applies: the remaining ~50 days buy a worse Google
  Maps with a quota attached. Take the minimum viable path at the end of
  `WORK_PLAN.md` (~8 days) and keep the assistant you have.

A "no" here is the single most valuable result this repo can produce right now.
It is worth two days to buy that answer.
