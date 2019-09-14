import copy
import itertools
from collections import namedtuple
from typing import Tuple
import numpy as np
import gym
from gym.utils import seeding

from ppo.blocks_world.constraints import Left, Right, Above, Below

Obs = namedtuple("Obs", "obs go")
Last = namedtuple("Last", "action reward terminal go")


class Env(gym.Env):
    def __init__(self, n_cols: int, seed: int, time_limit: int, n_constraints: int):
        self.n_constraints = n_constraints
        self.search_distance = time_limit - n_constraints
        self.n_rows = self.n_cols = n_cols
        self.n_grids = n_cols ** 2
        self.n_blocks = self.n_grids * 2 // 3
        self.random, self.seed = seeding.np_random(seed)
        self.columns = None
        self.constraints = None
        self.last = None
        self.t = None
        self.int_to_tuple = list(itertools.permutations(range(self.n_cols), 2))
        self.action_space = gym.spaces.Discrete(len(self.int_to_tuple))
        self.observation_space = gym.spaces.MultiDiscrete(
            # TODO: np.array([max(self.n_blocks + 1, 4)] * self.n_rows * self.n_cols + [2])
            np.array(
                [max(self.n_blocks + 1, 4)]
                * (self.n_rows * self.n_cols + 3 * self.n_constraints)
                + [2]
            )
        )

    def valid(self, _from, _to, columns=None):
        if columns is None:
            columns = self.columns
        return columns[_from] and len(columns[_to]) < self.n_rows

    def step(self, action: int):
        self.t += 1
        if self.t <= self.n_constraints:
            return self.get_observation(), 0, False, {}
        _from, _to = self.int_to_tuple[int(action)]
        if self.valid(_from, _to):
            self.columns[_to].append(self.columns[_from].pop())
        if all(c.satisfied(self.columns) for c in self.constraints):
            r = 1
            t = True
        else:
            r = 0
            t = False
        self.last = Last(action=(_from, _to), reward=r, terminal=t, go=0)
        return self.get_observation(), r, t, {}

    def reset(self):
        self.last = None
        self.t = 0
        self.columns = [[] for _ in range(self.n_cols)]
        blocks = list(range(1, self.n_blocks + 1))
        self.random.shuffle(blocks)
        for block in blocks:
            self.random.shuffle(self.columns)
            column = next(c for c in self.columns if len(c) < self.n_rows)
            column.append(block)
        final_state = self.search_ahead([], self.columns, self.search_distance)

        def generate_constraints():
            for column in final_state:
                for bottom, top in zip(column, column[1:]):
                    yield from [Above(top, bottom), Below(top, bottom)]
            for row in itertools.zip_longest(*final_state):
                for left, right in zip(row, row[1:]):
                    if None not in (left, right):
                        yield from [Left(left, right), Right(left, right)]

        self.constraints = [
            c for c in generate_constraints() if not c.satisfied(self.columns)
        ]
        self.random.shuffle(self.constraints)
        self.constraints = self.constraints[: self.n_constraints]
        return self.get_observation()

    def search_ahead(self, trajectory, columns, n_steps):
        if n_steps == 0:
            return columns
        trajectory = trajectory + [tuple(map(tuple, columns))]
        actions = list(itertools.permutations(range(self.n_rows), 2))
        self.random.shuffle(actions)
        for _from, _to in actions:
            if self.valid(_from, _to, columns):
                columns = copy.deepcopy(columns)
                columns[_to].append(columns[_from].pop())
            if tuple(map(tuple, columns)) not in trajectory:
                future_state = self.search_ahead(trajectory, columns, n_steps - 1)
                if future_state is not None:
                    return future_state

    def get_observation(self):
        if self.t < self.n_constraints:
            state = [[0] * (self.n_rows * self.n_cols)]
        else:
            state = [c + [0] * (self.n_rows - len(c)) for c in self.columns]
        constraints = [c.list() for c in self.constraints]
        go = [[int(self.t >= self.n_constraints)]]
        obs = [x for r in state + constraints + go for x in r]
        assert self.observation_space.contains(obs)
        return obs

    def render(self, mode="human", pause=True):
        for row in reversed(list(itertools.zip_longest(*self.columns))):
            for x in row:
                print("{:3}".format(x or " "), end="")
            print()
        for constraint in self.constraints:
            print(
                "{:3}".format("✔︎") if constraint.satisfied(self.columns) else "  ",
                end="",
            )
            print(str(constraint))
        print(self.last)
        if pause:
            input("pause")


if __name__ == "__main__":
    import argparse
    from rl_utils import hierarchical_parse_args
    from ppo import keyboard_control

    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", default=0, type=int)
    parser.add_argument("--n-cols", default=3, type=int)
    parser.add_argument("--n-constraints", default=4, type=int)
    parser.add_argument("--time-limit", default=8, type=int)
    args = hierarchical_parse_args(parser)
    int_to_tuple = list(itertools.permutations(range(args["n_cols"]), 2))

    def action_fn(string):
        try:
            _from, _to = string
            index = int_to_tuple.index((int(_from), int(_to)))
            return index
        except ValueError:
            return

    keyboard_control.run(Env(**args), action_fn=action_fn)