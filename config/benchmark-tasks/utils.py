"""Chat-model output normalization for lm-eval code tasks.

ONE generic adapter, no per-model rules.

WHY IT EXISTS
lm-eval's code tasks assume a COMPLETION model: `gen_prefix` prefills an
assistant turn opening a ```python fence, the model continues the body, and
`until` stop sequences end it. A chat-completions API cannot prefill an
assistant turn — lm-eval appends gen_prefix to the USER message instead
(verified in the request log). Chat models are then free to answer in whatever
shape they like, and three different shapes were measured:

  A. continue the body, no fence          (qwen3.5 family)
  B. restate the whole function in a fence (gemma4)
  C. prose, then a fenced block            (gemma4, some samples)

lm-eval's extractor is `r[:r.find("```")]`, which returns "" for shapes B and C.
MEASURED: gemma4 produced complete, correct implementations that extracted to
nothing — 405, 1046 and 920 characters discarded — and scored 0.000.

Naively switching to "largest fenced block" then broke shape A: qwen3.5:4b fell
from 1.000 to 0.267, because without a fence the fallback kept trailing prose and
test code that lm-eval's `until` had previously cut off.

So the adapter must handle all three shapes AND end the code where the answer
ends. It does that structurally, not by model name.
"""
import ast
import re

_FENCE = re.compile(r"```(?:[a-zA-Z0-9_+-]*)[ \t]*\n(.*?)(?:```|\Z)", re.DOTALL)

def _has_body(tree: ast.AST) -> bool:
    """Does any function here actually DO something?

    A signature plus docstring is valid Python and contains `def`, which is why
    "longest parseable prefix containing def" accepted the prompt alone and
    scored two models at zero while they wrote working code.

    Deliberately does NOT look for `return`: a function may legitimately raise,
    mutate its argument, yield, or terminate through control flow. The test is
    structural — is there at least one executable statement that is not the
    docstring, `pass`, or `...`.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = list(node.body)
        if body and isinstance(body[0], ast.Expr) and \
                isinstance(getattr(body[0], "value", None), ast.Constant) and \
                isinstance(body[0].value.value, str):
            body = body[1:]          # drop the docstring
        for stmt in body:
            if isinstance(stmt, ast.Pass):
                continue
            if isinstance(stmt, ast.Expr) and \
                    isinstance(getattr(stmt, "value", None), ast.Constant) and \
                    stmt.value.value is Ellipsis:
                continue
            return True
    return False


def _trim(code: str):
    """Longest parseable prefix whose function has a nontrivial body, else None.

    Truncating from the end is what removes trailing prose, bullet lists, tests
    and Unicode commentary — they simply do not parse, so they cannot be the
    accepted program. Inline comments and docstrings survive because they do.

    Returns None rather than a docstring-only shell: a candidate with no body is
    INVALID_EXTRACTION, and falling back to the prompt would score a model zero
    for the harness's mistake.
    """
    lines = code.splitlines()
    for end in range(len(lines), 0, -1):
        candidate = "\n".join(lines[:end])
        try:
            tree = ast.parse(candidate)
        except SyntaxError:
            continue
        if _has_body(tree):
            return candidate
    return None


def _extract(response: str, prompt: str) -> str:
    """One generic sequence, no branch on any model.

    Fenced blocks first (a model that restates the solution), then the response
    as a continuation of the prefilled signature. Every candidate must survive
    the nontrivial-body test; the first that does wins.
    """
    response = response or ""
    candidates = []
    for block in sorted(_FENCE.findall(response), key=len, reverse=True):
        if not block.strip():
            continue
        # A block may redefine the function, or only continue its body.
        candidates.append(block)
        candidates.append(prompt + block)
    candidates.append(prompt + response)

    for c in candidates:
        trimmed = _trim(c)
        if trimmed:
            return trimmed
    # INVALID_EXTRACTION: no candidate had an implementation. Returning the
    # prompt would silently score the model zero for a harness failure.
    return ""


def build_predictions_instruct(resps, docs):
    return [[_extract(r, doc["prompt"]) for r in resp]
            for resp, doc in zip(resps, docs)]


def pass_at_k(*args, **kwargs):
    """lm-eval's own metric, unmodified — only extraction differs here.

    Imported lazily: lm_eval.tasks.humaneval.utils runs a code_eval compute at
    import time, which makes the adapter untestable in isolation.
    """
    from lm_eval.tasks.humaneval.utils import pass_at_k as _p
    return _p(*args, **kwargs)


def build_predictions_mbpp(resps, docs):
    """MBPP extraction, same generic rule as HumanEval.

    UPSTREAM: lm-eval's `extract_code_blocks` is broken for chat models and has
    FIVE open issues — #3793, #3769, #3710, #3447, #3387. The mechanism, from
    #3793: `gen_prefix` supplies an opening ```python fence, so the extractor
    prepends ``` and the first token of code (`def`) is parsed as the language
    tag and stripped. #3447 reports it against a local OpenAI-compatible
    endpoint with --apply_chat_template, the same configuration used here.

    MEASURED here: gpt-oss and gemma4 both scored exactly 0.000 under it.

    MBPP differs from HumanEval in one respect only — the model emits a whole
    program rather than continuing a signature — so the prompt is empty and the
    same nontrivial-body validation applies unchanged.
    """
    return [[_extract(r, "") for r in resp] for resp in resps]


def pass_at_1(*args, **kwargs):
    """lm-eval's own MBPP metric, unmodified. Imported lazily."""
    from lm_eval.tasks.mbpp.utils import pass_at_1 as _p
    return _p(*args, **kwargs)


def list_fewshot_samples(*args, **kwargs):
    from lm_eval.tasks.mbpp.utils import list_fewshot_samples as _f
    return _f(*args, **kwargs)
