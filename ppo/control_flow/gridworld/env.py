import functools
from collections import defaultdict

import numpy as np
from gym import spaces
from rl_utils import hierarchical_parse_args

import ppo.control_flow.env
from ppo import keyboard_control
from ppo.control_flow.env import build_parser, State
from ppo.control_flow.lines import Subtask, Padding, Line, While, If, EndWhile, Else


class Env(ppo.control_flow.env.Env):
    wood = "wood"
    gold = "gold"
    iron = "iron"
    merchant = "merchant"
    bridge = "bridge"
    agent = "agent"
    mine = "mine"
    build = "build"
    visit = "visit"
    objects = [wood, gold, iron, merchant]
    other_objects = [bridge, agent]
    world_objects = objects + other_objects
    interactions = [mine, build, visit]

    def __init__(
        self,
        max_while_objects,
        num_subtasks,
        num_excluded_objects,
        temporal_extension,
        world_size=6,
        **kwargs,
    ):
        self.temporal_extension = temporal_extension
        self.num_excluded_objects = num_excluded_objects
        self.max_while_objects = max_while_objects

        def subtasks():
            for obj in self.objects:
                for interaction in self.interactions:
                    yield interaction, obj

        self.subtasks = list(subtasks())
        num_subtasks = len(self.subtasks)
        super().__init__(num_subtasks=num_subtasks, **kwargs)
        self.world_size = world_size
        self.world_shape = (len(self.world_objects), self.world_size, self.world_size)

        self.action_space = spaces.MultiDiscrete(
            np.array([num_subtasks + 1, 2 * self.n_lines, 2, 2, self.n_lines])
        )
        self.observation_space.spaces.update(
            obs=spaces.Box(low=0, high=1, shape=self.world_shape),
            lines=spaces.MultiDiscrete(
                np.array(
                    [
                        [
                            len(self.line_types),
                            1 + len(self.interactions),
                            1 + len(self.objects),
                        ]
                    ]
                    * self.n_lines
                )
            ),
        )

    def line_str(self, line: Line):
        if isinstance(line, Subtask):
            i, o = line.id
            return f"Subtask {self.subtasks.index(line.id)}: {line.id}"
        elif isinstance(line, (If, While)):
            return f"{line}: {line.id}"
        else:
            return f"{line}"

    def print_obs(self, obs):
        obs = obs.transpose(1, 2, 0).astype(int)
        grid_size = obs.astype(int).sum(-1).max()  # max objects per grid
        chars = [" "] + [o for o, *_ in self.world_objects]
        for i, row in enumerate(obs):
            string = ""
            for j, channel in enumerate(row):
                int_ids = 1 + np.arange(channel.size)
                number = channel * int_ids
                crop = sorted(number, reverse=True)[:grid_size]
                string += "".join(chars[x] for x in crop) + "|"
            print(string)
            print("-" * len(string))

    @functools.lru_cache(maxsize=200)
    def preprocess_line(self, line):
        if line is Padding:
            return [self.line_types.index(Padding), 0, 0]
        elif type(line) is Else:
            return [self.line_types.index(Else), 0, 0]
        elif type(line) is Subtask:
            i, o = line.id
            i, o = self.interactions.index(i), self.objects.index(o)
            return [self.line_types.index(Subtask), i + 1, o + 1]
        else:
            return [
                self.line_types.index(type(line)),
                0,
                self.objects.index(line.id) + 1,
            ]

    def world_array(self, object_pos, agent_pos):
        world = np.zeros(self.world_shape)
        for o, p in object_pos + [(self.agent, agent_pos)]:
            p = np.array(p)
            world[tuple((self.world_objects.index(o), *p))] = 1
        return world

    @staticmethod
    def evaluate_line(line, object_pos, condition_evaluations):
        if line is None:
            return None
        if type(line) is Subtask:
            return 1
        else:
            evaluation = any(o == line.id for o, _ in object_pos)
            if type(line) in (If, While):
                condition_evaluations[type(line)] += [evaluation]
            return evaluation

    def state_generator(self, lines) -> State:
        assert self.max_nesting_depth == 1
        agent_pos = self.random.randint(0, self.world_size, size=2)
        object_pos = self.populate_world(lines)
        line_iterator = self.line_generator(lines)
        condition_evaluations = defaultdict(list)
        self.time_remaining = self.time_to_waste

        def get_nearest(to):
            candidates = [np.array(p) for o, p in object_pos if o == to]
            if candidates:
                return min(candidates, key=lambda k: np.sum(np.abs(agent_pos - k)))

        def next_subtask(l):
            if l is None:
                l = line_iterator.send(None)
            else:
                l = line_iterator.send(
                    self.evaluate_line(lines[l], object_pos, condition_evaluations)
                )
            while not (l is None or type(lines[l]) is Subtask):
                l = line_iterator.send(
                    self.evaluate_line(lines[l], object_pos, condition_evaluations)
                )
            if l is not None:
                assert type(lines[l]) is Subtask
                _, o = lines[l].id
                n = get_nearest(o)
                if n is not None:
                    self.time_remaining += 1 + np.max(np.abs(agent_pos - n))
            return l

        possible_objects = [o for o, _ in object_pos]
        prev, ptr = 0, next_subtask(None)
        term = False
        while True:
            term |= not self.time_remaining
            subtask_id = yield State(
                obs=self.world_array(object_pos, agent_pos),
                condition=None,
                prev=prev,
                ptr=ptr,
                condition_evaluations=condition_evaluations,
                term=term,
            )
            self.time_remaining -= 1
            interaction, obj = self.subtasks[subtask_id]

            def pair():
                return (
                    obj,
                    tuple(agent_pos)
                    if self.temporal_extension
                    else next((p for o, p in object_pos if o == obj), None),
                )

            def on_object():
                return pair() in object_pos  # standing on the desired object

            correct_id = (interaction, obj) == lines[ptr].id
            if on_object() or not self.temporal_extension:
                if interaction in (self.mine, self.build):
                    if pair() in object_pos:
                        object_pos.remove(pair())
                        if correct_id:
                            possible_objects.remove(obj)
                        else:
                            term = True
                    else:
                        # subtask is impossible
                        prev, ptr = ptr, None
                if interaction == self.build:
                    object_pos.append((self.bridge, tuple(agent_pos)))
                if correct_id:
                    prev, ptr = ptr, next_subtask(ptr)
            else:
                nearest = get_nearest(obj)
                if nearest is not None:
                    agent_pos += np.clip(nearest - agent_pos, -1, 1)
                elif correct_id and obj not in possible_objects:
                    # subtask is impossible
                    prev, ptr = ptr, None

    def populate_world(self, lines):
        line_io = [line.id for line in lines if type(line) is Subtask]
        line_pos = self.random.randint(0, self.world_size, size=(len(line_io), 2))
        object_pos = [
            (o, tuple(pos)) for (interaction, o), pos in zip(line_io, line_pos)
        ]
        while_blocks = defaultdict(list)  # while line: child subtasks
        active_whiles = []
        for interaction, line in enumerate(lines):
            if type(line) is While:
                active_whiles += [interaction]
            elif type(line) is EndWhile:
                active_whiles.pop()
            elif active_whiles and type(line) is Subtask:
                while_blocks[active_whiles[-1]] += [interaction]
        for while_line, block in while_blocks.items():
            obj = lines[while_line].id
            l = self.random.choice(block)
            i = self.random.choice(2)
            assert self.interactions[i] in (self.mine, self.build)
            line_id = self.interactions[i], obj
            assert line_id in ((self.mine, obj), (self.build, obj))
            lines[l] = Subtask(line_id)
            if not self.evaluating and obj in self.world_objects:
                num_obj = self.random.randint(self.max_while_objects + 1)
                if num_obj:
                    pos = self.random.randint(0, self.world_size, size=(num_obj, 2))
                    object_pos += [(obj, tuple(p)) for p in pos]
        return object_pos

    def assign_line_ids(self, lines):
        excluded = self.random.randint(
            len(self.objects), size=self.num_excluded_objects
        )
        included_objects = [o for i, o in enumerate(self.objects) if i not in excluded]

        interaction_ids = self.random.choice(len(self.interactions), size=len(lines))
        object_ids = self.random.choice(len(included_objects), size=len(lines))
        line_ids = self.random.choice(len(self.objects), size=len(lines))

        for line, line_id, interaction_id, object_id in zip(
            lines, line_ids, interaction_ids, object_ids
        ):
            if line is Subtask:
                subtask_id = (
                    self.interactions[interaction_id],
                    included_objects[object_id],
                )
                yield Subtask(subtask_id)
            else:
                yield line(self.objects[line_id])


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser = build_parser(parser)
    parser.add_argument("--world-size", default=4, type=int)
    parser.add_argument("--seed", default=0, type=int)
    args = hierarchical_parse_args(parser)

    def action_fn(string):
        try:
            return int(string), 0
        except ValueError:
            return

    keyboard_control.run(Env(**args, baseline=False), action_fn=action_fn)