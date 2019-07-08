import gym
from gym import spaces
import numpy as np

import ppo.subtasks
from ppo.subtasks import Actions


class DebugWrapper(ppo.subtasks.DebugWrapper):
    def __init__(self, env):
        super().__init__(env)
        self.completed_subtask = False

    def reset(self):
        self.completed_subtask = False
        return super().reset()

    def step(self, action):
        actions = Actions(*[x.item() for x in np.split(action, self.action_sections)])
        env = self.env.unwrapped
        self.truth = int(env.subtask_idx)

        def lines_to_subtasks():
            i = 0
            for line in env.lines:
                if line[-1] == 0:
                    yield i
                    i += 1
                else:
                    yield None

        line = self.guess = int(actions.g)
        subtask = list(lines_to_subtasks())[line]
        r = 0
        print("subtask", subtask)
        print("line", line)
        print("self.guess", self.guess)
        print("subtask", subtask)
        print("self.truth", self.truth)

        if (subtask is not None and subtask != self.truth) or (
            subtask is None and not self.completed_subtask
        ):  # wrong subtask line
            # import ipdb

            # ipdb.set_trace()
            r = -0.1
        print("reward", r)
        subtask_before = env.subtask_idx
        s, _, t, i = gym.Wrapper.step(self, action)
        subtask_after = env.subtask_idx
        self.completed_subtask = subtask_before != subtask_after
        self.last_reward = r
        return s, r, t, i


class Wrapper(ppo.subtasks.Wrapper):
    def __init__(self, env):
        super().__init__(env)
        self.action_space.spaces.update(
            g=spaces.Discrete(len(env.observation_space.spaces["lines"].nvec))
        )

    def render_assigned_subtask(self):
        env = self.env.unwrapped
        g_type, g_count, g_obj, condition = tuple(env.lines[self.last_g])
        if condition:
            print("if", env.object_types[condition - 1])
        else:
            print(
                f"{self.last_g}:",
                env.interactions[g_type],
                g_count + 1,
                env.object_types[g_obj],
            )
