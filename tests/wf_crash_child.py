"""Child process for the crash test. `start`: run until the approval wait, then die without cleanup.
`resume`: launch DBOS (which recovers the run), deliver the approval, print a JSON report."""
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

from dbos import DBOS, SetWorkflowID
from sqlalchemy import func, select

import orchestra.workflows.market_close as wf
from orchestra.db import DataSnapshot, LLMCall, PackageRow, PublishJob, Run
from orchestra.runtime import set_runtime
from wf_support import GOOD, make_runtime, wait_for_status

phase, workdir, url = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
RUN = wf.run_id_for("pouyan", date(2026, 11, 11))
# the resumed process has no scripted reply left: if it called the model again, the test would fail loudly
rtm, provider, _ = make_runtime(workdir, [GOOD] if phase.startswith("start") else [])
set_runtime(rtm)
DBOS(config={"name": "orchestra-test", "system_database_url": url, "log_level": "WARNING"})
DBOS.launch()

if phase == "start":
    with SetWorkflowID(RUN):
        DBOS.start_workflow(wf.market_close, "pouyan", "2026-11-11", [["tg_test", "telegram"], ["bale_test", "bale"]])
    wait_for_status(rtm.sessions, RUN, {"awaiting_approval"})
    time.sleep(1.0)  # let DBOS record that the workflow is inside recv
    print("WAITING", flush=True)
    os._exit(0)  # no shutdown, no cleanup: as if the machine lost power

if phase == "start_cli_exit":
    with SetWorkflowID(RUN):
        DBOS.start_workflow(wf.market_close, "pouyan", "2026-11-11", [["tg_test", "telegram"], ["bale_test", "bale"]])
    wait_for_status(rtm.sessions, RUN, {"awaiting_approval"})
    time.sleep(1.0)
    print("WAITING", flush=True)
    from orchestra.cli import leave_runs_waiting
    leave_runs_waiting()  # exactly how `orchestra.cli run` exits while the run waits for approval

with rtm.sessions() as s:
    run = s.get(Run, RUN)
    pkg = s.get(PackageRow, run.package_id)
wf.submit_decision(RUN, pkg.package_hash, "approved", "user:1")
result = DBOS.retrieve_workflow(RUN).get_result()
with rtm.sessions() as s:
    report = {
        "status": result["status"],
        "llm_calls": s.execute(select(func.count(LLMCall.id))).scalar_one(),
        # the most times any one source was fetched (each source once: nothing redone after the restart)
        "snapshots": max(n for _, n in s.execute(select(DataSnapshot.source_id, func.count(DataSnapshot.id))
                                                 .group_by(DataSnapshot.source_id)).all()),
        "jobs": s.execute(select(func.count(PublishJob.id))).scalar_one(),
    }
DBOS.destroy()
print(json.dumps(report))
