"""diag-runner-context.py — characterize Ollama runner-context behaviour.

RESULT (gemma4:26b-mlx, Ollama 0.32.5, 19 observations): FIRST_LOAD_WINS.
The resident runner keeps whatever num_ctx it was FIRST loaded with, and later
requests naming a different num_ctx do not reload it -- verified in both
directions and across three alternating cycles. Only an explicit unload changes
it. Never more than one runner for the model.

This CONTRADICTS qwen3.5:2b, which reloaded on every num_ctx change (both up and
down). So runner policy is not uniform across models and must not be assumed
from one measurement -- which is why this exists as a repeatable diagnostic
rather than a note.

OPEN: whether the first-loaded context actually CONSTRAINS later requests. A
40,000-token prompt sent to a 98,304-declared alias, while the runner reported
24,576, returned prompt_eval_count 40,013 -- more than the reported context.
Resolve before deciding whether three roles may share one model at different
geometries.

DIAGNOSTIC ONLY — temporary aliases, no profile changes. The question that gates
same-model role unification: when three roles share one model at three different
num_ctx values, does Ollama keep one runner, reload, or run several?
"""
import sys, json, time, urllib.request
sys.path.insert(0,'/Users/vtchevalier/Documents/Projects/DevelopSolutions/ailocal/scripts/lib')
import benchmark as B

MODEL="gemma4:26b-mlx"
GEOM={24576:"a",65536:"b",98304:"c"}   # num_ctx -> alias suffix
def ps():
    with urllib.request.urlopen("http://127.0.0.1:11434/api/ps",timeout=15) as r:
        return [m for m in json.load(r).get("models",[]) if MODEL in m["name"]]
def snap():
    ms=ps()
    return [(m.get("context_length"), round(m["size"]/1e9,1), m.get("expires_at","")[:19]) for m in ms]
def unload():
    body=json.dumps({"model":MODEL,"keep_alive":0}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/generate",
            data=body,headers={"content-type":"application/json"}),timeout=120).read()
    except Exception: pass
    for _ in range(40):
        if not ps(): return
        time.sleep(1)

entries=[]
for ctx,suf in GEOM.items():
    e=B.build_alias(MODEL,"off",ctx-2048,2048,{}); e["model_name"]=f"bench-gm-{suf}"
    e["litellm_params"]["num_ctx"]=ctx            # exact target num_ctx
    e["litellm_params"]["keep_alive"]="5m"
    entries.append(e)
print("aliases:",[e["model_name"] for e in entries],flush=True)
applied=B.apply_aliases(entries); print("installed:",applied["ok"],flush=True)
key=B.api_key()
rows=[]
def ask(ctx,label):
    alias=f"bench-gm-{GEOM[ctx]}"
    before=snap()
    body=json.dumps({"model":alias,"messages":[{"role":"user","content":"hi"}],"max_tokens":4}).encode()
    t0=time.time()
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:4000/v1/chat/completions",
        data=body,headers={"content-type":"application/json","Authorization":f"Bearer {key}"}),timeout=900).read()
    dt=round(time.time()-t0,1); time.sleep(2); after=snap()
    prev_ctx = before[0][0] if before else None
    reload = (not before) or (after and after[0][0]!=prev_ctx) or dt>25
    rows.append((label,ctx,prev_ctx,after[0][0] if after else None,len(after),dt,reload))
    print(f"  {label:26} req={ctx:<7} before={prev_ctx} after={after[0][0] if after else None} "
          f"runners={len(after)} {dt}s reload={reload}",flush=True)
try:
    unload()
    print("A 24576 -> 98304",flush=True);  ask(24576,"A load 24576");  ask(98304,"A then 98304")
    unload()
    print("B 98304 -> 24576",flush=True);  ask(98304,"B load 98304");  ask(24576,"B then 24576")
    unload()
    print("C 65536 -> 98304",flush=True);  ask(65536,"C load 65536");  ask(98304,"C then 98304")
    unload()
    print("D 98304 -> 65536",flush=True);  ask(98304,"D load 98304");  ask(65536,"D then 65536")
    print("E unload between",flush=True)
    unload(); ask(65536,"E 65536 after unload"); unload(); ask(24576,"E 24576 after unload")
    print("G concurrent two aliases",flush=True)
    unload(); ask(24576,"G first 24576"); ask(98304,"G second 98304"); ask(24576,"G back to 24576")
    print("H alternating x3",flush=True)
    for i in range(3):
        ask(24576,f"H cycle{i+1} 24576"); ask(98304,f"H cycle{i+1} 98304")
finally:
    unload(); r=B.restore(); print("\nrestored:",r["restored"],"leaked:",r["leaked"],flush=True)
print()
print("%-26s %-8s %-9s %-9s %-9s %-7s %s"%("step","req","before","after","runners","secs","reload"))
for x in rows: print("%-26s %-8s %-9s %-9s %-9s %-7s %s"%x)
print("GEMMA_DONE")
