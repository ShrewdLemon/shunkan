import json, pathlib, warnings, time; warnings.filterwarnings("ignore")
from shunkan.data.bse import ingest_rpt, scrip_code
from shunkan.data.constituents import fetch_constituents
SP=pathlib.Path("/private/tmp/claude-501/-Users-shrewdlemon-Projects-Shunkan/deaee33b-ad0b-46f9-a341-84d888097caa/scratchpad")
LOG=SP/"rpt_ingest.log"
def say(m):
    with open(LOG,"a") as f: f.write(f"{time.strftime('%H:%M:%S')} {m}\n")
syms=[c.symbol for c in fetch_constituents("NIFTY500")]
ok=fail=edges=0
for i,s in enumerate(syms,1):
    try:
        code=scrip_code(s)
        if not (pathlib.Path.home()/".shunkan"/"store"/"bse"/f"rpt_{code}.json").exists(): continue
        r=ingest_rpt(code, symbol=s); ok+=1; edges+=r["edges"]
        if i%25==0: say(f"[{i}/{len(syms)}] {s}: {edges:,} edges so far")
    except Exception as e:
        fail+=1; say(f"{s}: FAIL {type(e).__name__} {str(e)[:60]}")
say(f"DONE ok={ok} fail={fail} edges={edges:,}")
