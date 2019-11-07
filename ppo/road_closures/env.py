import itertools
from collections import namedtuple, deque, defaultdict, OrderedDict

import gym
import numpy as np
from gym.utils import seeding
from rl_utils import hierarchical_parse_args

from ppo import keyboard_control
from ppo.utils import RED, RESET

Obs = namedtuple("Obs", "roads open")
Last = namedtuple("Last", "state action reward terminal")


class Env(gym.Env):
    def __init__(self, seed, n_states, baseline, flip_prob, time_limit):
        self.time_limit = time_limit
        self.flip_prob = flip_prob
        self.baseline = baseline
        assert n_states % 2 == 0
        self.n_states = n_states
        self.random, self.seed = seeding.np_random(seed)
        self.transitions = None
        self.state = None
        self.goal = None
        self.open = None
        self.path = None
        self.last = None
        self.t = None
        self.eye = np.eye(n_states)
        if baseline:
            self.action_space = gym.spaces.Discrete(n_states)
            self.observation_space = gym.spaces.MultiBinary(n_states ** 2 + n_states)
        else:
            self.action_space = gym.spaces.MultiDiscrete(n_states * np.ones(2))
            self.observation_space = gym.spaces.Dict(
                Obs(
                    roads=gym.spaces.MultiDiscrete(2 * np.ones((n_states, n_states))),
                    open=gym.spaces.MultiBinary(n_states),
                    # goal=gym.spaces.Discrete(n_states),
                )._asdict()
            )

    def step(self, action):
        if not self.baseline:
            action = int(action[0])
        self.t += 1
        new_state = int(action)
        open_road = ((self.eye[self.state] @ self.transitions) * self.open)[new_state]
        if not open_road or self.time_limit and self.t > self.time_limit:
            self.last = Last(
                state=self.state, action=action, reward=-self.time_limit, terminal=True
            )
            return self.get_observation(), -self.time_limit, True, {}
        self.state = new_state
        flip = self.random.binomial(n=1, size=1, p=self.flip_prob)
        if flip:
            self.open = 1 - self.open
            self.open[self.state] = 1
            self.open[self.goal] = 1
        t = self.state == self.goal
        self.last = Last(state=self.state, action=action, reward=-1, terminal=t)
        return self.get_observation(), -1, t, {}

    def reset(self):
        self.t = 0
        self.last = None
        i, j = np.indices((self.n_states, self.n_states))
        lattice = np.roll(np.abs(i - j) < 2, 1)
        indices = np.arange(1, self.n_states - 1)
        self.random.shuffle(indices)
        indices = np.concatenate([[0], indices, [self.n_states - 1]])
        self.transitions = lattice[indices][:, indices]
        self.open = np.arange(self.n_states)[indices] % 2 == self.random.choice(2)
        self.state = np.argmin(indices)
        self.goal = np.argmax(indices)
        self.open[self.state] = 1
        self.open[self.goal] = 1
        return self.get_observation()

    @staticmethod
    def floyd_warshall(graph):
        paths = np.empty_like(graph, object)
        for i in range(len(graph)):
            for j in range(len(graph)):
                paths[i, j] = [j] if graph[i, j] else []

        distances = graph.astype(float)
        distances[graph == 0] = np.inf
        for k in range(len(graph)):
            distance_through_k = distances[:, k : k + 1] + distances[k : k + 1]
            update = distance_through_k < distances
            distances[update] = distance_through_k[update]
            paths[update] = (paths[:, k : k + 1] + paths[k : k + 1])[update]
        return paths, distances

    def choose_path(self):
        paths, distances = self.floyd_warshall(self.transitions)  # all shortest paths
        distances[np.isinf(distances)] *= -1
        i, j = max(
            itertools.product(range(self.n_states), range(self.n_states)),
            key=lambda t: distances[t],
        )
        path = [i] + paths[i, j]
        return path

    def get_observation(self):
        obs = Obs(roads=self.transitions, open=self.open)
        if self.baseline:
            obs = np.concatenate([o.flatten() for o in obs])
        else:
            obs = obs._asdict()
        assert self.observation_space.contains(obs)
        return obs

    def render(self, pause=True, mode="human"):
        print("optimal path:", self.path)
        print(self.last)
        print(
            " ", *["v" if i == self.goal else " " for i in range(self.n_states)], sep=""
        )
        for i, row in enumerate(self.transitions):
            print(">" if i == self.state else " ", end="")
            for x, open_road in zip(row, self.open):
                print(RESET if open_road else RED, int(x), sep="", end="")
            print(RESET)
        if pause:
            input("pause")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", default=0, type=int)
    parser.add_argument("--n-states", default=6, type=int)
    parser.add_argument("--time-limit", default=100, type=int)
    parser.add_argument("--flip-prob", default=0.5, type=float)
    args = hierarchical_parse_args(parser)
    keyboard_control.run(Env(**args, baseline=False), action_fn=lambda s: (int(s), 0))