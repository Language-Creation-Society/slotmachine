import time
import pulp
import logging
from dateutil import parser
import sys
from importlib import reload
from slotmachine import SlotMachine
from slotmachine import Unsatisfiable
import json

reload(sys.modules["slotmachine"])
from slotmachine import SlotMachine
sm = SlotMachine()

reloadsm()

sch = json.load(open("schedule.json"))
res = sm.schedule(sch)

for t in sorted(res,key=lambda x:[x["time"],x["venue"]]):
    [t["plenary"], t["time"], t["duration"], t["venue"], t["id"], t["speakers"], t["title"]]

for t in sorted(res,key=lambda x:[x["venue"],x["time"]]):
    [t["plenary"], t["time"], t["duration"], t["venue"], t["id"], t["speakers"], t["title"]]


# SlotMachine.calculate_slots(parser.parse("2025-04-11 07:00"), parser.parse("2025-04-13 07:00"), parser.\
# parse("2025-04-13 19:00"))
# Talk = SlotMachine.Talk
#
# sm.schedule_from_file( infile="schedule.json", outfile="schedule2.json");
#
# venues = {
#     2114: "secondary",
#     2124: "garage",
#     2123: "tertiary",
#     1125: "atrium",
#     1205: "Jimenez",
#     1: "German library",
#     3: "secret"
# }
#
# capacities = {
#     2124: 80,
#     2123: 30,
#     1205: 65
# }