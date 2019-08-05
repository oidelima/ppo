import torch
from gym.spaces import Box, Discrete
from torch import nn as nn

from ppo.agent import AgentValues
from ppo.distributions import Categorical
from ppo.distributions import DiagGaussian
from ppo.layers import Flatten
from ppo.utils import init_


# noinspection PyMissingConstructor


class DebugAgent(nn.Module):
    def __init__(
        self,
        obs_shape,
        action_space,
        recurrent,
        hidden_size,
        entropy_coef,
        **network_args,
    ):
        super().__init__()
        self.entropy_coef = entropy_coef
        self.base = Recurrence(
            *obs_shape, recurrent=recurrent, hidden_size=hidden_size, **network_args
        )

        if isinstance(action_space, Discrete):
            num_outputs = action_space.n
            self.dist = Categorical(self.base.output_size, num_outputs)
        elif isinstance(action_space, Box):
            num_outputs = action_space.shape[0]
            self.dist = DiagGaussian(self.base.output_size, num_outputs)
        else:
            raise NotImplementedError
        self.continuous = isinstance(action_space, Box)

    @property
    def recurrent_hidden_state_size(self):
        return self.base.recurrent_hidden_state_size

    @property
    def is_recurrent(self):
        return self.base.is_recurrent

    def forward(self, inputs, rnn_hxs, masks, deterministic=False, action=None):
        value, actor_features, rnn_hxs = self.base(inputs, rnn_hxs, masks)

        dist = self.dist(actor_features)

        if action is None:
            if deterministic:
                action = dist.mode()
            else:
                action = dist.sample()

        action_log_probs = dist.log_probs(action)
        entropy = dist.entropy().mean()
        return AgentValues(
            value=value,
            action=action,
            action_log_probs=action_log_probs,
            aux_loss=-self.entropy_coef * entropy,
            dist=dist,
            rnn_hxs=rnn_hxs,
            log=dict(entropy=entropy),
        )

    def get_value(self, inputs, rnn_hxs, masks):
        value, _, _ = self.base(inputs, rnn_hxs, masks)
        return value


class Recurrence(nn.Module):
    def _forward_gru(self, x, hxs, masks):
        if x.size(0) == hxs.size(0):
            x, hxs = self.recurrent_module(x.unsqueeze(0), (hxs * masks).unsqueeze(0))
            x = x.squeeze(0)
            hxs = hxs.squeeze(0)
        else:
            # x is a (T, N, -1) tensor that has been flatten to (T * N, -1)
            N = hxs.size(0)
            T = int(x.size(0) / N)

            # unflatten
            x = x.view(T, N, *x.shape[1:])

            # Same deal with masks
            masks = masks.view(T, N)

            # Let's figure out which steps in the sequence have a zero for any agent
            # We will always assume t=0 has a zero in it as that makes the logic cleaner
            has_zeros = (masks[1:] == 0.0).any(dim=-1).nonzero().squeeze().cpu()

            # +1 to correct the masks[1:]
            if has_zeros.dim() == 0:
                # Deal with scalar
                has_zeros = [has_zeros.item() + 1]
            else:
                has_zeros = (has_zeros + 1).numpy().tolist()

            # add t=0 and t=T to the list
            has_zeros = [0] + has_zeros + [T]

            hxs = hxs.unsqueeze(0)
            outputs = []
            for i in range(len(has_zeros) - 1):
                # We can now process steps that don't have any zeros in masks together!
                # This is much faster
                start_idx = has_zeros[i]
                end_idx = has_zeros[i + 1]

                rnn_scores, hxs = self.recurrent_module(
                    x[start_idx:end_idx], hxs * masks[start_idx].view(1, -1, 1)
                )

                outputs.append(rnn_scores)

            # assert len(outputs) == T
            # x is a (T, N, -1) tensor
            x = torch.cat(outputs, dim=0)
            # flatten
            x = x.view(T * N, -1)
            hxs = hxs.squeeze(0)

        return x, hxs

    def __init__(self, d, h, w, activation, hidden_size, num_layers, recurrent=False):
        super().__init__()
        self._hidden_size = hidden_size
        self._recurrent = recurrent

        self.f = nn.Sequential(
            init_(nn.Conv2d(d, hidden_size, kernel_size=1)),
            activation,
            *[
                nn.Sequential(
                    init_(
                        nn.Conv2d(hidden_size, hidden_size, kernel_size=1), activation
                    ),
                    activation,
                )
                for _ in range(num_layers)
            ],
            activation,
            Flatten(),
            init_(nn.Linear(hidden_size * h * w, hidden_size)),
            activation,
        )
        self.critic = init_(nn.Linear(hidden_size, 1))
        self.train()

    def forward(self, inputs, rnn_hxs, masks):
        s = self.f(inputs)
        return self.critic(s), s, rnn_hxs

    @property
    def recurrent_hidden_state_size(self):
        """Size of rnn_hx."""
        return 1  # TODO

    @property
    def is_recurrent(self):
        return False  # TODO

    @property
    def output_size(self):
        return self._hidden_size
