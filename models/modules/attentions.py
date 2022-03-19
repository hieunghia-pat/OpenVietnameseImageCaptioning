import torch
from torch import nn
from torch.nn import functional as F
import numpy as np

from models.modules.containers import Module
from models.utils import clones, box_relational_embedding, get_grids_position
import config

class ScaledDotProductAttention(nn.Module):
    '''
    Scaled dot-product attention
    '''

    def __init__(self, d_model, d_k, d_v, h):
        '''
            :param d_model: Output dimensionality of the model
            :param d_k: Dimensionality of queries and keys
            :param d_v: Dimensionality of values
            :param h: Number of heads
        '''
        super(ScaledDotProductAttention, self).__init__()
        self.fc_q = nn.Linear(d_model, h * d_k)
        self.fc_k = nn.Linear(d_model, h * d_k)
        self.fc_v = nn.Linear(d_model, h * d_v)
        self.fc_o = nn.Linear(h * d_v, d_model)

        self.d_model = d_model
        self.d_k = d_k
        self.d_v = d_v
        self.h = h

        self.init_weights()

    def init_weights(self):
        nn.init.xavier_uniform_(self.fc_q.weight)
        nn.init.xavier_uniform_(self.fc_k.weight)
        nn.init.xavier_uniform_(self.fc_v.weight)
        nn.init.xavier_uniform_(self.fc_o.weight)
        nn.init.constant_(self.fc_q.bias, 0)
        nn.init.constant_(self.fc_k.bias, 0)
        nn.init.constant_(self.fc_v.bias, 0)
        nn.init.constant_(self.fc_o.bias, 0)

    def forward(self, queries, keys, values, attention_mask=None, attention_weights=None):
        '''
            Computes
            :param queries: Queries (b_s, nq, d_model)
            :param keys: Keys (b_s, nk, d_model)
            :param values: Values (b_s, nk, d_model)
            :param attention_mask: Mask over attention values (b_s, h, nq, nk). True indicates masking.
            :param attention_weights: Multiplicative weights for attention values (b_s, h, nq, nk).
            :return:
        '''
        b_s, nq = queries.shape[:2]
        nk = keys.shape[1]
        q = self.fc_q(queries).view(b_s, nq, self.h, self.d_k).permute(0, 2, 1, 3)  # (b_s, h, nq, d_k)
        k = self.fc_k(keys).view(b_s, nk, self.h, self.d_k).permute(0, 2, 3, 1)  # (b_s, h, d_k, nk)
        v = self.fc_v(values).view(b_s, nk, self.h, self.d_v).permute(0, 2, 1, 3)  # (b_s, h, nk, d_v)

        att = torch.matmul(q, k) / np.sqrt(self.d_k)  # (b_s, h, nq, nk)
        if attention_weights is not None:
            att = att * attention_weights
        if attention_mask is not None:
            att = att.masked_fill(attention_mask, -np.inf)
        att = torch.softmax(att, dim=-1)
        out = torch.matmul(att, v).permute(0, 2, 1, 3).contiguous().view(b_s, nq, self.h * self.d_v)  # (b_s, nq, h*d_v)
        out = self.fc_o(out)  # (b_s, nq, d_model)

        return out

