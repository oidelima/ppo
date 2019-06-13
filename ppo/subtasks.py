from collections import namedtuple

from gym import spaces
from gym.spaces import Box, Discrete
import numpy as np
import torch
from torch import nn as nn
import torch.jit
from torch.nn import functional as F

from ppo.agent import Agent, AgentValues, NNBase
from ppo.distributions import Categorical, DiagGaussian, FixedCategorical
from ppo.layers import Broadcast3d, Concat, Flatten, Reshape
from ppo.teacher import SubtasksTeacher
from ppo.utils import batch_conv1d, broadcast_3d, init_, interp, trace
from ppo.wrappers import SubtasksActions, get_subtasks_action_sections, get_subtasks_obs_sections

RecurrentState = namedtuple(
    'RecurrentState',
    'p r h b b_probs g g_probs c c_probs l l_probs a a_probs v c_truth '
    'c_loss l_loss p_loss r_loss g_loss b_loss subtask')


# noinspection PyMissingConstructor
class SubtasksAgent(Agent, NNBase):
    def __init__(self,
                 obs_shape,
                 action_space,
                 task_space,
                 hidden_size,
                 entropy_coef,
                 alpha,
                 zeta,
                 hard_update,
                 teacher_agent=None,
                 **kwargs):
        nn.Module.__init__(self)
        self.zeta = zeta
        self.alpha = alpha
        self.hard_update = hard_update
        if teacher_agent:
            assert isinstance(teacher_agent, SubtasksTeacher)
        self.teacher_agent = teacher_agent
        self.entropy_coef = entropy_coef
        self.action_space = SubtasksActions(*action_space.spaces)
        self.recurrent_module = SubtasksRecurrence(
            obs_shape=obs_shape,
            action_space=self.action_space,
            task_space=task_space,
            hidden_size=hidden_size,
            hard_update=hard_update,
            **kwargs,
        )
        self.obs_sections = get_subtasks_obs_sections(task_space)

    def forward(self, inputs, rnn_hxs, masks, action=None,
                deterministic=False):
        obs, g_target, task, next_subtask = torch.split(
            inputs, self.obs_sections, dim=1)


        n = inputs.shape[0]
        all_hxs, last_hx = self._forward_gru(
            inputs.view(n, -1), rnn_hxs, masks)
        rm = self.recurrent_module
        hx = RecurrentState(*rm.parse_hidden(all_hxs))

        # print('g       ', hx.g[0])
        # print('g_target', g_target[0, :, 0, 0])

        if self.hard_update:
            dists = SubtasksActions(
                a=FixedCategorical(hx.a_probs),
                b=FixedCategorical(hx.b_probs),
                c=FixedCategorical(hx.c_probs),
                g=FixedCategorical(hx.g_probs),
                l=FixedCategorical(hx.l_probs),
            )
        else:
            dists = SubtasksActions(
                a=FixedCategorical(hx.a_probs),
                b=FixedCategorical(hx.b_probs),
                c=None,
                g=FixedCategorical(hx.g_probs),
                l=None,
            )

        if action is None:
            actions = SubtasksActions(a=hx.a, b=hx.b, g=hx.g, l=hx.l, c=hx.c)
        else:
            action_sections = get_subtasks_action_sections(self.action_space)
            actions = SubtasksActions(
                *torch.split(action, action_sections, dim=-1))

        log_probs = sum(dist.log_probs(a) for dist, a in zip(dists, actions)
                        if dist is not None)
        entropies = sum(dists.entropy() for dist in dists if dist is not None)

        g_accuracy = torch.all(hx.g.round() == g_target[:, :, 0, 0], dim=-1)

        c_accuracy = torch.mean((hx.c.round() == hx.c_truth).float())
        c_precision = torch.mean(
            (hx.c.round()[hx.c > 0] == hx.c_truth[hx.c > 0]).float())
        c_recall = torch.mean(
            (hx.c.round()[hx.c_truth > 0] == hx.c_truth[hx.c_truth > 0]
             ).float())
        log = dict(
            g_accuracy=g_accuracy.float(),
            c_accuracy=c_accuracy,
            c_recall=c_recall,
            c_precision=c_precision)
        aux_loss = self.alpha * hx.c_loss - self.entropy_coef * entropies

        if self.teacher_agent:
            imitation_dist = self.teacher_agent(inputs, rnn_hxs, masks).dist
            imitation_probs = imitation_dist.probs.detach().unsqueeze(1)
            our_log_probs = torch.log(dists.a.probs).unsqueeze(2)
            imitation_obj = (imitation_probs @ our_log_probs).view(-1)
            log.update(imitation_obj=imitation_obj)
            aux_loss -= imitation_obj

        g_embed = rm.embed_task(actions.g)
        g_broad = broadcast_3d(g_embed, obs.shape[2:])
        value = rm.critic(rm.conv2((obs, g_broad)))

        for k, v in hx._asdict().items():
            if k.endswith('_loss'):
                log[k] = v

        return AgentValues(
            value=value,
            action=torch.cat(actions, dim=-1),
            action_log_probs=log_probs,
            aux_loss=aux_loss.mean(),
            rnn_hxs=torch.cat(hx, dim=-1),
            dist=None,
            log=log)

    def get_value(self, inputs, rnn_hxs, masks):
        n = inputs.shape[0]
        all_hxs, last_hx = self._forward_gru(
            inputs.view(n, -1), rnn_hxs, masks)
        return self.recurrent_module.parse_hidden(all_hxs).v

    @property
    def recurrent_hidden_state_size(self):
        return sum(self.recurrent_module.state_sizes)

    @property
    def is_recurrent(self):
        return True


