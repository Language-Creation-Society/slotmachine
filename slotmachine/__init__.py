from __future__ import annotations
from collections import namedtuple
from datetime import datetime
from dateutil import parser, relativedelta
from typing import Iterable
import json
import time
import logging
import pulp
import math
import sys
import pathlib

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
# root = logging.getLogger()
# root.setLevel(logging.DEBUG)
#
# handler = logging.StreamHandler(sys.stdout)
# handler.setLevel(logging.DEBUG)
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# handler.setFormatter(formatter)
# root.addHandler(handler)

class Unsatisfiable(Exception):
    pass


class SlotMachine(object):
    SLOT_INCREMENT=5 # minutes of granularity
    BIGNUM=2**32

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
        ("id", "name", "preferred_slots", "slots", "irl", "preferences", "languages", "attending" ),
    )

    Talk = namedtuple(
        "Talk",
        ("id", "duration", "durations", "venues", "speakers", "preferred_venues", "preferred_slots", "slots", "plenary", "irl_only", "prereqs", "rest", "languages", "before_rest", "after_rest", "meetup", "invite_only", "similarities" ),
    )

    Language = namedtuple(
        "Language",
        {"id", "name"}
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

    # TODO optimise duration assignments
    def duration(self, talk_id) -> pulp.LpVariable:
        name = "LENGTH_%D" % (talk_id)
        if name in self.var_cache:
            return self.var_cache[name]

        durations = self.talks_by_id[talk_id].durations

        var = pulp.LpVariable(name, lowBound=min(durations), upBound=max(durations), cat="Integer")

        self.var_cache[name] = var
        return var

    def adjacent_or_before(self, talk1_id, talk2_id, venue_id) -> pulp.LpVariable:
        """A 0/1 variable that is 1 if talk2 starts no later than directly after talk1"""
        name = "ADJACENT_OR_BEFORE_V_%d_%d_%d" % (talk1_id, talk2_id, venue_id)
        if name in self.var_cache:
            return self.var_cache[name]

        # if talk1_id == talk2_id:
        #     var = pulp.LpVariable(name, lowBound=0, upBound=0, cat="Binary") # cat="Integer")
        # else:
        var = pulp.LpVariable(name, cat="Binary")

        self.var_cache[name] = var
        return var

    def adjacent(self, talk1_id, talk2_id, venue_id) -> pulp.LpVariable:
        """A 0/1 variable that is 1 if talk 2 is adjacent to talk1"""
        name = "ADJACENT_V_%d_%d_%d" % (talk1_id, talk2_id, venue_id)
        if name in self.var_cache:
            return self.var_cache[name]

        # if talk1_id == talk2_id:
        #     var = pulp.LpVariable(name, lowBound=0, upBound=0, cat="Binary") # cat="Integer")
        # else:
        var = pulp.LpVariable(name, cat="Binary")

        self.var_cache[name] = var
        return var

    def simultaneous(self, talk1_id, talk2_id) -> pulp.LpVariable:
        """A 0/1 variable that is 1 if talk 1 & talk 2 are simultaneous in different places"""
        name = "SIMULTANEOUS_V_%d_%d" % (talk1_id, talk2_id)
        if name in self.var_cache:
            return self.var_cache[name]

        if talk1_id == talk2_id:
            var = pulp.LpVariable(name, lowBound=0, upBound=0, cat="Binary") # cat="Integer")
        else:
            var = pulp.LpVariable(name, cat="Binary")

        self.var_cache[name] = var
        return var

    def distance(self, talk1_id, talk2_id) -> pulp.LpVariable:
        "Signed integer number of slots between start of talk1 and talk2"
        name = "DISTANCE_V_%d_%d" % (talk1_id, talk2_id)
        if name in self.var_cache:
            return self.var_cache[name]

        # if talk1_id == talk2_id:
        #     var = pulp.LpVariable(name, lowBound=0, upBound=0, cat="Integer")
        # else:
        var = pulp.LpVariable(name, cat="Integer")

        self.var_cache[name] = var
        return var

    def abs_distance(self, talk1_id, talk2_id) -> pulp.LpVariable:
        "Absolute integer number of slots between talk1 and talk2"
        name = "ABS_DISTANCE_V_%d_%d" % (talk1_id, talk2_id)
        if name in self.var_cache:
            return self.var_cache[name]

        if talk1_id == talk2_id:
            var = pulp.LpVariable(name, lowBound=0, upBound=0, cat="Integer")
        else:
            var = pulp.LpVariable(name, lowBound=0, cat="Integer")

        self.var_cache[name] = var
        return var

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

        self.problem.addConstraint(variable == definition, "CONTIGUITY_%d_%d_%d" % (slot, talk_id, venue))
        self.var_cache[name] = variable
        return variable

    def attending_some(self, talk_id, person_id) -> pulp.LpVariable:
        """A 0/1 variable that is 1 if this person is attending this talk in whole"""
        name = "ATTEND_%d_%d" % (talk_id, person_id)
        if name in self.var_cache:
            return self.var_cache[name]

        variable = pulp.LpVariable(name, cat="Binary")

        self.var_cache[name] = variable
        return variable

    def attending_at(self, slot, talk_id, person_id) -> pulp.LpVariable:
        """A 0/1 variable that is 1 if talk with ID talk_id is active during
        this slot and this person is attending it"""
        name = "ATTEND_AT_%d_%d_%d" % (slot, talk_id, person_id)
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

    def get_problem(self, venues: Iterable[Venue], talks: Iterable[Talk], old_talks, people: Iterable[Person], languages: Iterable[Language]) -> pulp.LpProblem:
        # Reset problem and cached variables
        self.problem = pulp.LpProblem("Scheduler", pulp.LpMaximize)
        self.var_cache = {}

        self.talks_by_id = {talk.id: talk for talk in talks}
        self.people_by_id = {person.id: person for person in people}
        talk_ids = {t.id for t in talks}
        venue_ids = {v.id for v in venues}
        people_ids = {p.id for p in people}
        people_with_preferences_ids = {p.id for p in people if len(p.preferences) >= 1}
        rest_talks = [ talk.id for talk in talks if (talk.rest == 1) ]
        nonrest_talks = [ talk.id for talk in talks if (talk.rest == 0) ]

        # TODO
        # max_session_size = 4
        # max_sessions = 20
        # possible_sessions = [tuple(c) for c in pulp.allcombinations(talks, max_session_size)]
        # # create a binary variable to state that a table setting is used
        # x = pulp.LpVariable.dicts(
        #     "session", possible_sessions, lowBound=0, upBound=1, cat=pulp.LpInteger # cat=pulp.LpBinary
        # )
        # self.problem += pulp.lpSum([happiness(session) * x[session] for session in possible_sessions])
        # self.problem += (
        #     pulp.lpSum([x[session] for session in possible_sessions]) <= max_sessions,
        #     "Maximum_number_of_sessions",
        # )
        # # A talk must be in exactly one session
        # for talk in talks:
        # self.problem += (
        #    pulp.lpSum([x[session] for session in possible_sessions if talk in session]) == 1,
        #    f"Must_include_talk_ID_{talk.id}",
        # )

        # TODO
        # # Every talk has a permitted duration
        # for talk in talks:
        #     self.problem.addConstraint(
        #         pulp.lpSum(
        #             self.duration(talk.id)
        #         )
        #         == 1,
        #         name = "DURATIONS_%d" % (talk.id)
        #     )

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

        # it's only possible to attend a thing when it's active
        for talk in talks:
            for slot in self.slots_available:
                for person in people:
                    self.problem.addConstraint(
                        self.attending_at(slot, talk.id, person.id)
                        <= pulp.lpSum(
                            self.active(slot, talk.id, vid)
                            for vid in venue_ids
                        ),
                        name = "ATTEND_AVAILABILITY_%d_%d_%d" % (talk.id, slot, person.id)
                    )

        # people can attend at most one thing at a time
        for person in people:
            for slot in self.slots_available:
                self.problem.addConstraint(
                    pulp.lpSum(
                        self.attending_at(slot, tid, person.id)
                        for tid in talk_ids
                    )
                    <= 1,
                    name = "UNIPRESENCE_%d_%d" % (person.id, slot)
                )

        # this just sets the distance variable
        # start time of talk2 - start time of talk1 = distance
        for talk1 in talks:
            for talk2 in talks:
                self.problem.addConstraint(
                    pulp.LpAffineExpression([
                        (self.start_var(s, talk2.id, vid), s)
                        for s in self.slots_available
                        for vid in venue_ids
                    ] + [
                        (self.start_var(s, talk1.id, vid), -s)
                        for s in self.slots_available
                        for vid in venue_ids
                    ])
                    == self.distance(talk1.id, talk2.id),
                    name = "DISTANCE_C_%d_%d" % (talk2.id, talk1.id)
                )

        # these set the abs_distance variable
        for talk1 in talks:
            for talk2 in talks:
                self.problem.addConstraint(
                    self.distance(talk1.id, talk2.id)
                    <= self.abs_distance(talk1.id, talk2.id),
                    name = "ABS_DISTANCE_12_C_%d_%d" % (talk2.id, talk1.id)
                )
                self.problem.addConstraint(
                    self.distance(talk2.id, talk1.id)
                    <= self.abs_distance(talk1.id, talk2.id),
                    name = "ABS_DISTANCE_21_C_%d_%d" % (talk2.id, talk1.id)
                )

        # this just sets the adjacency variable, it isn't a constraint as such unless we tie it to something else (e.g. in the weight function)
        # start time of talk2 - start time of talk1 + adjacencyvar*bignum <= bignum + talk1 duration
        for talk1 in talks:
            for talk2 in talks:
                for vid in venue_ids:
                    self.problem.addConstraint(
                        pulp.LpAffineExpression([
                            (self.start_var(s, talk2.id, vid), s)
                            for s in self.slots_available
                            # for vid in venue_ids
                        ] + [
                            (self.start_var(s, talk1.id, vid), -s)
                            for s in self.slots_available
                            # for vid in venue_ids
                        ])
                        + (3 * self.BIGNUM * self.adjacent_or_before(talk1.id, talk2.id, vid))
                        <= self.BIGNUM * (
                            1
                            + pulp.lpSum(
                                self.start_var(s, talk1.id, vid)
                                for s in self.slots_available
                            )
                            + pulp.lpSum(
                                self.start_var(s, talk2.id, vid)
                                for s in self.slots_available
                            )
                        )
                        + talk1.duration,
                        name = "ADJACENT_OR_BEFORE_C_%d_%d_%d" % (talk2.id, talk1.id, vid)
                    )
                    self.problem.addConstraint(
                        self.adjacent_or_before(talk1.id, talk2.id, vid)
                        + self.adjacent_or_before(talk2.id, talk1.id, vid)
                        - 1
                        <= self.adjacent(talk1.id, talk2.id, vid),
                        name = "ADJACENT_C_%d_%d_%d" % (talk1.id, talk2.id, vid)
                    )
                    self.problem.addConstraint(
                        self.adjacent_or_before(talk1.id, talk2.id, vid)
                        >= self.adjacent(talk1.id, talk2.id, vid),
                        name = "ADJACENT_C2_%d_%d_%d" % (talk1.id, talk2.id, vid)
                    )

        # TODO
        # for talk1 in talks:
        #     for talk2 in talks:
        #         for s in self.slots_available
        #             self.problem.addConstraint(
        #                 pulp.lpSum(
        #                     self.start_var(s, talk1.id, vid)
        #                     + self.start_var(s, talk1.id, vid)
        #                     for vid in venue_ids
        #                 )
        #                 <= 1,
        #                 name = "SIMULTANEOUS_NOT_IN_SAME_VENUE_%d_%d_%d" % (talk1.id, talk2.id, s)
        #             )

        # # TODO make a weight that prefers anticorrelated things to be scheduled against each other
        # # this just sets the simultaneous variable, it isn't a constraint as such unless we tie adjacent_or_before to something else
        # # start time of talk2 - start time of talk1 + simultaneous*bignum <= bignum + talk2 duration
        # for talk1 in talks:
        #     for talk2 in talks:
        #         self.problem.addConstraint(
        #             pulp.LpAffineExpression([
        #                 (self.start_var(s, talk2.id, vid), s)
        #                 for s in self.slots_available
        #                 for vid in venue_ids
        #             ] + [
        #                 (self.start_var(s, talk1.id, vid), -s)
        #                 for s in self.slots_available
        #                 for vid in venue_ids
        #             ]) + self.BIGNUM * self.simultaneous(talk1.id, talk2.id)
        #             <= self.BIGNUM + talk2.duration,
        #             name = "SIMULTANEOUS_C_%d_%d" % (talk2.id, talk1.id)
        #         )

        # FIXME
        # # people attend something at all times they can
        # for person in people:
        #     for slot in person.slots:
        #         self.problem.addConstraint(
        #             pulp.lpSum(
        #                 self.attending_at(slot, tid, person.id)
        #                 for tid in talk_ids
        #             )
        #             == 1,
        #             name = "ATTENDANCE_%d_%d" % (person.id, slot)
        #         )

        # IRL-only talks
        for talk in talks:
            if (talk.irl_only == 1):
                for person in people:
                    if (person.attending == 0):
                        self.problem.addConstraint(
                            self.attending_some(talk.id, person.id)
                            == 0,
                            name = "IRL_ONLY_%d_%d" % (talk.id, person.id)
                        )

        # invite-only talks
        for talk in talks:
            if (talk.invite_only == 1):
                for person in people:
                    if (person.preferences.get(talk.id, 0) == 0):
                        self.problem.addConstraint(
                            self.attending_some(talk.id, person.id)
                            == 0,
                            name = "INVITE_ONLY_%d_%d" % (talk.id, person.id)
                        )

        # TODO
        # # room capacity
        # for vid in venue_ids:
        #     for slot in self.slots_available:
        #         for talk in talks:
        #             self.problem.addConstraint(
        #                 pulp.lpSum(
        #                     self.attending_at(slot, talk.id, pid)
        #                     for pid in people_ids
        #                     if (self.people_by_id[pid].attending == 1)
        #                     if (self.active(slot,talk.id, vid) == 1)
        #                 )
        #                 <= self.venues_by_id[vid].capacity,
        #                 name = "CAPACITY_%d_%d" % (vid, talk.id)
        #             )

        # require speakers to attend their whole talk
        for talk in talks:
            for speaker_id in talk.speakers:
                self.problem.addConstraint(
                    pulp.lpSum(
                        self.attending_at(s, talk.id, speaker_id)
                        for s in talk.slots
                    )
                    == talk.duration,
                    name = "SPEAKER_ATTENDS_WHOLE_%d_%d" % (talk.id, speaker_id)
                )

        # require speakers to attend
        for talk in talks:
            for speaker_id in talk.speakers:
                self.problem.addConstraint(
                    self.attending_some(talk.id, speaker_id)
                    == 1,
                    name = "SPEAKER_ATTENDS_%d_%d" % (talk.id, speaker_id)
                )

        # people attend non-meetup talks in full or not at all
        for talk in talks:
            if (talk.meetup == 0):
                for person in people:
                    self.problem.addConstraint(
                        pulp.lpSum(
                            self.attending_at(s, talk.id, person.id)
                            for s in talk.slots
                        )
                        == talk.duration * self.attending_some( talk.id, person.id ),
                        name = "ATTEND_FULL_TALK_%d_%d" % (talk.id, person.id)
                    )

        # # FIXME - no ^ allowed in affine expression
        # # attending any is attending some
        # for talk in talks:
        #     for person in people:
        #         self.problem.addConstraint(
        #             pulp.lpSum(
        #                 self.attending_at(s, talk.id, person.id)
        #                 for s in talk.slots
        #             ) ^ 0
        #             == self.attending_some( talk.id, person.id ),
        #             name = "ATTEND_SOME_%d_%d" % (talk.id, person.id)
        #         )

        # disallow person's unavailable slots
        for person in people:
            self.problem.addConstraint(
                pulp.lpSum(
                    self.attending_at(s, t, person.id)
                    for s in (self.slots_available - set(person.slots))
                    for t in talk_ids
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
        # start time of talk2 - start time of talk1 >= duration of talk1.
        for talk2 in talks:
            for t1id in talk2.prereqs:
                talk1 = self.talks_by_id[t1id]
                self.problem.addConstraint(
                    pulp.LpAffineExpression([
                        (self.start_var(s, talk2.id, vid), s)
                        for s in self.slots_available
                        for vid in venue_ids
                    ] + [
                        (self.start_var(s, t1id, vid), -s)
                        for s in self.slots_available
                        for vid in venue_ids
                    ])
                    >= talk1.duration,
                    name = "PREREQS_%d_%d" % (talk2.id, t1id)
                )

        for t2id in rest_talks:
            talk2 = self.talks_by_id[t2id]
            if talk2.rest == 1:
                for t1id in talk2.prereqs:
                    talk1 = self.talks_by_id[t1id]
                    if talk1.rest == 1:
                        # Require rests (talk2) to come at least an hour after the prior rest (talk1):
                        # start time of talk2 - start time of talk1 >= duration of talk1 + 1h.
                        self.problem.addConstraint(
                            pulp.LpAffineExpression([
                                (self.start_var(s, t2id, vid), s)
                                for s in self.slots_available
                                for vid in venue_ids
                            ] + [
                                (self.start_var(s, t1id, vid), -s)
                                for s in self.slots_available
                                for vid in venue_ids
                            ])
                            >= talk1.duration + math.ceil(60/self.SLOT_INCREMENT),
                            name = "REST_MIN_SPACING_%d_%d" % (t2id, t1id)
                        )
                        # Require rests (talk2) to come no more than 2 hours after the prior rest (talk1):
                        self.problem.addConstraint(
                            pulp.LpAffineExpression([
                                (self.start_var(s, t2id, vid), s)
                                for s in self.slots_available
                                for vid in venue_ids
                            ] + [
                                (self.start_var(s, t1id, vid), -s)
                                for s in self.slots_available
                                for vid in venue_ids
                            ])
                            <= talk1.duration + math.ceil(120/self.SLOT_INCREMENT),
                            name = "REST_MAX_SPACING_%d_%d" % (t2id, t1id)
                        )

        # Require some things directly before rests
        for t in talks:
            if (t.before_rest == 1):
                for slot in self.slots_available:
                    for vid in venue_ids:
                        self.problem.addConstraint(
                            pulp.lpSum(
                                (self.active(slot, t.id, vid) * t.before_rest * self.BIGNUM)
                            )
                            # any nonrest after this breaks the constraint
                            + pulp.lpSum(
                                self.active(slot + 1, t2id, vid)
                                for t2id in (set(nonrest_talks) - set([t.id]))
                            )
                            # itself, or a rest, must be after each of its slots
                            - self.active(slot + 1, t.id, vid)
                            - pulp.lpSum(
                                self.active(slot + 1, t2id, vid2)
                                for t2id in set(rest_talks)
                                for vid2 in venue_ids
                            )
                            <= self.BIGNUM - 1,
                            name = "BEFORE_REST_%d_%d_%d" % (t.id, slot, vid)
                        )

        # Require some things directly after rests
        for t in talks:
            if (t.after_rest == 1):
                for slot in self.slots_available:
                    for vid in venue_ids:
                        self.problem.addConstraint(
                            pulp.lpSum(
                                (self.active(slot, t.id, vid) * t.after_rest * self.BIGNUM)
                            )
                            + pulp.lpSum(
                                self.active(slot - 1, t2id, vid)
                                for t2id in (set(nonrest_talks) - set([t.id]))
                            )
                            - self.active(slot - 1, t.id, vid)
                            - pulp.lpSum(
                                self.active(slot - 1, t2id, vid2)
                                for t2id in set(rest_talks)
                                for vid2 in venue_ids
                            )
                            <= self.BIGNUM - 1,
                            name = "AFTER_REST_%d_%d_%d" % (t.id, slot, vid)
                        )

        # plenary talks can't have anything else parallel
        for slot in self.slots_available:
            self.problem.addConstraint(
                pulp.lpSum(
                    (self.active(slot, t.id, vid) * t.plenary * self.BIGNUM)
                    + self.active(slot, t.id, vid)
                    for t in talks
                    for vid in venue_ids
                )
                <= self.BIGNUM + 1,
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

        # TODO
        # # 2 terps have to attend signers' talks
        # for langid in [ 1, 2 ]
        #     for person in self.people:
        #         if langid in person.languages:
        #             for slot in self.slots_available:
        #                 self.problem.addConstraint(
        #                     ,
        #                     name = "TERPS_FOR_%d_%d" % (langid, person.id)
        #                 )

        self.problem += (
            10
            * pulp.lpSum(
                # try to have similar talks together
                self.adjacent(talk1.id, talk2.id, vid) * max(talk1.similarities.get(talk2.id, 0), talk2.similarities.get(talk1.id, 0))
                for vid in venue_ids
                for talk1 in talks
                for talk2 in talks
            )
            + 1
            * pulp.lpSum(
                # attendees try to go to as much as possible
                self.attending_at(s, tid, pid)
                for tid in talk_ids
                for pid in people_ids # people_with_preferences_ids
                for s in self.slots_available
            )
            + 10
            * pulp.lpSum(
                # attendee preferences
                (
                    (
                        self.attending_at(s, tid, pid)
                        * self.people_by_id[pid].preferences.get(tid, (0 if (self.talks_by_id[tid].meetup == 1) else 1))
                        * (
                            1
                            # worth extra if invite only
                            + (0 if (self.talks_by_id[tid].invite_only == 1) else 1)
                            # worth half if not in preferred slot
                            + (1 * (s in self.people_by_id[pid].preferred_slots))
                        )/2
                    ) / 7
                    # / (
                    #     min(1,sum(self.people_by_id[pid].preferences.values())) # normalize so fully satisfied person = 1
                    #     * len(people_with_preferences_ids) # normalize so fully satisfied audience = 1
                    # )
                )
                for tid in talk_ids
                for pid in people_ids # people_with_preferences_ids
                for s in self.slots_available
            )
            + 5
            * pulp.lpSum(
                # Maximise the number of things in speakers' preferred times
                self.active(s, t.id, vid)
                for vid in venue_ids
                for t in talks
                for p in t.speakers
                for s in self.people_by_id[p].preferred_slots
            )
            + 5
            * pulp.lpSum(
                # Maximise the number of things in their preferred venues (for putting big talks on big stages)
                self.active(s, t.id, vid)
                for t in talks
                for vid in t.preferred_venues
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
            # + 10
            # * pulp.lpSum(
            #     # We'd like talks with a slot & venue to try and stay there if they can
            #     self.active(s, talk_id, venue_id)
            #     for (slot, talk_id, venue_id) in old_talks
            #     for s in range(slot, slot + self.talks_by_id[talk_id].duration)
            # )
            # + 5
            # * pulp.lpSum(
            #     # And we'd prefer to just move stage rather than slot
            #     self.active(s, talk_id, vid)
            #     for (slot, talk_id, _) in old_talks
            #     for s in range(slot, slot + self.talks_by_id[talk_id].duration)
            #     for vid in self.talk_permissions[talk_id]["venues"]
            # )
            # + 1
            # * pulp.lpSum(
            #     # But if they have to move slot, 60mins either way is ok
            #     self.active(s, talk_id, vid)
            #     for (slot, talk_id, _) in old_talks
            #     for s in range(slot - 6, slot + self.talks_by_id[talk_id].duration + 6)
            #     for vid in self.talk_permissions[talk_id]["venues"]
            # )
        )

        return self.problem

    def schedule_talks(self, talks: Iterable[Talk], people: Iterable[Person], venues: Iterable[Venue], languages: Iterable[Language], old_talks=[]):
        start = time.time()

        self.log.info("Generating schedule problem...")

        # venues = {v for talk in talks for v in talk.venues}
        problem = self.get_problem(venues=venues, talks=talks, old_talks=old_talks, people=people, languages=languages)

        self.log.info(
            "Problem generated (%s variables) in %.2f seconds, attempting to solve...",
            len(self.var_cache),
            time.time() - start,
        )

        solve_start = time.time()
        # We use CBC's simplex solver rather than dual, as it is faster and the
        # accuracy difference is negligable for this problem
        # We use COIN_CMD() over COIN() as it allows us to run in parallel mode

        # problem.solve(pulp.COIN_CMD(threads=16, keepFiles=0, timeLimit=1200, logPath=f'{pathlib.Path().resolve()}/coin.log')) # presolve=1, warmStart=1
        problem.solve(pulp.GUROBI_CMD(threads=16, timeLimit=1200)) # warmStart=1, keepFiles=0, logPath=f'{pathlib.Path().resolve()}/gurobi.log'

        if pulp.LpStatus[self.problem.status] != "Optimal":
            self.log.error("Violated constraint:")
            self.log.error(self.violated_constr())
            self.log.error("Violating variable:")
            self.log.error(self.violating_var())
            raise Unsatisfiable()

        self.log.info(
            "Problem solved in %.2f seconds. Total runtime %.2f seconds.",
            time.time() - solve_start,
            time.time() - start,
        )

        return [
            (slot, talk.id, venue.id,
                [
                    person.id
                    for person in people
                    if pulp.value(self.attending_some(talk.id, person.id))
                ],
                list(set([
                    person.id
                    for person in people
                    for offset in range(0, talk.duration) # 1,
                    if pulp.value(self.attending_at(slot + offset, talk.id, person.id))
                    # if talk.duration > 1
                ]))
            )
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
                constr_name = c.name # [c.name, c.__dict__, c.items()] # c.toDict()['name']
                ret_suspected_constr.append(constr_name)
                self.suspected_constr.append(c)
        return ret_suspected_constr

    def violating_var(self):
        ret_suspected_var = []
        self.suspected_var = []
        for v in self.problem.variables():
            if not v.valid(0):
                var_name = v.name # [v.name, v.__dict__] # v.toDict()['name']
                ret_suspected_var.append(var_name)
                self.suspected_var.append(v)
        return ret_suspected_var

    @classmethod
    def num_slots(self, start_time, end_time):
        return int(math.ceil((end_time - start_time).total_seconds() / 60 / self.SLOT_INCREMENT))

    @classmethod
    def calculate_slots(self, event_start, range_start, range_end, spacing_slots=1):
        slot_start = int(math.ceil((range_start - event_start).total_seconds() / 60 / self.SLOT_INCREMENT))
        # We add the number of slots that must be between events to the end to
        # allow events to finish in the last period of the schedule
        return range(
            slot_start,
            slot_start + SlotMachine.num_slots(range_start, range_end) + spacing_slots,
        )

    def calc_time(self, event_start: datetime, slots: int):
        return event_start + relativedelta.relativedelta(minutes=slots * self.SLOT_INCREMENT)

    def calc_slot(self, event_start: datetime, time: datetime):
        return int(math.ceil((time - event_start).total_seconds() / 60 / self.SLOT_INCREMENT))

    def prep_schedule(self, schedule: dict, spacing_slots: int = 1) -> dict:
        talks = []
        talk_data = {}
        old_slots = []
        people = []
        venues = []
        languages = []

        event_start = min(
            parser.parse(r["start"]) for event in schedule["talks"] for r in event["time_ranges"]
        )

        for language in schedule["languages"]:
            languages.append(
                self.Language(
                    id=language["id"],
                    name=language["name"]
                )
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
                    preferences=prefs,
                    languages=person.get("languages", [ 0 ] ), # 0 = English
                    attending=person["attending"]
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

            similarities = {}
            for talk2_id in event.get("similarities",[]):
                similarities[int(talk2_id)] = event["similarities"][talk2_id]

            duration=int(math.ceil(event["duration"] / self.SLOT_INCREMENT) + spacing_slots) # / 10

            talks.append(
                self.Talk(
                    id=event["id"],
                    venues=event["valid_venues"],
                    slots=slots,
                    speakers=speaker_ids,
                    # We add the number of spacing slots that must be between
                    # events to the duration
                    duration=duration,
                    durations=[int(math.ceil((d/self.SLOT_INCREMENT) + 1)) for d in event.get("durations", [duration])],
                    preferred_venues=event.get("preferred_venues", []),
                    preferred_slots=preferred_slots,
                    plenary=event.get("plenary", 0),
                    irl_only=event.get("irl_only", 0),
                    prereqs=event.get("prereqs", []),
                    rest=event.get("rest", 0),
                    languages=event.get("languages", [ 0 ]),  # 0 = English
                    before_rest=event.get("before_rest", 0),
                    after_rest=event.get("after_rest", 0),
                    meetup=event.get("meetup", 0),
                    invite_only=event.get("invite_only", 0),
                    similarities=similarities
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

        return { "talks": talks, "old_slots": old_slots, "people": people, "talk_data": talk_data, "event_start": event_start, "venues": venues, "languages": languages }

    def schedule(self, schedule: dict, spacing_slots: int = 1) -> list[dict]:
        prep = self.prep_schedule(schedule=schedule,spacing_slots=spacing_slots)

        solved = self.schedule_talks( talks=prep["talks"], old_talks=prep["old_slots"], people=prep["people"], venues=prep["venues"], languages=prep["languages"])

        for slot_id, talk_id, venue_id, attendees, partial_attendees in solved:
            prep["talk_data"][talk_id]["slot"] = slot_id
            prep["talk_data"][talk_id]["time"] = str(self.calc_time(prep["event_start"], slot_id))
            prep["talk_data"][talk_id]["end_time"] = str(self.calc_time(prep["event_start"], slot_id + self.talks_by_id[talk_id].duration))
            prep["talk_data"][talk_id]["venue"] = venue_id
            prep["talk_data"][talk_id]["attendees"] = attendees
            prep["talk_data"][talk_id]["partial_attendees"] = partial_attendees

        return list(prep["talk_data"].values())

    def schedule_from_file(self, infile, outfile):
        schedule = json.load(open(infile))

        result = self.schedule(schedule)

        with open(outfile, "w") as f:
            json.dump(result, f, sort_keys=True, indent=4, separators=(",", ": "))