class AugmentedGeometryScaledDotProductAttention(nn.Module):
    '''
    Scaled dot-product attention with box relation
    '''

    def __init__(self, d_model, d_k, d_v, h, trignometric_embedding=True):
        '''
            :param d_model: Output dimensionality of the model
            :param d_k: Dimensionality of queries and keys
            :param d_v: Dimensionality of values
            :param h: Number of heads
        '''
        super(AugmentedGeometryScaledDotProductAttention, self).__init__()

        self.trignometric_embedding = trignometric_embedding
        if trignometric_embedding:
            self.d_g = d_model // h
        else:
            self.d_g = 4

        self.fc_q = nn.Linear(d_model, h * d_k)
        self.fc_k = nn.Linear(d_model, h * d_k)
        self.fc_v = nn.Linear(d_model, h * d_v)
        self.fc_o = nn.Linear(h * d_v, d_model)
        self.fc_gs = clones(nn.Linear(self.d_g, 1), h)

        self.d_model = d_model
        self.d_k = d_k
        self.d_v = d_v
        self.h = h

        self.init_weights()

    def init_weights(self):
        nn.init.xavier_uniform_(self.fc_q.weight)
        nn.init.xavier_uniform_(self.fc_k.weight)
        nn.init.xavier_uniform_(self.fc_v.weight)
        nn.init.xavier_uniform_(self.fc_o.weight)
        for fc_g in self.fc_gs:
            nn.init.xavier_uniform_(fc_g.weight)

        nn.init.constant_(self.fc_q.bias, 0)
        nn.init.constant_(self.fc_k.bias, 0)
        nn.init.constant_(self.fc_v.bias, 0)
        nn.init.constant_(self.fc_o.bias, 0)
        for fc_g in self.fc_gs:
            nn.init.constant_(fc_g.bias, 0)

    def forward(self, queries, keys, values, boxes=None, grid_size=None, attention_mask=None, attention_weights=None):
        '''
            Computes
            :param queries: Queries (b_s, nq, d_model)
            :param keys: Keys (b_s, nk, d_model)
            :param values: Values (b_s, nk, d_model)
            :param boxes: Boxes (b_s, nk, 4). None indicates image is extracted under region-based methods.
            :param grid_size: Size of the image features. Default value is None if image features is regional features
            :param attention_mask: Mask over attention values (b_s, h, nq, nk). True indicates masking.
            :param attention_weights: Multiplicative weights for attention values (b_s, h, nq, nk).
            :return:
        '''
        
        # embedding geometric information from boxes coordinates
        if grid_size is not None:
            assert boxes is None, "there is no boxe when using grid-based extractor"
            bs, seq_len = queries.shape[:2]
            boxes = get_grids_position(bs, seq_len, grid_size)
        else:
            assert boxes is not None, "coordinates of objects are requiered for region-based extractor"
        relative_geometry_embeddings = box_relational_embedding(boxes, dim_g=self.d_g, trignometric_embedding=self.trignometric_embedding)
        flatten_relative_geometry_embeddings = relative_geometry_embeddings.view(-1, self.d_g)
        bs, nk, _, _ = relative_geometry_embeddings.shape
        box_size_per_head = [bs, 1, nk, nk]
        relative_geometry_weights_per_head = [fc_g(flatten_relative_geometry_embeddings).view(box_size_per_head) for fc_g in self.fc_gs]
        relative_geometry_weights = torch.cat(relative_geometry_weights_per_head, dim=1) # (bs, h, nk, nk)
        relative_geometry_weights = F.relu(relative_geometry_weights)
        
        b_s, nq = queries.shape[:2]
        nk = keys.shape[1]
        q = self.fc_q(queries).view(b_s, nq, self.h, self.d_k).permute(0, 2, 1, 3)  # (b_s, h, nq, d_k)
        k = self.fc_k(keys).view(b_s, nk, self.h, self.d_k).permute(0, 2, 3, 1)  # (b_s, h, d_k, nk)
        v = self.fc_v(values).view(b_s, nk, self.h, self.d_v).permute(0, 2, 1, 3)  # (b_s, h, nk, d_v)

        a = torch.matmul(q, k) / np.sqrt(self.d_k)  # (b_s, h, nq, nk)
        if attention_weights is not None:
            a = a * attention_weights
        if attention_mask is not None:
            a = a.masked_fill(attention_mask, -np.inf)

        g = relative_geometry_weights
        mn = torch.log(torch.clamp(g, min = 1e-6)) + a
        mn = torch.softmax(mn, dim=-1)
        out = torch.matmul(mn, v).permute(0, 2, 1, 3).contiguous().view(b_s, nq, self.h * self.d_v)  # (b_s, nq, h*d_v)
        out = self.fc_o(out)  # (b_s, nq, d_model)

        return out