class SubtasksRecurrence(torch.jit.ScriptModule):
    __constants__ = [
        'input_sections', 'subtask_space', 'state_sizes', 'recurrent'
    ]

    def __init__(self, obs_shape, action_space, task_space, hidden_size,
                 recurrent, hard_update, multiplicative_interaction):
        super().__init__()
        d, h, w = obs_shape
        conv_out_size = h * w * hidden_size
        self.subtask_space = list(map(int, task_space.nvec[0]))
        self.hard_update = hard_update
        subtask_size = sum(self.subtask_space)
        n_subtasks = task_space.shape[0]
        self.obs_sections = get_subtasks_obs_sections(task_space)
        self.obs_shape = d, h, w

        # networks
        self.recurrent = recurrent

        self.conv1 = nn.Sequential(
            init_(
                nn.Conv2d(
                    self.obs_sections.base,
                    hidden_size,
                    kernel_size=3,
                    stride=1,
                    padding=1), 'relu'), nn.ReLU(), Flatten())

        if multiplicative_interaction:
            conv_weight_shape = hidden_size, self.obs_sections.base, 3, 3
            self.conv_weight = nn.Sequential(
                nn.Linear(self.obs_sections.subtask,
                          np.prod(conv_weight_shape)),
                Reshape(-1, *conv_weight_shape))

        else:
            self.conv2 = nn.Sequential(
                Concat(dim=1),
                init_(
                    nn.Conv2d(
                        self.obs_sections.base + self.obs_sections.subtask,
                        hidden_size,
                        kernel_size=3,
                        stride=1,
                        padding=1), 'relu'), nn.ReLU(), Flatten())

        input_size = h * w * hidden_size  # conv output
        if isinstance(action_space.a, Discrete):
            num_outputs = action_space.a.n
            self.actor = Categorical(input_size, num_outputs)
        elif isinstance(action_space.a, Box):
            num_outputs = action_space.a.shape[0]
            self.actor = DiagGaussian(input_size, num_outputs)
        else:
            raise NotImplementedError

        self.critic = init_(nn.Linear(input_size, 1))

        in_size = (
            conv_out_size +  # x
            subtask_size +  # r
            subtask_size +  # g
            1)  # b

        self.f = nn.Sequential(
            Concat(1),
            init_(
                nn.Conv2d(
                    self.obs_sections.base +  # obs
                    action_space.a.n +  # hx.a
                    subtask_size +  # hx.g
                    subtask_size,  # hx.r
                    hidden_size,
                    kernel_size=3,
                    stride=1,
                    padding=1),
                'relu'),
            nn.MaxPool2d(kernel_size=(h + 2, w + 2), stride=1, padding=1),
            nn.ReLU(),
            Flatten(),
        )

        subcontroller = nn.GRUCell if recurrent else nn.Linear
        self.subcontroller = trace(
            lambda in_size: nn.Sequential(
                init_(subcontroller(in_size, hidden_size), 'relu'),
                nn.ReLU(),
            ),
            in_size=conv_out_size)  # h

        self.phi_update = trace(
            # lambda in_size: init_(nn.Linear(in_size, 2), 'sigmoid'),
            # in_size=(
            # hidden_size +  s
            # hidden_size))  h
            lambda in_size: init_(nn.Linear(in_size, 1), 'sigmoid'),
            in_size=(hidden_size))

        self.phi_shift = trace(
            lambda in_size: nn.Sequential(
                # init_(nn.Linear(in_size, hidden_size), 'relu'),
                # nn.ReLU(),
                # init_(nn.Linear(hidden_size, 3)),  # 3 for {-1, 0, +1}
                init_(nn.Linear(in_size, 3)),  # 3 for {-1, 0, +1}
            ),
            # in_size=hidden_size)
            in_size=hidden_size)

        self.pi_theta = nn.Sequential(
            Concat(dim=-1),
            Broadcast3d(h, w),
            torch.jit.trace(
                nn.Sequential(
                    init_(
                        nn.Conv2d(
                            (
                                subtask_size +  # r
                                hidden_size),  # h
                            hidden_size,
                            kernel_size=3,
                            stride=1,
                            padding=1),
                        'relu'),
                    nn.ReLU(),
                    Flatten(),
                ),
                example_inputs=torch.rand(1, subtask_size + hidden_size, h, w),
            ),
            Categorical(h * w * hidden_size, np.prod(self.subtask_space)),
        )

        self.beta = Categorical(
            conv_out_size +  # x
            subtask_size,  # g
            2)

        # embeddings
        for name, d in zip(
            ['type_embeddings', 'count_embeddings', 'obj_embeddings'],
                self.subtask_space):
            self.register_buffer(name, torch.eye(int(d)))

        self.register_buffer('a_one_hots', torch.eye(action_space.a.n))
        self.register_buffer('g_one_hots',
                             torch.eye(int(np.prod(self.subtask_space))))
        self.task_sections = [n_subtasks] * task_space.nvec.shape[1]
        state_sizes = RecurrentState(
            p=n_subtasks,
            r=subtask_size,
            h=hidden_size,
            g=1,
            b=1,
            b_probs=2,
            g_probs=np.prod(self.subtask_space),
            c=1,
            c_truth=1,
            c_probs=2,
            l=1,
            a=1,
            v=1,
            a_probs=action_space.a.n,
            l_probs=3,
            c_loss=1,
            l_loss=1,
            p_loss=1,
            r_loss=1,
            g_loss=1,
            b_loss=1,
            subtask=1)
        self.state_sizes = RecurrentState(*map(int, state_sizes))

    # @torch.jit.script_method
    def parse_hidden(self, hx):
        return RecurrentState(*torch.split(hx, self.state_sizes, dim=-1))

    @torch.jit.script_method
    def task_one_hots(self, task_type, count, obj):
        return torch.cat([
            self.type_embeddings[task_type.long()],
            self.count_embeddings[count.long()],
            self.obj_embeddings[obj.long()],
        ],
                         dim=-1)

    def encode(self, g1, g2, g3):
        x1, x2, x3 = self.subtask_space
        return (g1 * (x2 * x3) + g2 * x3 + g3).long()

    def decode(self, g):
        x1, x2, x3 = self.subtask_space
        g1 = g // (x2 * x3)
        x4 = g % (x2 * x3)
        g2 = x4 // x3
        g3 = x4 % x3
        return g1, g2, g3

    def embed_task(self, g):
        return self.task_one_hots(*self.decode(g)).squeeze(1)

    def check_grad(self, **kwargs):
        for k, v in kwargs.items():
            if v.grad_fn is not None:
                grads = torch.autograd.grad(
                    v.mean(),
                    self.parameters(),
                    retain_graph=True,
                    allow_unused=True)
                for (name, _), grad in zip(self.named_parameters(), grads):
                    if grad is None:
                        print(f'{k} has no grad wrt {name}')
                    else:
                        print(
                            f'mean grad ({v.mean().item()}) of {k} wrt {name}:',
                            grad.mean())
                        if torch.isnan(grad.mean()):
                            import ipdb
                            ipdb.set_trace()

    # @torch.jit.script_method
    def forward(self, inputs, hx):
        assert hx is not None
        T, N, _ = inputs.shape
        inputs = inputs.view(T, N, *self.obs_shape)

        obs, subtasks, task, next_subtask = torch.split(
            inputs, self.obs_sections, dim=2)
        subtasks = subtasks[:, :, :, 0, 0]
        task = task[:, :, :, 0, 0]
        next_subtask = next_subtask[:, :, :, 0, 0]
        task_type, count, obj = torch.split(task, self.task_sections, dim=-1)

        M = self.task_one_hots(task_type[0], (count - 1)[0], obj[0])
        new_episode = torch.all(hx.squeeze(0) == 0, dim=-1)
        hx = self.parse_hidden(hx)

        p = hx.p
        r = hx.r
        g = hx.g
        b = hx.b
        h = hx.h
        float_subtask = hx.subtask

        for x in hx:
            x.squeeze_(0)

        p[new_episode, 0] = 1.  # initialize pointer to first subtask
        r[new_episode] = M[new_episode, 0]  # initialize r to first subtask

        outputs = RecurrentState(*[[] for _ in RecurrentState._fields])

        n = obs.shape[0]
        for i in range(n):
            float_subtask += next_subtask[i]
            outputs.subtask.append(float_subtask)
            subtask = float_subtask.long()
            m = M.shape[0]
            conv_out = self.conv1(obs[i])

            # s = self.f(torch.cat([conv_out, r, g, b], dim=-1))
            # logits = self.phi_update(torch.cat([s, h], dim=-1))
            # if self.hard_update:
            # dist = FixedCategorical(logits=logits)
            # c = dist.sample().float()
            # outputs.c_probs.append(dist.probs)
            # else:
            # c = torch.sigmoid(logits[:, :1])
            # outputs.c_probs.append(torch.zeros_like(logits))  # dummy value

            a = self.a_one_hots[hx.a.long().flatten()]
            g_embed = self.embed_task(hx.g)
            g_embed[new_episode] = 0
            context = broadcast_3d(
                torch.cat([a, g_embed, hx.r], dim=-1), self.obs_shape[1:])
            h = self.f((obs[i], context))

            # print(debug_in[:, [39, 30, 21, 12, 98, 89]])
            # print(next_subtask[i])
            c = torch.sigmoid(self.phi_update(h))
            outputs.c_truth.append(next_subtask[i])

            if torch.any(next_subtask[i] > 0):
                weight = torch.ones_like(c)
                weight[next_subtask[i] > 0] /= torch.sum(next_subtask[i] > 0)
                weight[next_subtask[i] == 0] /= torch.sum(next_subtask[i] == 0)

                outputs.c_loss.append(
                    F.binary_cross_entropy(
                        torch.clamp(c, 0., 1.),
                        next_subtask[i],
                        weight=weight,
                        reduction='none'))
            else:
                outputs.c_loss.append(torch.zeros_like(c))

            outputs.c.append(c)

            # TODO: figure this out
            # if self.recurrent:
            #     h2 = self.subcontroller(obs[i], h)
            # else:
            # h2 = self.subcontroller(conv_out)

            logits = self.phi_shift(h)
            # if self.hard_update:
            # dist = FixedCategorical(logits=logits)
            # l = dist.sample()
            # outputs.l.append(l.float())
            # outputs.l_probs.append(dist.probs)
            # l = self.l_one_hots[l]
            # else:
            l = F.softmax(logits, dim=1)
            outputs.l.append(torch.zeros_like(c))  # dummy value
            outputs.l_probs.append(torch.zeros_like(l))  # dummy value

            # l_loss
            l_target = next_subtask[i].long().flatten()
            outputs.l_loss.append(
                F.cross_entropy(
                    logits,
                    l_target,
                    reduction='none',
                ).unsqueeze(1))

            p2 = batch_conv1d(p, l)

            # p_losss
            outputs.p_loss.append(
                F.cross_entropy(
                    p2.squeeze(1), subtask.squeeze(1),
                    reduction='none').unsqueeze(1))

            r2 = p2 @ M

            # r_loss
            r_target = []
            for j in range(m):
                r_target.append(M[j, subtask[j]])
            r_target = torch.cat(r_target).detach()
            r_loss = F.binary_cross_entropy(
                torch.clamp(r2.squeeze(1), 0., 1.),
                r_target,
                reduction='none',
            )
            outputs.r_loss.append(torch.mean(r_loss, dim=-1, keepdim=True))

            p = interp(p, p2.squeeze(1), c)
            r = interp(r, r2.squeeze(1), c)

            # h = interp(h, h2, c)

            outputs.p.append(p)
            outputs.r.append(r)
            outputs.h.append(h)

            # TODO: deterministic
            # g
            probs = self.pi_theta((h, r)).probs
            g_prev = self.g_one_hots[hx.g.long()].squeeze(1)
            c_g = c.clone()
            c_g[new_episode] = 1
            dist = FixedCategorical(probs=interp(g_prev, probs, c_g))
            g = dist.sample()
            outputs.g.append(g.float())
            outputs.g_probs.append(dist.probs)

            # g_loss
            # assert (int(i1), int(i2), int(i3)) == \
            #        np.unravel_index(int(g), self.subtask_space)
            g_embed = self.embed_task(g)
            g_loss = F.binary_cross_entropy(
                torch.clamp(g_embed, 0., 1.),
                r_target,
                reduction='none',
            )
            outputs.g_loss.append(torch.mean(g_loss, dim=-1, keepdim=True))

            # b
            dist = self.beta(torch.cat([conv_out, g_embed], dim=-1))
            b = dist.sample().float()
            outputs.b_probs.append(dist.probs)
            outputs.c_probs.append(torch.zeros_like(dist.probs))  # TODO

            # b_loss
            outputs.b_loss.append(-dist.log_probs(next_subtask[i]))
            outputs.b.append(b)

            # a
            g_broad = broadcast_3d(g_embed, self.obs_shape[1:])
            conv_out2 = self.conv2((obs[i], g_broad))
            dist = self.actor(conv_out2)
            a = dist.sample()
            # a[:] = 'wsadeq'.index(input('act:'))

            outputs.a.append(a.float())
            outputs.a_probs.append(dist.probs)

            # v
            outputs.v.append(self.critic(conv_out2))

        stacked = []
        for x in outputs:
            stacked.append(torch.stack(x))

        hx = torch.cat(stacked, dim=-1)
        return hx, hx[-1]
