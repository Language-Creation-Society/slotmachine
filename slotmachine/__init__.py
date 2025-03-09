from __future__ import annotations
from collections import namedtuple
from datetime import datetime
from dateutil import parser, relativedelta
from typing import Iterable
import json
import time
import logging
import pulp

class Unsatisfiable(Exception):
    pass


class SlotMachine(object):
    Session = namedtuple(
        "Session",
        ("id","duration","venues","preferred_venues","speakers","slots","preferred_slots","plenary","talks","preferred_talks" ),
    )
    Session.__new__.__defaults__ = ([], [])

    Venue = namedtuple(
        "venue",
        ("id", "name", "capacity", "slots" ),
    )

    Person = namedtuple(
        "person",
        ("id", "name", "preferred_slots", "slots", "irl", "preferences" ),
    )

    Talk = namedtuple(
        "Talk",
        ("id", "duration", "venues", "speakers", "preferred_venues", "preferred_slots", "slots", "plenary", "irl_only", "prereqs", "rest" ),
    )
    # If preferred venues and/or slots are not specified, assume there are no preferences
    Talk.__new__.__defaults__ = ([], [])

    def __init__(self):
        self.log = logging.getLogger(__name__)
        self.talks_by_id = {}
        self.people_by_id = {}
        self.people_by_name = {}
        self.venues_by_id = {}
        self.talks_by_speaker = {}
        self.talk_permissions = {}
        self.slots_available = set()
        self.var_cache: dict[str, pulp.LpVariable] = {}
        self.suspected_constr = []
        self.suspected_var = []

    def start_var(self, slot, talk_id, venue) -> pulp.LpVariable:
        """A 0/1 variable that is 1 if talk with ID talk_id begins in this
        slot and venue"""
        name = "START_%d_%d_%d" % (slot, talk_id, venue)
        if name in self.var_cache:
            return self.var_cache[name]

        # Check if this talk doesn't span a period of no talks
        contiguous = True
        for slot_offset in range(0, self.talks_by_id[talk_id].duration):
            if slot + slot_offset not in self.slots_available:
                contiguous = False
                break

        # There isn't enough time left for the talk if it starts in this slot.
        if not contiguous:
            var = pulp.LpVariable(name, lowBound=0, upBound=0, cat="Binary") # cat="Integer")
        else:
            var = pulp.LpVariable(name, cat="Binary")

        self.var_cache[name] = var
        return var

    def active(self, slot, talk_id, venue) -> pulp.LpVariable:
        """A 0/1 variable that is 1 if talk with ID talk_id is active during
        this slot and venue"""
        name = "ACTIVE_%d_%d_%d" % (slot, talk_id, venue)
        if name in self.var_cache:
            return self.var_cache[name]

        if (
            slot in self.talk_permissions[talk_id]["slots"]
            and venue in self.talk_permissions[talk_id]["venues"]
        ):
            variable = pulp.LpVariable(name, cat="Binary")
        else:
            variable = pulp.LpVariable(name, lowBound=0, upBound=0, cat="Binary") # cat="Integer")

        duration = self.talks_by_id[talk_id].duration
        definition = pulp.lpSum(
            self.start_var(s, talk_id, venue)
            for s in range(slot, max(-1, slot - duration), -1)
        )

        self.problem.addConstraint(variable == definition)
        self.var_cache[name] = variable
        return variable

    def attending(self, slot, talk_id, person_id) -> pulp.LpVariable:
        """A 0/1 variable that is 1 if talk with ID talk_id is active during
        this slot and this person is attending it"""
        name = "ATTEND_%d_%d_%d" % (slot, talk_id, person_id)
        if name in self.var_cache:
            return self.var_cache[name]

        if (
            slot in self.people_by_id[person_id].slots
        ):
            variable = pulp.LpVariable(name, cat="Binary")
        else:
            variable = pulp.LpVariable(name, lowBound=0, upBound=0, cat="Binary") # cat="Integer")

        self.var_cache[name] = variable
        return variable

    def happiness(session):
        return 1

    def get_problem(self, venues: Iterable[Venue], talks: Iterable[Talk], old_talks, people: Iterable[Person]) -> pulp.LpProblem:
        # Reset problem and cached variables
        self.problem = pulp.LpProblem("Scheduler", pulp.LpMaximize)
        self.var_cache = {}

        self.talks_by_id = {talk.id: talk for talk in talks}
        talk_ids = {t.id for t in talks}
        venue_ids = {v.id for v in venues}
        people_ids = {p.id for p in people}

