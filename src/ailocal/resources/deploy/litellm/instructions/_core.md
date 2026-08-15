# Local runtime constraints

You are served by a local model through the ailocal proxy. Only the facts below
are true *because* the model is local; everything else about how to engineer
software you already know, and this file deliberately does not repeat it.

- Context is smaller and slower to fill than a hosted model's. Search narrowly,
  stop once you have enough evidence, and keep answers short. A long recap costs
  more here than the answer itself.
- Prefer summarising what you found over quoting it back in full.
- If a `/tmp/scratchpad/` directory was provided for this session, keep durable
  notes there rather than re-deriving them in context.
