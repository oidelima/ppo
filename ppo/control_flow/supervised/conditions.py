from __future__ import print_function

import numpy as np
import argparse
import csv
import itertools
import random
import subprocess
from pathlib import Path

import torch
import torch.nn.functional as F
import torch.optim as optim
from rl_utils import hierarchical_parse_args
from torch.utils.data import DataLoader, IterableDataset
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
import torch.nn as nn

from ppo.control_flow.lines import If
from ppo.control_flow.multi_step.env import Env
from ppo.layers import Flatten
from ppo.utils import init_
import ppo.control_flow.multi_step.env

MAX_LAYERS = 3

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO


def format_image(data, output, target):
    return torch.stack([data.sum(1)[0], target[0], output[0]], dim=0).unsqueeze(1)


class GridworldDataset(IterableDataset):
    def __init__(self, **kwargs):
        self.env = Env(rank=0, lower_level="pretrained", **kwargs)

    def __iter__(self):
        while True:
            agent_pos, objects = self.env.populate_world([])
            evaluation = self.env.evaluate_line(
                line=If,
                counts=self.env.count_objects(objects),
                condition_evaluations=[],
                loops=0,
            )
            yield self.env.world_array(objects, agent_pos), int(evaluation)

    # def __len__(self):
    #     pass


class Network(nn.Module):
    def __init__(
        self,
        d,
        h,
        w,
        conv_layers,
        conv_kernels,
        conv_strides,
        pool_type,
        pool_kernels,
        pool_strides,
    ):
        super().__init__()

        def remove_none(xs):
            return [x for x in xs if x is not None]

        conv_layers = remove_none(conv_layers)
        conv_kernels = remove_none(conv_kernels)
        conv_strides = remove_none(conv_strides)
        pool_kernels = remove_none(pool_kernels)
        pool_strides = remove_none(pool_strides)

        def generate_pools(k):
            for (kernel, stride) in zip(pool_kernels, pool_strides):
                kernel = min(k, kernel)
                padding = (kernel // 2) % stride
                if pool_type == "avg":
                    pool = nn.AvgPool2d(
                        kernel_size=kernel, stride=stride, padding=padding
                    )
                elif pool_type == "max":
                    pool = nn.MaxPool2d(
                        kernel_size=kernel, stride=stride, padding=padding
                    )
                else:
                    raise RuntimeError
                k = int((k + 2 * padding - kernel) / stride + 1)
                k = yield k, pool

        def generate_convolutions(k):
            in_size = d
            for (layer, kernel, stride) in zip(conv_layers, conv_kernels, conv_strides):
                kernel = min(k, kernel)
                padding = (kernel // 2) % stride
                conv = init_(
                    nn.Conv2d(
                        in_channels=in_size,
                        out_channels=layer,
                        kernel_size=kernel,
                        stride=stride,
                        padding=padding,
                    )
                )
                k = int((k + (2 * padding) - (kernel - 1) - 1) // stride + 1)
                k = yield k, conv
                in_size = layer

        def generate_modules(k):
            n_pools = min(len(pool_strides), len(pool_kernels))
            n_conv = min(len(conv_layers), len(conv_strides), len(conv_kernels))
            conv_iterator = generate_convolutions(k)
            try:
                k, conv = next(conv_iterator)
                yield conv
                pool_iterator = None
                for i in itertools.count():
                    if pool_iterator is None:
                        if i >= n_conv - n_pools and pool_type is not None:
                            pool_iterator = generate_pools(k)
                            k, pool = next(pool_iterator)
                            yield pool
                    else:
                        k, pool = pool_iterator.send(k)
                        yield pool
                    k, conv = conv_iterator.send(k)
                    yield conv
            except StopIteration:
                pass
            yield Flatten()
            yield init_(nn.Linear(k ** 2 * conv.out_channels, 1))
            yield nn.Sigmoid()

        self.net = nn.Sequential(*generate_modules(h))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


def main(
    no_cuda: bool,
    seed: int,
    batch_size: int,
    lr: float,
    log_dir: Path,
    run_id: str,
    env_args: dict,
    network_args: dict,
    log_interval: int,
    save_interval: int,
):
    use_cuda = not no_cuda and torch.cuda.is_available()
    writer = SummaryWriter(str(log_dir))

    torch.manual_seed(seed)

    if use_cuda:
        nvidia_smi = subprocess.check_output(
            "nvidia-smi --format=csv --query-gpu=memory.free".split(),
            universal_newlines=True,
        )
        n_gpu = len(list(csv.reader(StringIO(nvidia_smi)))) - 1
        try:
            index = int(run_id[-1])
        except (ValueError, IndexError):
            index = random.randrange(0, n_gpu)
        print("Using GPU", index)
        device = torch.device("cuda", index=index % n_gpu)
    else:
        device = "cpu"

    dataset = GridworldDataset(**env_args)
    obs_shape = dataset.env.observation_space.spaces["obs"].shape
    network = Network(*obs_shape, **network_args)
    network = network.to(device)
    optimizer = optim.Adam(network.parameters(), lr=lr)
    network.train()
    start = 0

    train_loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        **(dict(num_workers=1, pin_memory=True) if use_cuda else dict()),
    )
    total_loss = 0
    log_progress = None
    for i, (data, target) in enumerate(train_loader):
        data, target = data.to(device).float(), target.to(device).float()
        optimizer.zero_grad()
        output = network(data).flatten()
        loss = F.binary_cross_entropy(output, target, reduction="mean")
        total_loss += loss
        loss.backward()
        avg_loss = total_loss / i
        optimizer.step()
        step = i + start
        if i % log_interval == 0:
            log_progress = tqdm(total=log_interval, desc="next log")
            writer.add_scalar("loss", loss, step)
            writer.add_scalar("avg_loss", avg_loss, step)

        if i % save_interval == 0:
            torch.save(network.state_dict(), str(Path(log_dir, "network.pt")))
        log_progress.update()


def maybe_int(string):
    if string == "None":
        return None
    return int(string)


def cli():
    # Training settings
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        metavar="N",
        help="input batch size for training",
    )
    parser.add_argument(
        "--lr", type=float, default=0.01, metavar="LR", help="learning rate "
    )
    parser.add_argument(
        "--no-cuda", action="store_true", default=False, help="disables CUDA training"
    )
    parser.add_argument("--seed", type=int, default=0, metavar="S", help="random seed ")
    parser.add_argument(
        "--save-interval",
        type=int,
        default=100,
        metavar="N",
        help="how many batches to wait before logging training status",
    )
    parser.add_argument(
        "--log-interval",
        type=int,
        default=10,
        metavar="N",
        help="how many batches to wait before logging training status",
    )
    parser.add_argument("--log-dir", default="/tmp/mnist", metavar="N", help="")
    parser.add_argument("--run-id", default="", metavar="N", help="")
    env_parser = parser.add_argument_group("env_args")
    ppo.control_flow.multi_step.env.build_parser(
        env_parser,
        default_max_while_loops=1,
        default_max_world_resamples=0,
        default_min_lines=1,
        default_max_lines=1,
        default_time_to_waste=0,
    )
    network_parser = parser.add_argument_group("network_args")
    network_parser.add_argument(f"--pool-type", choices=("avg", "max", "None"))
    for i in range(MAX_LAYERS):
        network_parser.add_argument(
            f"--conv-layer{i}", dest="conv_layers", action="append", type=maybe_int
        )
        for mod in ("conv", "pool"):
            for component in ("kernel", "stride"):
                network_parser.add_argument(
                    f"--{mod}-{component}{i}",
                    dest=f"{mod}_{component}s",
                    action="append",
                    type=maybe_int,
                )
    main(**hierarchical_parse_args(parser))


if __name__ == "__main__":
    cli()
