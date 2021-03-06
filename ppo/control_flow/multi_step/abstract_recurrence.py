import numpy as np
import torch
import torch.nn.functional as F
from gym import spaces
from torch import nn as nn

from ppo.control_flow.multi_step.env import Obs
from ppo.utils import init_


class Recurrence:
    def __init__(self):
        d, h, _ = self.obs_spaces.obs.shape
        ones = torch.ones(1, dtype=torch.long)
        self.register_buffer("ones", ones)
        line_nvec = torch.tensor(self.obs_spaces.lines.nvec[0, :-1])
        offset = F.pad(line_nvec.cumsum(0), [1, 0])
        self.register_buffer("offset", offset)

    @property
    def gru_in_size(self):
        return self.hidden_size + self.conv_hidden_size + self.encoder_hidden_size

    @staticmethod
    def eval_lines_space(n_eval_lines, train_lines_space):
        return spaces.MultiDiscrete(
            np.repeat(train_lines_space.nvec[:1], repeats=n_eval_lines, axis=0)
        )

    def build_embed_task(self, hidden_size):
        return nn.EmbeddingBag(self.obs_spaces.lines.nvec[0].sum(), hidden_size)

    def preprocess_embed(self, N, T, inputs):
        lines = inputs.lines.view(T, N, *self.obs_spaces.lines.shape)
        lines = lines.long()[0, :, :] + self.offset
        return lines.view(-1, self.obs_spaces.lines.nvec[0].size)
