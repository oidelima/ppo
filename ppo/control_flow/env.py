from abc import ABC
from collections import defaultdict, namedtuple, OrderedDict

import numpy as np

# import skimage.draw
from gym.utils import seeding
from gym.vector.utils import spaces
from rl_utils import hierarchical_parse_args, gym

from ppo import keyboard_control
from ppo.control_flow.lines import If, Else, EndIf, While, EndWhile, Subtask, Padding

Obs = namedtuple("Obs", "active lines obs")
Last = namedtuple("Last", "action active reward terminal selected")
State = namedtuple("State", "obs condition done")


class Env(gym.Env, ABC):
    pairs = {If: EndIf, Else: EndIf, While: EndWhile}
    line_types = [If, Else, EndIf, While, EndWhile, Subtask, Padding]

    def __init__(
        self,
        min_lines,
        max_lines,
        flip_prob,
        terminate_on_failure,
        num_subtasks,
        max_nesting_depth,
        eval_condition_size,
        no_op_limit,
        seed=0,
        eval_lines=None,
        time_limit=100,
        evaluating=False,
        baseline=False,
    ):
        super().__init__()
        self.no_op_limit = no_op_limit
        self.eval_condition_size = eval_condition_size
        self.max_nesting_depth = max_nesting_depth
        self.num_subtasks = num_subtasks
        self.terminate_on_failure = terminate_on_failure
        self.eval_lines = eval_lines
        self.min_lines = min_lines
        self.max_lines = max_lines
        if evaluating:
            self.n_lines = eval_lines
        else:
            self.n_lines = max_lines
        self.n_lines += 1
        self.random, self.seed = seeding.np_random(seed)
        self.time_limit = time_limit
        self.flip_prob = flip_prob
        self.baseline = baseline
        self.evaluating = evaluating
        self.iterator = None
        self._render = None
        if baseline:
            self.action_space = spaces.Discrete(self.num_subtasks + 1)
            n_line_types = len(self.line_types) + num_subtasks
            self.observation_space = spaces.Dict(
                dict(
                    obs=spaces.Discrete(2),
                    lines=spaces.MultiBinary(n_line_types * self.n_lines),
                )
            )
            self.eye = np.eye(n_line_types)
        else:
            self.action_space = spaces.MultiDiscrete(
                np.array([self.num_subtasks + 1, 2 * self.n_lines])
            )
            self.observation_space = spaces.Dict(
                dict(
                    obs=spaces.Discrete(2),
                    lines=spaces.MultiDiscrete(
                        np.array([len(self.line_types) + num_subtasks] * self.n_lines)
                    ),
                    active=spaces.Discrete(self.n_lines + 1),
                )
            )

    def reset(self):
        self.iterator = self.generator()
        s, r, t, i = next(self.iterator)
        return s

    def step(self, action):
        return self.iterator.send(action)

    def generator(self):
        failing = False
        step = 0
        n = 0
        eval_condition_size = self.eval_condition_size and self.evaluating
        condition_bit = 0 if eval_condition_size else self.random.randint(0, 2)
        lines = self.build_lines(eval_condition_size)
        line_iterator = self.line_generator(lines)

        def next_subtask(msg=condition_bit):
            a = line_iterator.send(msg)
            while not (a is None or type(lines[a]) is Subtask):
                a = line_iterator.send(condition_bit)
            return a

        selected = 0
        prev, active = 0, next_subtask(None)
        if active is None:
            yield from self.generator()
        i = {}
        t = False
        while True:

            def line_strings(index, level):
                if index == len(lines):
                    return
                line = lines[index]
                if line in [Else, EndIf, EndWhile]:
                    level -= 1
                if index == active and index == selected:
                    pre = "+ "
                elif index == selected:
                    pre = "- "
                elif index == active:
                    pre = "| "
                else:
                    pre = "  "
                indent = pre * level
                if type(line) is Subtask:
                    yield f"{indent}Subtask {line.id}"
                else:
                    yield f"{indent}{line.__name__}"
                if line in [If, While, Else]:
                    level += 1
                yield from line_strings(index + 1, level)

            def render():
                for i, string in enumerate(line_strings(index=0, level=1)):
                    print(f"{i}{string}")
                print("Condition:", condition_bit)
                print("Failing:", failing)

            self._render = render

            success = active is None
            r = int(t) * int(not failing)
            if success:
                i.update(success_line=len(lines))

            action = yield self.get_observation(condition_bit, active, lines), r, t, i
            t = success or (not self.evaluating and step == self.time_limit)

            if self.baseline:
                selected = None
            else:
                action, delta = action
                selected = (selected + delta - self.n_lines) % self.n_lines
            i = self.get_task_info(lines) if step == 0 else {}

            if action == self.num_subtasks:
                n += 1
                if (not self.evaluating) and self.no_op_limit and n == self.no_op_limit:
                    failing = True
            elif active is not None:
                if action != lines[active].id:
                    failing = True
                    i.update(sucess_line=prev, failure_line=active)
                step += 1
                condition_bit = abs(
                    condition_bit - int(self.random.rand() < self.flip_prob)
                )
                prev, active = active, next_subtask()

    def build_lines(self, eval_condition_size):
        if self.evaluating:
            assert self.eval_lines is not None
            n_lines = self.eval_lines
        else:
            n_lines = self.random.random_integers(self.min_lines, self.max_lines)
        if eval_condition_size:
            line0 = self.random.choice([While, If])
            edge_length = self.random.random_integers(
                self.max_lines, self.eval_lines - 1
            )
            lines = [line0] + [Subtask] * (edge_length - 2)
            lines += [EndWhile if line0 is While else EndIf, Subtask]
        else:
            lines = self.get_lines(
                n_lines, active_conditions=[], max_nesting_depth=self.max_nesting_depth
            )
        lines = [
            Subtask(self.random.choice(self.num_subtasks)) if line is Subtask else line
            for line in lines
        ]
        return lines

    def get_lines(
        self, n, active_conditions, last=None, nesting_depth=0, max_nesting_depth=None
    ):
        if n < 0:
            return []
        if n == 0:
            return []
        if n == len(active_conditions):
            lines = [self.pairs[c] for c in reversed(active_conditions)]
            return lines + [Subtask for _ in range(n - len(lines))]
        elif n == 1:
            return [Subtask]
        line_types = [Subtask]
        if n > len(active_conditions) + 2 and (
            max_nesting_depth is None or nesting_depth < max_nesting_depth
        ):
            line_types += [If, While]
        if active_conditions and last is Subtask:
            last_condition = active_conditions[-1]
            if last_condition is If:
                line_types += [Else, EndIf]
            elif last_condition is Else:
                line_types += [EndIf]
            elif last_condition is While:
                line_types += [EndWhile]
        line_type = self.random.choice(line_types)
        if line_type in [If, While]:
            active_conditions = active_conditions + [line_type]
            nesting_depth += 1
        elif line_type is Else:
            active_conditions = active_conditions[:-1] + [line_type]
        elif line_type in [EndIf, EndWhile]:
            active_conditions = active_conditions[:-1]
            nesting_depth -= 1
        get_lines = self.get_lines(
            n - 1,
            active_conditions=active_conditions,
            last=line_type,
            nesting_depth=nesting_depth,
            max_nesting_depth=max_nesting_depth,
        )
        return [line_type] + get_lines

    def line_generator(self, lines):
        line_transitions = defaultdict(list)
        for _from, _to in self.get_transitions(iter(enumerate(lines)), []):
            line_transitions[_from].append(_to)
        i = 0
        if_evaluations = []
        while True:
            condition_bit = yield (None if i >= len(lines) else i)
            if lines[i] is Else:
                evaluation = not if_evaluations.pop()
            else:
                evaluation = bool(condition_bit)
            if lines[i] is If:
                if_evaluations.append(evaluation)
            i = line_transitions[i][evaluation]

    def get_transitions(self, lines_iter, previous):
        while True:  # stops at StopIteration
            try:
                current, line = next(lines_iter)
            except StopIteration:
                return
            if line is EndIf or type(line) is Subtask:
                yield current, current + 1  # False
                yield current, current + 1  # True
            if line is If:
                yield from self.get_transitions(
                    lines_iter, previous + [current]
                )  # from = If
            elif line is Else:
                prev = previous[-1]
                yield prev, current  # False: If -> Else
                yield prev, prev + 1  # True: If -> If + 1
                previous[-1] = current
            elif line is EndIf:
                prev = previous[-1]
                yield prev, current  # False: If/Else -> EndIf
                yield prev, prev + 1  # True: If/Else -> If/Else + 1
                return
            elif line is While:
                yield from self.get_transitions(
                    lines_iter, previous + [current]
                )  # from = While
            elif line is EndWhile:
                prev = previous[-1]
                # While
                yield prev, current + 1  # False: While -> EndWhile + 1
                yield prev, prev + 1  # True: While -> While + 1
                # EndWhile
                yield current, prev  # False: EndWhile -> While
                yield current, prev  # True: EndWhile -> While
                return

    def get_observation(self, condition_bit, active, lines):
        padded = lines + [Padding] * (self.n_lines - len(lines))
        lines = [
            t.id if type(t) is Subtask else self.num_subtasks + self.line_types.index(t)
            for t in padded
        ]
        obs = Obs(
            obs=condition_bit,
            lines=lines,
            active=self.n_lines if active is None else active,
        )
        if self.baseline:
            obs = OrderedDict(obs=obs.obs, lines=self.eye[obs.lines].flatten())
        else:
            obs = obs._asdict()
        if not self.evaluating:
            assert self.observation_space.contains(obs)
        return obs

    def get_task_info(self, lines):
        num_if = lines.count(If)
        num_else = lines.count(Else)
        num_while = lines.count(While)
        num_subtask = lines.count(lambda l: type(l) is Subtask)
        i = dict(
            if_lines=num_if,
            else_lines=num_else,
            while_lines=num_while,
            nesting_depth=self.get_nesting_depth(lines),
            num_edges=2 * (num_if + num_else + num_while) + num_subtask,
        )
        keys = {
            (If, EndIf): "if clause length",
            (If, Else): "if-else clause length",
            (Else, EndIf): "else clause length",
            (While, EndWhile): "while clause length",
        }
        for k, v in self.average_interval(lines):
            i[keys[k]] = v
        return i

    @staticmethod
    def average_interval(lines):
        intervals = defaultdict(lambda: [None])
        pairs = [(If, EndIf), (While, EndWhile)]
        if Else in lines:
            pairs.extend([(If, Else), (Else, EndIf)])
        for line in lines:
            for start, stop in pairs:
                if line is start:
                    intervals[start, stop][-1] = 0
                if line is stop:
                    intervals[start, stop].append(None)
            for k, (*_, value) in intervals.items():
                if value is not None:
                    intervals[k][-1] += 1
        for keys, values in intervals.items():
            values = [v for v in values if v]
            if values:
                yield keys, sum(values) / len(values)

    @staticmethod
    def get_nesting_depth(lines):
        max_depth = 0
        depth = 0
        for line in lines:
            if line in [If, While]:
                depth += 1
            if line in [EndIf, EndWhile]:
                depth -= 1
            max_depth = max(depth, max_depth)
        return max_depth

    def seed(self, seed=None):
        assert self.seed == seed

    def render(self, mode="human", pause=True):
        self._render()
        if pause:
            input("pause")


def build_parser(p):
    p.add_argument("--min-lines", type=int, required=True)
    p.add_argument("--max-lines", type=int, required=True)
    p.add_argument("--num-subtasks", type=int, default=12)
    p.add_argument("--no-op-limit", type=int)
    p.add_argument("--flip-prob", type=float, default=0.5)
    p.add_argument("--terminate-on-failure", action="store_true")
    p.add_argument("--eval-condition-size", action="store_true")
    p.add_argument("--max-nesting-depth", type=int)
    return p


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    args = hierarchical_parse_args(build_parser(parser))

    def action_fn(string):
        try:
            return int(string), 0
        except ValueError:
            return

    keyboard_control.run(Env(**args, baseline=False), action_fn=action_fn)
