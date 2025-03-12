import time
import pulp
import logging
from dateutil import parser
import sys
from importlib import reload
from slotmachine import SlotMachine
from slotmachine import Unsatisfiable
import json


# This part can be rerun without having to kill the REPL
reload(sys.modules["slotmachine"])
from slotmachine import SlotMachine
sch = json.load(open("schedule.json"))
sm = SlotMachine()
prep = sm.prep_schedule(sch)
res = sm.schedule(sch)
#problem = sm.get_problem(venues=prep["venues"], talks=prep["talks"], old_talks=prep["old_slots"], people=prep["people"], languages=prep["languages"])
#solution = problem.solve(pulp.COIN_CMD(threads=12, msg=1, keepFiles=0))

for t in sorted(res,key=lambda x:[x["time"],x["venue"]]):
    [t["plenary"], t["time"], t["end_time"], t["duration"], sm.venues_by_id[t["venue"]].name, t["id"], t["speakers"], t["title"], [sm.people_by_id[pid].name for pid in t["attendees"]], [sm.people_by_id[pid].name for pid in t["partial_attendees"]]]

# for t in sorted(res,key=lambda x:[x["venue"],x["time"]]):
#     [t["plenary"], t["time"], t["end_time"], t["duration"], sm.venues_by_id[t["venue"]].name, t["id"], t["speakers"], t["title"], [sm.people_by_id[pid].name for pid in t["attendees"]], [sm.people_by_id[pid].name for pid in t["partial_attendees"]]]


# SlotMachine.calculate_slots(parser.parse("2025-04-11 07:00"), parser.parse("2025-04-13 07:00"), parser.\
# parse("2025-04-13 19:00"))
# Talk = SlotMachine.Talk
#
# sm.schedule_from_file( infile="schedule.json", outfile="schedule2.json");