class AugmentedMemoryScaledDotProductAttention(nn.Module):
    '''
        Scaled dot-product attention with memory
    '''

    def __init__(self, d_model, d_k, d_v, h, m=config.total_memory):
        '''
        :param d_model: Output dimensionality of the model
        :param d_k: Dimensionality of queries and keys
        :param d_v: Dimensionality of values
        :param h: Number of heads
        :param m: Number of memory slots
        '''
        super(AugmentedMemoryScaledDotProductAttention, self).__init__()
        self.fc_q = nn.Linear(d_model, h * d_k)
        self.fc_k = nn.Linear(d_model, h * d_k)
        self.fc_v = nn.Linear(d_model, h * d_v)
        self.fc_o = nn.Linear(h * d_v, d_model)
        self.m_k = nn.Parameter(torch.FloatTensor(1, m, h * d_k))
        self.m_v = nn.Parameter(torch.FloatTensor(1, m, h * d_v))

        self.d_model = d_model
        self.d_k = d_k
        self.d_v = d_v
        self.h = h
        self.m = m

        self.init_weights()

    def init_weights(self):
        nn.init.xavier_uniform_(self.fc_q.weight)
        nn.init.xavier_uniform_(self.fc_k.weight)
        nn.init.xavier_uniform_(self.fc_v.weight)
        nn.init.xavier_uniform_(self.fc_o.weight)
        nn.init.normal_(self.m_k, 0, 1 / self.d_k)
        nn.init.normal_(self.m_v, 0, 1 / self.m)
        nn.init.constant_(self.fc_q.bias, 0)
        nn.init.constant_(self.fc_k.bias, 0)
        nn.init.constant_(self.fc_v.bias, 0)
        nn.init.constant_(self.fc_o.bias, 0)

    def forward(self, queries, keys, values, attention_mask=None, attention_weights=None):
        '''
        Computes
            :param queries: Queries (b_s, nq, d_model)
            :param keys: Keys (b_s, nk, d_model)
            :param values: Values (b_s, nk, d_model)
            :param attention_mask: Mask over attention values (b_s, h, nq, nk). True indicates masking.
            :param attention_weights: Multiplicative weights for attention values (b_s, h, nq, nk).
            :return:
        '''
        b_s, nq = queries.shape[:2]
        nk = keys.shape[1]

        m_k = np.sqrt(self.d_k) * self.m_k.expand(b_s, self.m, self.h * self.d_k)
        m_v = np.sqrt(self.m) * self.m_v.expand(b_s, self.m, self.h * self.d_v)

        q = self.fc_q(queries).view(b_s, nq, self.h, self.d_k).permute(0, 2, 1, 3)  # (b_s, h, nq, d_k)
        k = torch.cat([self.fc_k(keys), m_k], 1).view(b_s, nk + self.m, self.h, self.d_k).permute(0, 2, 3, 1)  # (b_s, h, d_k, nk)
        v = torch.cat([self.fc_v(values), m_v], 1).view(b_s, nk + self.m, self.h, self.d_v).permute(0, 2, 1, 3)  # (b_s, h, nk, d_v)

        att = torch.matmul(q, k) / np.sqrt(self.d_k)  # (b_s, h, nq, nk)
        if attention_weights is not None:
            att = torch.cat([att[:, :, :, :nk] * attention_weights, att[:, :, :, nk:]], -1)
        if attention_mask is not None:
            att[:, :, :, :nk] = att[:, :, :, :nk].masked_fill(attention_mask, -np.inf)
        att = torch.softmax(att, -1)
        out = torch.matmul(att, v).permute(0, 2, 1, 3).contiguous().view(b_s, nq, self.h * self.d_v)  # (b_s, nq, h*d_v)
        out = self.fc_o(out)  # (b_s, nq, d_model)

        return out

