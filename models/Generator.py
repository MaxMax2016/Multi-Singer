# -*- coding: utf-8 -*-

"""Generator Modules."""

import logging
import math

import numpy as np
import torch

from layers import Conv1d
from layers import Conv1d1x1
from layers import ResidualBlock
from layers import upsample
from layers import PQMF
import models


class Generator1(torch.nn.Module):
    """Generator1 module."""

    def __init__(self,
                 in_channels=1,
                 out_channels=1,
                 kernel_size=3,
                 layers=30,
                 stacks=3,
                 residual_channels=64,
                 gate_channels=128,
                 skip_channels=64,
                 aux_channels=80,
                 aux_context_window=2,
                 dropout=0.0,
                 bias=True,
                 use_weight_norm=True,
                 use_causal_conv=False,
                 upsample_conditional_features=True,
                 upsample_net="ConvInUpsampleNetwork",
                 upsample_params={"upsample_scales": [4, 4, 4, 4]},
                 ):
        """Initialize Generator module.

        Args:
            in_channels (int): Number of input channels.
            out_channels (int): Number of output channels.
            kernel_size (int): Kernel size of dilated convolution.
            layers (int): Number of residual block layers.
            stacks (int): Number of stacks i.e., dilation cycles.
            residual_channels (int): Number of channels in residual conv.
            gate_channels (int):  Number of channels in gated conv.
            skip_channels (int): Number of channels in skip conv.
            aux_channels (int): Number of channels for auxiliary feature conv.
            aux_context_window (int): Context window size for auxiliary feature.
            dropout (float): Dropout rate. 0.0 means no dropout applied.
            bias (bool): Whether to use bias parameter in conv layer.
            use_weight_norm (bool): Whether to use weight norm.
                If set to true, it will be applied to all of the conv layers.
            use_causal_conv (bool): Whether to use causal structure.
            upsample_conditional_features (bool): Whether to use upsampling network.
            upsample_net (str): Upsampling network architecture.
            upsample_params (dict): Upsampling network parameters.

        """
        super(Generator1, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.aux_channels = aux_channels
        self.aux_context_window = aux_context_window
        self.layers = layers
        self.stacks = stacks
        self.kernel_size = kernel_size

        # check the number of layers and stacks
        assert layers % stacks == 0
        layers_per_stack = layers // stacks

        # define first convolution
        self.first_conv = Conv1d1x1(in_channels, residual_channels, bias=True)

        # define conv + upsampling network
        if upsample_conditional_features:
            upsample_params.update({
                "use_causal_conv": use_causal_conv,
            })
            if upsample_net == "MelGANGenerator":
                assert aux_context_window == 0
                upsample_params.update({
                    "use_weight_norm": False,  # not to apply twice
                    "use_final_nonlinear_activation": False,
                })
                self.upsample_net = getattr(models, upsample_net)(**upsample_params)
            else:
                if upsample_net == "ConvInUpsampleNetwork":
                    upsample_params.update({
                        "aux_channels": aux_channels,
                        "aux_context_window": aux_context_window,
                    })
                self.upsample_net = getattr(upsample, upsample_net)(**upsample_params)
            self.upsample_factor = np.prod(upsample_params["upsample_scales"])
        else:
            self.upsample_net = None
            self.upsample_factor = 1

        # define residual blocks
        self.conv_layers = torch.nn.ModuleList()
        for layer in range(layers):
            dilation = 2 ** (layer % layers_per_stack)
            conv = ResidualBlock(
                kernel_size=kernel_size,
                residual_channels=residual_channels,
                gate_channels=gate_channels,
                skip_channels=skip_channels,
                aux_channels=aux_channels,
                dilation=dilation,
                dropout=dropout,
                bias=bias,
                use_causal_conv=use_causal_conv,
            )
            self.conv_layers += [conv]

        # define output layers
        self.last_conv_layers = torch.nn.ModuleList([
            torch.nn.ReLU(inplace=True),
            Conv1d1x1(skip_channels, skip_channels, bias=True),
            torch.nn.ReLU(inplace=True),
            Conv1d1x1(skip_channels, in_channels, bias=True),
        ])

        self.pqmf_conv1 = Conv1d(1, 128, kernel_size, 1, padding=3)
        self.pqmf_conv2 = Conv1d(128, 1, kernel_size, 1, padding=3)

        # apply weight norm
        if use_weight_norm:
            self.apply_weight_norm()

        self.pqmf = PQMF()


    def forward(self, x, c):
        """Calculate forward propagation.

        Args:
            x (Tensor): Input noise signal (B, 1, T).
            c (Tensor): Local conditioning auxiliary features (B, C ,T').

        Returns:
            Tensor: Output tensor (B, out_channels, T)

        """

        # perform upsampling

        if c is not None and self.upsample_net is not None:
            c = self.upsample_net(c)
            assert c.size(-1) * 4 == x.size(-1)
        x = self.pqmf.analysis(x)

        # encode to hidden representation
        x = self.first_conv(x)
        skips = 0
        for f in self.conv_layers:
            x, h = f(x, c)
            skips += h
        skips *= math.sqrt(1.0 / len(self.conv_layers))

        # apply final layers
        x = skips
        for f in self.last_conv_layers:
            x = f(x)

        x = self.pqmf.synthesis(x)
        x = self.pqmf_conv1(x)
        x = self.pqmf_conv2(x)
        return x

    def inference(self, c=None, x=None):
        """Perform inference.

        Args:
            c (Union[Tensor, ndarray]): Local conditioning auxiliary features (T' ,C).
            x (Union[Tensor, ndarray]): Input noise signal (T, 1).

        Returns:
            Tensor: Output tensor (T, out_channels)

        """
        if x is not None:
            if not isinstance(x, torch.Tensor):
                x = torch.tensor(x, dtype=torch.float).to(next(self.parameters()).device)
            x = x.transpose(1, 0).unsqueeze(0)
        else:
            assert c is not None
            x = torch.randn(1, 1, len(c) * self.upsample_factor * 4).to(next(self.parameters()).device)
        if c is not None:
            if not isinstance(c, torch.Tensor):
                c = torch.tensor(c, dtype=torch.float).to(next(self.parameters()).device)
            c = c.transpose(1, 0).unsqueeze(0)
            c = torch.nn.ReplicationPad1d(self.aux_context_window)(c)

        return self.forward(x, c).squeeze(0).transpose(1, 0)

    def remove_weight_norm(self):
        """Remove weight normalization module from all of the layers."""
        def _remove_weight_norm(m):
            try:
                logging.debug(f"Weight norm is removed from {m}.")
                torch.nn.utils.remove_weight_norm(m)
            except ValueError:  # this module didn't have weight norm
                return

        self.apply(_remove_weight_norm)

    def apply_weight_norm(self):
        """Apply weight normalization module from all of the layers."""
        def _apply_weight_norm(m):
            if isinstance(m, torch.nn.Conv1d) or isinstance(m, torch.nn.Conv2d):
                torch.nn.utils.weight_norm(m)
                logging.debug(f"Weight norm is applied to {m}.")

        self.apply(_apply_weight_norm)

    @staticmethod
    def _get_receptive_field_size(layers, stacks, kernel_size,
                                  dilation=lambda x: 2 ** x):
        assert layers % stacks == 0
        layers_per_cycle = layers // stacks
        dilations = [dilation(i % layers_per_cycle) for i in range(layers)]
        return (kernel_size - 1) * sum(dilations) + 1

    @property
    def receptive_field_size(self):
        """Return receptive field size."""
        return self._get_receptive_field_size(self.layers, self.stacks, self.kernel_size)

class Generator2(torch.nn.Module):
    """Generator2 module."""

    def __init__(self,
                 in_channels=1,
                 out_channels=4,
                 kernel_sizes=[7, 5],
                 layers=[16, 15],
                 stacks=[8, 5],
                 residual_channels=64,
                 aux_channels=80,
                 aux_context_window=2,
                 dropout=0.0,
                 bias=True,
                 use_weight_norm=True,
                 upsample_net="ConvInUpsampleNetwork",
                 upsample_params={"upsample_scales": [4, 4, 4, 4]},
                 ):

        super(Generator2, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.aux_channels = aux_channels
        self.stacks = stacks
        self.pqmf = PQMF(out_channels)
        self.aux_context_window = aux_context_window
        # define first convolution
        self.low_first_conv = Conv1d1x1(in_channels, residual_channels, bias=True)
        self.up_first_conv = Conv1d1x1(in_channels, residual_channels, bias=True)

        # define conv + upsampling network
        upsample_params.update({
            "aux_channels": aux_channels,
            "aux_context_window": aux_context_window,
        })
        self.upsample_net = getattr(upsample, upsample_net)(**upsample_params)

        # define residual blocks
        self.low_conv_layers = torch.nn.ModuleList()
        for layer in range(layers[0]):
            dilation = 2 ** (layer % stacks[0])
            conv = ResidualBlock(
                kernel_size=kernel_sizes[0],
                residual_channels=residual_channels,
                aux_channels=aux_channels,
                dilation=dilation,
                dropout=dropout,
                bias=bias,
            )
            self.low_conv_layers += [conv]

        self.up_conv_layers = torch.nn.ModuleList()
        for layer in range(layers[1]):
            dilation = 2 ** (layer % stacks[1])
            conv = ResidualBlock(
                kernel_size=kernel_sizes[1],
                residual_channels=residual_channels,
                aux_channels=aux_channels,
                dilation=dilation,
                dropout=dropout,
                bias=bias,
            )
            self.up_conv_layers += [conv]

        # define output layers
        self.last_low_conv_layers = torch.nn.ModuleList([
            torch.nn.ReLU(inplace=True),
            Conv1d1x1(residual_channels, residual_channels, bias=True),
            torch.nn.ReLU(inplace=True),
            Conv1d1x1(residual_channels, out_channels // 2, bias=True),
        ])

        self.last_up_conv_layers = torch.nn.ModuleList([
            torch.nn.ReLU(inplace=True),
            Conv1d1x1(residual_channels, residual_channels, bias=True),
            torch.nn.ReLU(inplace=True),
            Conv1d1x1(residual_channels, out_channels // 2, bias=True),
        ])
        self.upsample_factor = np.prod(upsample_params["upsample_scales"])



        # apply weight norm
        if use_weight_norm:
            self.apply_weight_norm()

    def forward(self, x, c):

        # perform upsampling
        if c is not None and self.upsample_net is not None:
            c = self.upsample_net(c)
            assert c.size(-1) == x.size(-1)

        x1 = self.low_first_conv(x)
        x2 = self.up_first_conv(x)

        skips = 0
        for f in self.low_conv_layers:
            x1, h = f(x1, c)
            skips += h
        skips *= math.sqrt(1.0 / len(self.low_conv_layers))

        # apply final layers
        x1 = skips
        for f in self.last_low_conv_layers:
            x1 = f(x1)

        skips = 0
        for f in self.up_conv_layers:
            x2, h = f(x2, c)
            skips += h
        skips *= math.sqrt(1.0 / len(self.up_conv_layers))

        # apply final layers
        x2 = skips
        for f in self.last_up_conv_layers:
            x2 = f(x2)

        w = torch.cat((x1, x2), dim=1)
        res = self.pqmf.synthesis(w)
        return res

    def remove_weight_norm(self):
        """Remove weight normalization module from all of the layers."""
        def _remove_weight_norm(m):
            try:
                logging.debug(f"Weight norm is removed from {m}.")
                torch.nn.utils.remove_weight_norm(m)
            except ValueError:  # this module didn't have weight norm
                return

        self.apply(_remove_weight_norm)

    def apply_weight_norm(self):
        """Apply weight normalization module from all of the layers."""
        def _apply_weight_norm(m):
            if isinstance(m, torch.nn.Conv1d) or isinstance(m, torch.nn.Conv2d):
                torch.nn.utils.weight_norm(m)
                logging.debug(f"Weight norm is applied to {m}.")

        self.apply(_apply_weight_norm)

    @staticmethod
    def _get_receptive_field_size(layers, stacks, kernel_size,
                                  dilation=lambda x: 2 ** x):
        assert layers % stacks == 0
        layers_per_cycle = layers // stacks
        dilations = [dilation(i % layers_per_cycle) for i in range(layers)]
        return (kernel_size - 1) * sum(dilations) + 1

    @property
    def receptive_field_size(self):
        """Return receptive field size."""
        return self._get_receptive_field_size(self.layers, self.stacks, self.kernel_size)
