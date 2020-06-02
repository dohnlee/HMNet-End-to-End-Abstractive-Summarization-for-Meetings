from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch
import torch.nn as nn


class MultiHeadAttention(nn.Module):
    def __init__(self, input_depth, total_key_depth, total_value_depth, output_depth,
                 num_heads, bias_mask=None, dropout=0.0, attention_type=None):
        """
        Parameters:
            input_depth: Size of last dimension of input
            total_key_depth: Size of last dimension of keys. Must be divisible by num_head
            total_value_depth: Size of last dimension of values. Must be divisible by num_head
            output_depth: Size last dimension of the final output
            num_heads: Number of attention heads
            bias_mask: Masking tensor to prevent connections to future elements
            dropout: Dropout probability (Should be non-zero only during training)
        """
        super(MultiHeadAttention, self).__init__()
        # Checks borrowed from
        # https://github.com/tensorflow/tensor2tensor/blob/master/tensor2tensor/layers/common_attention.py
        if total_key_depth % num_heads != 0:
            raise ValueError("Key depth (%d) must be divisible by the number of "
                             "attention heads (%d)." % (total_key_depth, num_heads))
        if total_value_depth % num_heads != 0:
            raise ValueError("Value depth (%d) must be divisible by the number of "
                             "attention heads (%d)." % (total_value_depth, num_heads))

        self.num_heads = num_heads
        self.query_scale = (total_key_depth//num_heads) ** -0.5
        self.bias_mask = bias_mask

        self.query_linear = nn.Linear(input_depth, total_key_depth, bias=False)
        self.key_linear = nn.Linear(input_depth, total_key_depth, bias=False)
        self.value_linear = nn.Linear(input_depth, total_value_depth, bias=False)
        self.output_linear = nn.Linear(total_value_depth, output_depth, bias=False)

        self.dropout = nn.Dropout(dropout)

        self.key_projected = None
        self.value_projected = None
        self.attention_type = attention_type

        self.attention = None

    def _split_heads(self, x):
        """
        Split x such to add an extra num_heads dimension
        Input:
            x: a Tensor with shape [batch_size, seq_length, depth]
        Returns:
            A Tensor with shape [batch_size, num_heads, seq_length, depth/num_heads]
        """

        if len(x.shape) != 3:
            raise ValueError("x must have rank 3")
        shape = x.shape
        return x.view(shape[0], shape[1], self.num_heads, shape[2]//self.num_heads).permute(0, 2, 1, 3)

    def _merge_heads(self, x):
        """
        Merge the extra num_heads into the last dimension
        Input:
            x: a Tensor with shape [batch_size, num_heads, seq_length, depth/num_heads]
        Returns:
            A Tensor with shape [batch_size, seq_length, depth]
        """
        if len(x.shape) != 4:
            raise ValueError("x must have rank 4")
        shape = x.shape
        return x.permute(0, 2, 1, 3).contiguous().view(shape[0], shape[2], shape[3]*self.num_heads)

    def forward(self, queries, keys, values, src_masks=None, layer_cache=None):

        queries = self.query_linear(queries)
        queries = self._split_heads(queries) # [batch_size, num_heads, seq_length, depth/num_heads]

        if (layer_cache is not None) and (layer_cache[self.attention_type] is not None):
            # for inference
            if self.attention_type == 'self-attention':

                keys = self.key_linear(keys)
                values = self.value_linear(values)

                keys = self._split_heads(keys)
                values = self._split_heads(values)

                keys = torch.cat([layer_cache[self.attention_type]['key_projected'], keys], dim=2)
                values = torch.cat([layer_cache[self.attention_type]['value_projected'], values], dim=2)
            else:
                # for word-level or turn-level attention (in these cases, keys and values are already processed in encoder)
                keys = layer_cache[self.attention_type]['key_projected']
                values = layer_cache[self.attention_type]['value_projected']
        else:
            keys = self.key_linear(keys)
            values = self.value_linear(values)
            keys = self._split_heads(keys)
            values = self._split_heads(values)

        self.key_projected = keys
        self.value_projected = values

        # scale queries
        queries *= self.query_scale

        logits = torch.matmul(queries, keys.permute(0, 1, 3, 2)) # (batch_size, num_heads, queries_seq_len,  keys_seq_len)

        if src_masks is not None:
            # Encoder Self-Attention
            logits += src_masks

        # Add bias to mask future values (Triangular Masking)
        if self.bias_mask is not None:
            logits += self.bias_mask[:, :, :logits.shape[-2], :logits.shape[-1]].type_as(logits.data)

        weights = nn.functional.softmax(logits, dim=-1)

        self.attention = weights

        # if self.attention_type == 'turn-level-attention':
        #     print('========= turn-level-attention =============')
        #     print('queries shape: ', queries.shape)
        #     print('keys.shape: ', keys.shape)
        #     print('logits.shape: ', logits.shape)
        #     print('self.attention shape: ', self.attention.shape)
        #     print('\n')

        weights = self.dropout(weights)

        contexts = torch.matmul(weights, values)
        # Merge Heads
        contexts = self._merge_heads(contexts)
        outputs = self.output_linear(contexts)
        return outputs


class Conv(nn.Module):
    """
    Convenience class that does padding and convolution for inputs in the format
    [batch_size, sequence length, hidden size]
    """

    def __init__(self, input_size, output_size, kernel_size, pad_type):
        """
        Parameters:
            input_size: Input feature size
            output_size: Output feature size
            kernel_size: Kernel width
            pad_type: left -> pad on the left side (to mask future data),
                      both -> pad on both sides
        """
        super(Conv, self).__init__()
        padding = (kernel_size - 1, 0) if pad_type == 'left' else (kernel_size // 2, (kernel_size - 1) // 2)
        self.pad = nn.ConstantPad1d(padding, 0)
        self.conv = nn.Conv1d(input_size, output_size, kernel_size=kernel_size, padding=0)

    def forward(self, inputs):
        inputs = self.pad(inputs.permute(0, 2, 1))
        outputs = self.conv(inputs).permute(0, 2, 1)

        return outputs


class PositionwiseFeedForward(nn.Module):
    """
    Does a Linear + RELU + Linear on each of the timesteps
    """

    def __init__(self, input_depth, filter_size, output_depth, layer_config='ll', padding='left', dropout=0.0):
        """
        Parameters:
            input_depth: Size of last dimension of input
            filter_size: Hidden size of the middle layer
            output_depth: Size last dimension of the final output
            layer_config: ll -> linear + ReLU + linear
                          cc -> conv + ReLU + conv etc.
            padding: left -> pad on the left side (to mask future data),
                     both -> pad on both sides
            dropout: Dropout probability (Should be non-zero only during training)
        """
        super(PositionwiseFeedForward, self).__init__()

        layers = []
        sizes = ([(input_depth, filter_size)] +
                 [(filter_size, filter_size)] * (len(layer_config) - 2) +
                 [(filter_size, output_depth)])

        for lc, s in zip(list(layer_config), sizes):
            if lc == 'l':
                layers.append(nn.Linear(*s))
            elif lc == 'c':
                layers.append(Conv(*s, kernel_size=3, pad_type=padding))
            else:
                raise ValueError("Unknown layer type {}".format(lc))

        self.layers = nn.ModuleList(layers)
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)

    def forward(self, inputs):
        x = inputs
        for i, layer in enumerate(self.layers):
            x = layer(x)
            if i < len(self.layers):
                x = self.relu(x)
                x = self.dropout(x)

        return x