#        max_session_size = 4
#        max_sessions = 20
#        possible_sessions = [tuple(c) for c in pulp.allcombinations(talks, max_session_size)]
#        # create a binary variable to state that a table setting is used
#        x = pulp.LpVariable.dicts(
#            "session", possible_sessions, lowBound=0, upBound=1, cat=pulp.LpInteger # cat=pulp.LpBinary
#        )
#        self.problem += pulp.lpSum([happiness(session) * x[session] for session in possible_sessions])
#        self.problem += (
#            pulp.lpSum([x[session] for session in possible_sessions]) <= max_sessions,
#            "Maximum_number_of_sessions",
#        )
#        # A talk must be in one and only one session
#        for talk in talks:
#            self.problem += (
#                pulp.lpSum([x[session] for session in possible_sessions if talk in session]) == 1,
#                f"Must_include_talk_ID_{talk.id}",
#            )

        # Every talk begins exactly once
        for talk in talks:
            self.problem.addConstraint(
                pulp.lpSum(
                    self.start_var(slot, talk.id, vid)
                    for vid in venue_ids
                    for slot in self.slots_available
                )
                == 1,
                name = "ONE_START_%d" % (talk.id)
            )

        # require talks in times & places they're allowed
        for talk in talks:
            self.problem.addConstraint(
                pulp.lpSum(
                    self.start_var(s, talk.id, vid)
                    for vid in talk.venues
                    for s in talk.slots
                )
                == 1,
                name = "ALLOWED_TIME_PLACE_%d" % (talk.id)
            )

        # At most one talk may be active in a given venue and slot.
        for vid in venue_ids:
            for slot in self.slots_available:
                self.problem.addConstraint(
                    pulp.lpSum(
                        self.active(slot, talk.id, vid)
                        for talk in talks
                    )
                    <= 1,
                    name = "ONE_ACTIVE_%d_%d" % (vid, slot)
                )

        # people can attend one thing at a time
        for person in people:
            for slot in self.slots_available:
                self.problem.addConstraint(
                    pulp.lpSum(
                        self.attending(slot, tid, person.id)
                        for tid in talk_ids
                    )
                    <= 1,
                    name = "UNIPRESENCE_%d_%d" % (person.id, slot)
                )

        # # room capacity
        # for vid in venue_ids:
        #     for slot in self.slots_available:
        #         for talk in talks:
        #             self.problem.addConstraint(
        #                 pulp.lpSum(
        #                     self.attending(slot, talk.id, pid)
        #                     for pid in people_ids
        #                     if (people_by_id[pid].attending == 1)
        #                     if (self.active(slot,talk.id, vid) == 1)
        #                 )
        #                 <= self.venues_by_id[vid].capacity
        #             )

        # require speakers to attend
        for talk in talks:
            for speaker_id in talk.speakers:
                self.problem.addConstraint(
                    pulp.lpSum(
                        self.attending(s, talk.id, speaker_id)
                        for s in talk.slots
                    )
                    == 1,
                    name = "SPEAKER_ATTENDS/_%d_%d" % (talk.id, speaker_id)
                )

        # disallow person's unavailable slots
        for person in people:
            self.problem.addConstraint(
                pulp.lpSum(
                    self.attending(s, talk.id, person.id)
                    for s in (self.slots_available - set(person.slots))
                )
                == 0,
                name = "PERSON_AVAILABILITY_%d" % (person.id)
            )

        # disallow talk's unavailable slots
        for talk in talks:
            self.problem.addConstraint(
                pulp.lpSum(
                    self.active(s, talk.id, vid)
                    for vid in talk.venues
                    for s in (self.slots_available - set(talk.slots))
                )
                == 0,
                name = "TALK_NOT_IN_BAD_SLOTS_%d" % (talk.id)
            )

        # disallow venue's unavailable slots
        for venue in venues:
            self.problem.addConstraint(
                pulp.lpSum(
                    self.active(s, tid, venue.id)
                    for tid in talk_ids
                    for s in (self.slots_available - set(venue.slots))
                )
                == 0,
                name = "VENUE_NOT_IN_BAD_SLOTS_%d" % (venue.id)
            )

        # disallow invalid venues
        for talk in talks:
            self.problem.addConstraint(
                pulp.lpSum(
                    self.active(s, talk.id, vid)
                    for vid in (venue_ids - set(talk.venues))
                    for s in self.slots_available
                )
                == 0,
                name = "TALK_NOT_IN_INVALID_VENUE_%d" % (talk.id)
            )

        # Require a talk (talk2) to come after its prerequisites (talk1):
        # talk2 shouldn't start at or before s if the last segment of talk1
        # is at or after s.
        # for talk2 in talks:
        #     for t1id in talk2.prereqs:
        #         talk1 = self.talks_by_id[t1id]
        #         for s in self.slots_available:
        #             self.problem.addConstraint(
        #                 pulp.lpSum([
        #                     self.start_var(earlier, talk2.id, vid)
        #                     for vid in venue_ids
        #                     for earlier in self.slots_available
        #                     if earlier <= s
        #                 ] + [
        #                     self.start_var(later, talk1.id, vid)
        #                     for vid in venue_ids
        #                     for later in self.slots_available
        #                     if later + talk1.duration - 1 >= s
        #                 ])
        #                 <= 1
        #             )
        # Require a talk (talk2) to come after its prerequisites (talk1):
        # start time of talk2 - start time of talk1 >= duration of talk1.
        # for talk2 in talks:
        #     for t1id in talk2.prereqs:
        #         talk1 = self.talks_by_id[t1id]
        #         self.problem.addConstraint(
        #             pulp.LpAffineExpression([
        #                 (self.start_var(vid, talk2.id, s), s)
        #                 for s in self.slots_available
        #                 for vid in venue_ids
        #             ] + [
        #                 (self.start_var(vid, talk1.id, s), -s)
        #                 for s in self.slots_available
        #                 for vid in venue_ids
        #             ])
        #             >= talk1.duration
        #         )
        #
        # plenary talks can't have anything else parallel
        for slot in self.slots_available:
            self.problem.addConstraint(
                pulp.lpSum(
                    (self.active(slot, t.id, vid) * t.plenary * 100) + self.active(slot, t.id, vid)
                    for t in talks
                    for vid in venue_ids
                )
                <= 101, # FIXME
                name = "PLENARY_EXCLUSIVITY_%d" % (slot)
            )

        # For each talk by the same speaker it can only be active in at most one
        # talk slot at the same time.
        for speaker_id in self.talks_by_speaker:
            conflicts = self.talks_by_speaker[speaker_id]
            if len(conflicts) > 1:
                for slot in self.slots_available:
                    self.problem.addConstraint(
                        pulp.lpSum(
                            self.active(slot, talk_id, vid)
                            for talk_id in conflicts
                            for vid in venue_ids
                        )
                        <= 1,
                        name = "NO_SPEAKER_CONFLICTS_%d_%d" % (speaker_id, slot)
                    )

        self.problem += (
            5
            * pulp.lpSum(
                # Maximise the number of things in their preferred venues (for putting big talks on big stages)
                self.active(s, t.id, vid)
                for t in talks
                for vid in talk.preferred_venues
                for s in self.slots_available
            )
            + 10
            * pulp.lpSum(
                # Try and keep everything inside its preferred time period (for packing things earlier in the day)
                self.active(s, t.id, vid)
                for t in talks
                for s in talk.preferred_slots
                for vid in venue_ids
            )
            + 10
            * pulp.lpSum(
                # We'd like talks with a slot & venue to try and stay there if they can
                self.active(s, talk_id, venue_id)
                for (slot, talk_id, venue_id) in old_talks
                for s in range(slot, slot + self.talks_by_id[talk_id].duration)
            )
            + 5
            * pulp.lpSum(
                # And we'd prefer to just move stage rather than slot
                self.active(s, talk_id, vid)
                for (slot, talk_id, _) in old_talks
                for s in range(slot, slot + self.talks_by_id[talk_id].duration)
                for vid in self.talk_permissions[talk_id]["venues"]
            )
            + 1
            * pulp.lpSum(
                # But if they have to move slot, 60mins either way is ok
                self.active(s, talk_id, vid)
                for (slot, talk_id, _) in old_talks
                for s in range(slot - 6, slot + self.talks_by_id[talk_id].duration + 6)
                for vid in self.talk_permissions[talk_id]["venues"]
            )
        )

        return self.problem

    def schedule_talks(self, talks: Iterable[Talk], people: Iterable[Person], venues: Iterable[Venue], old_talks=[]):
        start = time.time()

        self.log.info("Generating schedule problem...")

        # venues = {v for talk in talks for v in talk.venues}
        problem = self.get_problem(venues=venues, talks=talks, old_talks=old_talks, people=people)

        self.log.info(
            "Problem generated (%s variables) in %.2f seconds, attempting to solve...",
            len(self.var_cache),
            time.time() - start,
        )

        solve_start = time.time()
        # We use CBC's simplex solver rather than dual, as it is faster and the
        # accuracy difference is negligable for this problem
        # We use COIN_CMD() over COIN() as it allows us to run in parallel mode
        problem.solve(pulp.COIN_CMD(dual=0, threads=8, msg=1, keepFiles=0))

        if pulp.LpStatus[self.problem.status] != "Optimal":
            print("Violated constraint:")
            print(self.violated_constr())
            print("Violating variable:")
            print(self.violating_var())
            raise Unsatisfiable()

        self.log.info(
            "Problem solved in %.2f seconds. Total runtime %.2f seconds.",
            time.time() - solve_start,
            time.time() - start,
        )

        return [
            (slot, talk.id, venue.id)
            for slot in self.slots_available
            for talk in talks
            for venue in venues
            if pulp.value(self.start_var(slot, talk.id, venue.id))
        ]

    # https://blend360.github.io/OptimizationBlog/solution%20notebook/infeasibility_resolution_with_pulp/
    def violated_constr(self):
        ret_suspected_constr = []
        self.suspected_constr = []
        for c in self.problem.constraints.values():
            if not c.valid(0):
                ## check if the constraint is a soft constraint;
                ## soft constraints should not cause infeasibility and may be ignored
                constr_name = [c.name, c.__dict__, c.items()] # c.toDict()['name']
                ret_suspected_constr.append(constr_name)
                self.suspected_constr.append(c)
        return ret_suspected_constr

    def violating_var(self):
        ret_suspected_var = []
        self.suspected_var = []
        for v in self.problem.variables():
            if not v.valid(0):
                var_name = [v.name, v.__dict__] # v.toDict()['name']
                ret_suspected_var.append(var_name)
                self.suspected_var.append(v)
        return ret_suspected_var

    @classmethod
    def num_slots(self, start_time, end_time):
        return int((end_time - start_time).total_seconds() / 60 / 5) # /10

    @classmethod
    def calculate_slots(self, event_start, range_start, range_end, spacing_slots=1):
        slot_start = int((range_start - event_start).total_seconds() / 60 / 5) # /10
        # We add the number of slots that must be between events to the end to
        # allow events to finish in the last period of the schedule
        return range(
            slot_start,
            slot_start + SlotMachine.num_slots(range_start, range_end) + spacing_slots,
        )

    def calc_time(self, event_start: datetime, slots: int):
        return event_start + relativedelta.relativedelta(minutes=slots * 5) # * 10

    def calc_slot(self, event_start: datetime, time: datetime):
        return int((time - event_start).total_seconds() / 60 / 5) # /10

    def prep_schedule(self, schedule: dict, spacing_slots: int = 1) -> dict:
        talks = []
        talk_data = {}
        old_slots = []
        people = []
        venues = []

        event_start = min(
            parser.parse(r["start"]) for event in schedule["talks"] for r in event["time_ranges"]
        )

        for person in schedule["people"]:
            slots = []
            preferred_slots = []
            prefs = {}

            for trange in person.get("time_ranges",[]):
                person_slots = SlotMachine.calculate_slots(
                    event_start,
                    parser.parse(trange["start"]),
                    parser.parse(trange["end"]),
                    spacing_slots,
                )
                slots.extend(person_slots)

            for trange in person.get("preferred_time_ranges", []):
                person_slots = SlotMachine.calculate_slots(
                    event_start,
                    parser.parse(trange["start"]),
                    parser.parse(trange["end"]),
                    spacing_slots,
                )
                preferred_slots.extend(person_slots)

            for talk_id in person.get("preferences",[]):
                prefs[int(talk_id)] = person["preferences"][talk_id]

            people.append(
                self.Person(
                    id=person["id"],
                    name=person["name"],
                    slots=slots,
                    preferred_slots=preferred_slots,
                    irl=(person["attending"]==1),
                    preferences=prefs
                )
            )

        self.people_by_id = {person.id: person for person in people}
        self.people_by_name = {person.name: person for person in people}

        for venue in schedule["venues"]:
            slots = []

            for trange in venue.get("time_ranges",[]):
                venue_slots = SlotMachine.calculate_slots(
                    event_start,
                    parser.parse(trange["start"]),
                    parser.parse(trange["end"]),
                    0,
                )
                slots.extend(venue_slots)

            venues.append(
                self.Venue(
                    id=venue["id"],
                    name=venue["name"],
                    capacity=venue["capacity"],
                    slots=slots
                )
            )

        self.venues_by_id = {venue.id: venue for venue in venues}

        for event in schedule["talks"]:
            talk_data[event["id"]] = event
            spacing_slots = event.get("spacing_slots", spacing_slots)
            slots = []
            preferred_slots = []

            if "plenary" in event:
                plenary = event["plenary"]
            else:
                plenary = 0

            if "irl_only" in event:
                irl_only = event["irl_only"]
            else:
                irl_only = 0

            for trange in event["time_ranges"]:
                event_slots = SlotMachine.calculate_slots(
                    event_start,
                    parser.parse(trange["start"]),
                    parser.parse(trange["end"]),
                    spacing_slots,
                )
                slots.extend(event_slots)

            for trange in event.get("preferred_time_ranges", []):
                event_slots = SlotMachine.calculate_slots(
                    event_start,
                    parser.parse(trange["start"]),
                    parser.parse(trange["end"]),
                    spacing_slots,
                )
                preferred_slots.extend(event_slots)

            self.slots_available = self.slots_available.union(set(slots))

            self.talk_permissions[event["id"]] = {
                "slots": slots,
                "venues": event["valid_venues"],
            }

            speaker_ids = []
            for speaker in event["speakers"]:
                speaker_ids.append(self.people_by_name[speaker].id)

            talks.append(
                self.Talk(
                    id=event["id"],
                    venues=event["valid_venues"],
                    slots=slots,
                    speakers=speaker_ids,
                    # We add the number of spacing slots that must be between
                    # events to the duration
                    duration=int(event["duration"] / 5) + spacing_slots, # / 10
                    preferred_venues=event.get("preferred_venues", []),
                    preferred_slots=preferred_slots,
                    plenary=plenary,
                    irl_only=irl_only,
                    prereqs=event.get("prereqs", []),
                    rest=event.get("rest", 0)
                )
            )

            if "time" in event and "venue" in event:
                old_slots.append(
                    (
                        self.calc_slot(event_start, parser.parse(event["time"])),
                        event["id"],
                        event["venue"],
                    )
                )

        self.talks_by_id = {talk.id: talk for talk in talks}
        self.talks_by_speaker: dict[int, list[int]] = {}
        for talk in talks:
            for speaker in talk.speakers:
                self.talks_by_speaker.setdefault(speaker, []).append(talk.id)

        return { "talks": talks, "old_slots": old_slots, "people": people, "talk_data": talk_data, "event_start": event_start, "venues": venues }

    def schedule(self, schedule: dict, spacing_slots: int = 1) -> list[dict]:
        prep = self.prep_schedule(schedule=schedule,spacing_slots=spacing_slots)

        solved = self.schedule_talks( talks=prep["talks"], old_talks=prep["old_slots"], people=prep["people"], venues=prep["venues"])

        for slot_id, talk_id, venue_id in solved:
            prep["talk_data"][talk_id]["time"] = str(self.calc_time(prep["event_start"], slot_id))
            prep["talk_data"][talk_id]["venue"] = venue_id

        return list(prep["talk_data"].values())

    def schedule_from_file(self, infile, outfile):
        schedule = json.load(open(infile))

        result = self.schedule(schedule)

        with open(outfile, "w") as f:
            json.dump(result, f, sort_keys=True, indent=4, separators=(",", ": "))
