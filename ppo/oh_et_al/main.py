from gym.wrappers import TimeLimit
from rl_utils import hierarchical_parse_args

import ppo.arguments
from ppo import gntm, oh_et_al
import ppo.train


def build_parser():
    parsers = ppo.arguments.build_parser()
    parser = parsers.main
    parser.add_argument("--no-tqdm", dest="use_tqdm", action="store_false")
    parsers.agent.add_argument("--debug", action="store_true")
    return parsers


def train(increment_curriculum_at, **_kwargs):
    class Train(ppo.train.Train):
        @staticmethod
        def make_env(
            seed, rank, evaluation, env_id, add_timestep, time_limit, **env_args
        ):
            return oh_et_al.Env(**env_args, seed=seed + rank)

        def build_agent(self, envs, recurrent=None, entropy_coef=None, **agent_args):
            recurrence = oh_et_al.Recurrence(
                action_space=envs.action_space,
                observation_space=envs.observation_space,
                **agent_args,
            )
            return oh_et_al.Agent(entropy_coef=entropy_coef, recurrence=recurrence)

        def run_epoch(self, *args, **kwargs):
            dictionary = super().run_epoch(*args, **kwargs)
            rewards = dictionary["rewards"]
            if (
                increment_curriculum_at
                and rewards
                and sum(rewards) / len(rewards) > increment_curriculum_at
            ):
                self.envs.increment_curriculum()
            return dictionary

    Train(**_kwargs, time_limit=None).run()


def cli():
    parsers = build_parser()
    parsers.main.add_argument("--increment-curriculum-at", type=float)
    parsers.env.add_argument("--height", type=int, default=4)
    parsers.env.add_argument("--width", type=int, default=4)
    parsers.env.add_argument("--min-subtasks", type=int, default=2)
    parsers.env.add_argument("--max-subtasks", type=int, default=20)
    parsers.env.add_argument("--implement-lower-level", action="store_true")
    args = hierarchical_parse_args(parsers.main)
    train(**args)


if __name__ == "__main__":
    cli()