# third party
import csv
from io import StringIO
import random
import subprocess

import numpy as np
import torch
from torch import nn as nn
import torch.jit
import torch.nn as nn


def round(x, dec):
    return torch.round(x * 10 ** dec) / 10 ** dec


def grad(x, y):
    return torch.autograd.grad(
        x.mean(), y.parameters() if isinstance(y, nn.Module) else y, retain_graph=True
    )


def get_render_func(venv):
    if hasattr(venv, "envs"):
        return venv.envs[0].render
    elif hasattr(venv, "venv"):
        return get_render_func(venv.venv)
    elif hasattr(venv, "env"):
        return get_render_func(venv.env)

    return None


# Necessary for my KFAC implementation.
class AddBias(nn.Module):
    def __init__(self, bias):
        super(AddBias, self).__init__()
        self._bias = nn.Parameter(bias.unsqueeze(1))

    def forward(self, x):
        if x.dim() == 2:
            bias = self._bias.t().view(1, -1)
        else:
            bias = self._bias.t().view(1, -1, 1, 1)

        return x + bias


def init(module, weight_init, bias_init, gain=1):
    weight_init(module.weight.data, gain=gain)
    bias_init(module.bias.data)
    return module


# https://github.com/openai/baselines/blob/master/baselines/common/tf_util.py#L87
def init_normc_(weight, gain=1):
    weight.normal_(0, 1)
    weight *= gain / torch.sqrt(weight.pow(2).sum(1, keepdim=True))


def set_index(array, idxs, value):
    idxs = np.array(idxs)
    if idxs.size > 0:
        array[tuple(idxs.T)] = value


def get_index(array, idxs):
    idxs = np.array(idxs)
    if idxs.size == 0:
        return np.array([], array.dtype)
    return array[tuple(idxs.T)]


def get_n_gpu():
    nvidia_smi = subprocess.check_output(
        "nvidia-smi --format=csv --query-gpu=memory.free".split(),
        universal_newlines=True,
    )
    return len(list(csv.reader(StringIO(nvidia_smi)))) - 1


def get_random_gpu():
    return random.randrange(0, get_n_gpu())


def get_freer_gpu():
    nvidia_smi = subprocess.check_output(
        "nvidia-smi --format=csv --query-gpu=memory.free".split(),
        universal_newlines=True,
    )
    free_memory = [
        float(x[0].split()[0])
        for i, x in enumerate(csv.reader(StringIO(nvidia_smi)))
        if i > 0
    ]
    return int(np.argmax(free_memory))


def init_(network, nonlinearity=nn.ReLU):
    nonlinearity_str = {
        nn.Linear: "linear",
        nn.Conv1d: "conv1d",
        nn.Conv2d: "conv2d",
        nn.Conv3d: "conv3d",
        nn.ConvTranspose1d: "conv_transpose1d",
        nn.ConvTranspose2d: "conv_transpose2d",
        nn.ConvTranspose3d: "conv_transpose3d",
        nn.Sigmoid: "sigmoid",
        nn.Tanh: "tanh",
        nn.ReLU: "relu",
        nn.LeakyReLU: "leaky_relu",
    }.get(nonlinearity.__class__, "linear")

    if nonlinearity is None:
        return init(network, init_normc_, lambda x: nn.init.constant_(x, 0))
        # return init(network, nn.init.orthogonal_, lambda x: nn.init.constant_(x, 0))
    return init(
        network,
        nn.init.orthogonal_,
        lambda x: nn.init.constant_(x, 0),
        nn.init.calculate_gain(nonlinearity_str),
    )


def broadcast3d(inputs, shape):
    return inputs.view(*inputs.shape, 1, 1).expand(*inputs.shape, *shape)


def interp(x1, x2, c):
    return c * x2 + (1 - c) * x1


@torch.jit.script
def log_prob(i, probs):
    return torch.log(torch.gather(probs, -1, i))


def trace(module_fn, in_size):
    return torch.jit.trace(module_fn(in_size), example_inputs=torch.rand(1, in_size))


RED = "\033[1;31m"
BLUE = "\033[1;34m"
CYAN = "\033[1;36m"
GREEN = "\033[0;32m"
RESET = "\033[0;0m"
BOLD = "\033[;1m"
REVERSE = "\033[;7m"


def k_scalar_pairs(*args, **kwargs):
    for k, v in dict(*args, **kwargs).items():
        mean = np.mean(v)
        if not np.isnan(mean):
            yield k, mean