class AdaptiveScaledDotProductAttention(nn.Module):
    '''
    Scaled dot-product with adaptive attention
    '''

    def __init__(self, d_model, d_k, d_v, h, dropout=.1, comment=None):
        '''
        :param d_model: Output dimensionality of the model
        :param d_k: Dimensionality of queries and keys
        :param d_v: Dimensionality of values
        :param h: Number of heads
        '''
        super(AdaptiveScaledDotProductAttention, self).__init__()
        self.fc_q = nn.Linear(d_model, h * d_k)
        self.fc_k = nn.Linear(d_model, h * d_k)
        self.fc_v = nn.Linear(d_model, h * d_v)
        self.fc_s = nn.Linear(d_model, h * d_k) # for language signals
        self.fc_o = nn.Linear(h * d_v, d_model)

        self.dropout = nn.Dropout(dropout)

        self.d_model = d_model
        self.d_k = d_k
        self.d_v = d_v
        self.h = h

        self.init_weights()

        self.comment = comment

    def init_weights(self):
        nn.init.xavier_uniform_(self.fc_q.weight)
        nn.init.xavier_uniform_(self.fc_k.weight)
        nn.init.xavier_uniform_(self.fc_v.weight)
        nn.init.xavier_uniform_(self.fc_o.weight)
        nn.init.xavier_uniform_(self.fc_s.weight)
        nn.init.constant_(self.fc_q.bias, 0)
        nn.init.constant_(self.fc_k.bias, 0)
        nn.init.constant_(self.fc_v.bias, 0)
        nn.init.constant_(self.fc_o.bias, 0)
        nn.init.constant_(self.fc_s.bias, 0)

    def forward(self, queries, keys, values, language_signals, attention_mask=None, attention_weights=None):
        '''
        Computes
        :param queries: Queries (b_s, nq, d_model)
        :param keys: Keys (b_s, nk, d_model)
        :param values: Values (b_s, nk, d_model)
        :param language_signals: Language signals (b_s, nq, d_model)
        :param attention_mask: Mask over attention values (b_s, h, nq, nk). True indicates masking.
        :param attention_weights: Multiplicative weights for attention values (b_s, h, nq, nk).
        :return:
        '''

        b_s, nq = queries.shape[:2]
        nk = keys.shape[1]

        q = self.fc_q(queries).view(b_s, nq, self.h, self.d_k).permute(0, 2, 1, 3)  # (b_s, h, nq, d_k)
        s = self.fc_s(language_signals).view(b_s, nq, self.h, self.d_k).permute(0, 2, 1, 3)  # (b_s, h, nq, d_k)

        k = self.fc_k(keys).view(b_s, nk, self.h, self.d_k).permute(0, 2, 3, 1)  # (b_s, h, d_k, nk)
        v = self.fc_v(values).view(b_s, nk, self.h, self.d_v).permute(0, 2, 1, 3)  # (b_s, h, nk, d_v)

        attn = torch.matmul(q, k) / np.sqrt(self.d_k)  # (b_s, h, nq, nk)
        if attention_weights is not None:
            attn = attn * attention_weights
        if attention_mask is not None:
            attn = attn.masked_fill(attention_mask, -np.inf)

        language_attn = torch.matmul(q, s.permute(0, 1, 3, 2)) / np.sqrt(self.d_k)  # (b_s, h, nq, nq)
        language_attn = torch.cat([language_attn[:, :, i, i].unsqueeze(-1) for i in range(nq)], -1) # (b_s, h, nq)

        combined_attn = torch.cat([attn, language_attn.unsqueeze(-1)], dim=-1)     # (b_s, h, nq, nk + 1)
        combined_attn = [torch.softmax(combined_attn[:, :, i, :].unsqueeze(2), dim=-1) for i in range(nq)] # [ (b_s, h, 1, nk + 1) ]
        # if use dropout
        # combined_att = [self.dropout(item) for item in combined_att]

        combined_v = [torch.cat([v, s[:, :, i, :].unsqueeze(2)], 2) for i in range(nq)] # [ (b_s, h, nk + 1, d_v) ]

        assert len(combined_attn) == len(combined_v) == nq
        out = torch.cat([torch.matmul(combined_attn[i], combined_v[i]) for i in range(nq)], dim=2)

        out = out.permute(0, 2, 1, 3).contiguous().view(b_s, nq, self.h * self.d_v)  # (b_s, nq, h*d_v)
        out = self.fc_o(out)  # (b_s, nq, d_model)

        return out

class MultiHeadAttention(Module):
    '''
        Multi-head attention layer with Dropout and Layer Normalization.
    '''

    def __init__(self, d_model, d_k, d_v, h, dropout=.1, identity_map_reordering=False, can_be_stateful=False,
                 use_aoa=False, attention_module=None, attention_module_kwargs=None):
        super(MultiHeadAttention, self).__init__()
        self.identity_map_reordering = identity_map_reordering

        self.use_aoa = use_aoa # whether to use Attention on Attention (AoA) mechanism or not
        if self.use_aoa:    # define additionally AoA layers
            self.informative_attention = nn.Linear(2*d_model, d_model)
            self.gated_attention = nn.Linear(2*d_model, d_model)

        if attention_module is not None:
            if attention_module_kwargs is not None:
                self.attention = attention_module(d_model=d_model, d_k=d_k, d_v=d_v, h=h, **attention_module_kwargs)
            else:
                self.attention = attention_module(d_model=d_model, d_k=d_k, d_v=d_v, h=h)
        else:
            self.attention = ScaledDotProductAttention(d_model=d_model, d_k=d_k, d_v=d_v, h=h)
        self.dropout = nn.Dropout(p=dropout)
        self.layer_norm = nn.LayerNorm(d_model)

        self.can_be_stateful = can_be_stateful
        if self.can_be_stateful:
            self.register_state('running_keys', torch.zeros((0, d_model)))
            self.register_state('running_values', torch.zeros((0, d_model)))

    def forward(self, queries, keys, values, boxes=None, grid_size=None, language_signals=None, attention_mask=None, attention_weights=None):
        if self.can_be_stateful and self._is_stateful:
            self.running_keys = torch.cat([self.running_keys, keys], 1)
            keys = self.running_keys

            self.running_values = torch.cat([self.running_values, values], 1)
            values = self.running_values

        if self.identity_map_reordering:
            queries = self.layer_norm(queries)
            keys = self.layer_norm(keys)
            values = self.layer_norm(values)

            if (boxes is not None) or (grid_size is not None):  
                # attention with geometry-augmented attention
                out = self.attention(queries, keys, values, boxes, grid_size, attention_mask, attention_weights)
            elif language_signals is not None:  
                # adaptive attention
                out = self.attention(queries, keys, values, language_signals, attention_mask, attention_weights)
            else:                   
                # original attention or memory augmented attention
                out = self.attention(queries, keys, values, attention_mask, attention_weights)
            
            # residual connection after normalizing
            out = queries + self.dropout(torch.relu(out))
        else:
            if (boxes is not None) or (grid_size is not None):  
                # attention with geometry-augmented attention
                out = self.attention(queries, keys, values, boxes, grid_size, attention_mask, attention_weights)
            elif language_signals is not None:  
                # adaptive attention
                out = self.attention(queries, keys, values, language_signals, attention_mask, attention_weights)
            else:                   
                # original attention or memory augmented attention
                out = self.attention(queries, keys, values, attention_mask, attention_weights)
            
            # normalization after residual connection
            out = self.dropout(out)
            out = self.layer_norm(queries + out)

        if self.use_aoa:
            aoa_input = torch.cat([queries, out], dim=-1)
            i = self.informative_attention(aoa_input)
            g = torch.sigmoid(self.gated_attention(aoa_input))
            out = i * g
            
        return out