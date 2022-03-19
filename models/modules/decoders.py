import torch
from torch import nn
from torch.nn import functional as F
import numpy as np

from models.modules.attentions import MultiHeadAttention
from models.utils import generate_sequential_mask, sinusoid_encoding_table, generate_padding_mask
from models.modules.positionwise_feed_forward import PositionWiseFeedForward
from models.modules.containers import Module, ModuleList

class DecoderLayer(Module):
    "Decoder is made of self-attn, src-attn, and feed forward (defined below)"
    def __init__(self, d_model=512, d_k=64, d_v=64, h=8, d_ff=2048, dropout=.1, self_att_module=None,
                 use_aoa=False, enc_att_module=None, self_att_module_kwargs=None, enc_att_module_kwargs=None):
        super(DecoderLayer, self).__init__()
        self.self_attn = MultiHeadAttention(d_model, d_k, d_v, h, dropout, can_be_stateful=True,
                                            use_aoa=use_aoa,
                                            attention_module=self_att_module,
                                            attention_module_kwargs=self_att_module_kwargs)
        self.enc_attn = MultiHeadAttention(d_model, d_k, d_v, h, dropout, can_be_stateful=False,
                                            use_aoa=use_aoa,
                                            attention_module=enc_att_module,
                                            attention_module_kwargs=enc_att_module_kwargs)

        self.pwff = PositionWiseFeedForward(d_model, d_ff, dropout)

    def forward(self, input, enc_output, language_signals=None, mask_pad=None, mask_self_att=None, mask_enc_att=None):
        self_att = self.self_attn(input, input, input, attention_mask=mask_self_att)
        self_att = self_att.masked_fill(mask_pad, value=0)

        enc_att = self.enc_attn(self_att, enc_output, enc_output, language_signals=language_signals, 
                                attention_mask=mask_enc_att).masked_fill(mask_pad, value=0)
        enc_att = enc_att.masked_fill(mask_pad, value=0)

        ff = self.pwff(enc_att)
        
        return ff

class MeshedDecoderLayer(Module):
    def __init__(self, N_enc, d_model=512, d_k=64, d_v=64, h=8, d_ff=2048, dropout=.1, self_att_module=None,
                 use_aoa=False, enc_att_module=None, self_att_module_kwargs=None, enc_att_module_kwargs=None):
        super(MeshedDecoderLayer, self).__init__()
        self.self_att = MultiHeadAttention(d_model, d_k, d_v, h, dropout, can_be_stateful=True,
                                            use_aoa=use_aoa,
                                            attention_module=self_att_module,
                                            attention_module_kwargs=self_att_module_kwargs)
        self.enc_att = MultiHeadAttention(d_model, d_k, d_v, h, dropout, can_be_stateful=False,
                                            use_aoa=use_aoa,
                                            attention_module=enc_att_module,
                                            attention_module_kwargs=enc_att_module_kwargs)
        self.pwff = PositionWiseFeedForward(d_model, d_ff, dropout)

        self.N_enc = N_enc
        self.fc_alphas = nn.ModuleList([nn.Linear(d_model + d_model, d_model) for _ in range(N_enc)])

        self.init_weights()

    def init_weights(self):
        for fc_alpha in self.fc_alphas:
            nn.init.xavier_uniform_(fc_alpha.weight)

        for fc_alpha in self.fc_alphas:
            nn.init.constant_(fc_alpha.bias, 0)

    def forward(self, input, enc_output, language_signals=None, mask_pad=None, mask_self_att=None, mask_enc_att=None):
        assert enc_output.size(1) == self.N_enc, "total layers of the encoder must equal to total number of the encoder outputs"
        
        self_att = self.self_att(input, input, input, attention_mask=mask_self_att)
        self_att = self_att.masked_fill(mask_pad, value=0)

        enc_atts = []
        for ith in range(self.N_enc):
            enc_atts.append(self.enc_att(self_att, enc_output[:, ith], enc_output[:, ith], 
                            language_signals=language_signals, attention_mask=mask_enc_att).masked_fill(mask_pad, value=0))

        alphas = []
        for fc_alpha, enc_att in zip(self.fc_alphas, enc_atts):
            alphas.append(torch.sigmoid(fc_alpha(torch.cat([self_att, enc_att], -1))))

        attn = 0
        for alpha, enc_att in zip(alphas, enc_atts):
            attn += enc_att * alpha
        attn = attn / np.sqrt(self.N_enc)
        enc_att = enc_att.masked_fill(mask_pad, value=0)

        ff = self.pwff(attn)
        ff = ff.masked_fill(mask_pad, value=0)

        return ff

# class AdaptiveDecoderLayer(Module):
#     def __init__(self, d_model=512, d_k=64, d_v=64, h=8, d_ff=2048, dropout=.1, self_att_module=None,
#                  enc_att_module=None, self_att_module_kwargs=None, enc_att_module_kwargs=None):
#         super(AdaptiveDecoderLayer, self).__init__()
#         self.self_att = MultiHeadAttention(d_model, d_k, d_v, h, dropout, can_be_stateful=True,
#                                            attention_module=self_att_module,
#                                            attention_module_kwargs=self_att_module_kwargs)

#         self.enc_att = MultiHeadAttention(d_model, d_k, d_v, h, dropout, can_be_stateful=False, 
#                                             attention_module=enc_att_module, 
#                                             attention_module_kwargs=enc_att_module_kwargs)

#         self.dropout1 = nn.Dropout(dropout)
#         self.lnorm1 = nn.LayerNorm(d_model)

#         self.dropout2 = nn.Dropout(dropout)
#         self.lnorm2 = nn.LayerNorm(d_model)
#         self.pwff = PositionWiseFeedForward(d_model, d_ff, dropout)

#     def forward(self, input, enc_output, mask_pad, mask_self_att, mask_enc_att, language_feature=None, pos=None):
#         # MHA+AddNorm
#         self_att = self.self_att(input, input, input, mask_self_att)
#         self_att = self.lnorm1(input + self.dropout1(self_att))
#         self_att = self_att * mask_pad
#         # MHA+AddNorm
#         key = enc_output + pos
#         enc_att = self.enc_att(self_att, key, enc_output, mask_enc_att, language_feature=language_feature)
#         enc_att = self.lnorm2(self_att + self.dropout2(enc_att))
#         enc_att = enc_att * mask_pad
#         # FFN+AddNorm
#         ff = self.pwff(enc_att)
#         ff = ff * mask_pad
#         return ff

class Decoder(Module):
    "Generic N layer decoder with masking."
    def __init__(self, vocab_size, max_len, N_dec, padding_idx, d_model=512, d_k=64, d_v=64, h=8, d_ff=2048, dropout=.1,
                 use_aoa=False, self_att_module=None, enc_att_module=None, self_att_module_kwargs=None, enc_att_module_kwargs=None):
        super(Decoder, self).__init__()
        
        self.d_model = d_model
        self.word_emb = nn.Embedding(vocab_size, d_model, padding_idx=padding_idx)
        self.pos_emb = nn.Embedding.from_pretrained(sinusoid_encoding_table(max_len + 1, d_model, 0), freeze=True)
        self.layers = ModuleList(
            [DecoderLayer(d_model, d_k, d_v, h, d_ff, dropout, self_att_module=self_att_module, use_aoa=use_aoa,
                                enc_att_module=enc_att_module, self_att_module_kwargs=self_att_module_kwargs,
                                enc_att_module_kwargs=enc_att_module_kwargs) for _ in range(N_dec)])
        self.fc = nn.Linear(d_model, vocab_size, bias=False)
        self.max_len = max_len
        self.padding_idx = padding_idx
        self.N = N_dec

        self.register_state('running_mask_self_attention', torch.zeros((1, 1, 0)).bool())
        self.register_state('running_seq', torch.zeros((1,)).long())

    def forward(self, input, encoder_output, mask_encoder=None):
        # input (b_s, seq_len)
        b_s, seq_len = input.shape[:2]
        mask_queries = generate_padding_mask(input, self.padding_idx).to(input.device)  # (b_s, seq_len)
        mask_self_attention = generate_sequential_mask(seq_len).to(input.device)
        mask_self_attention = mask_self_attention.unsqueeze(0).unsqueeze(0)  # (1, 1, seq_len, seq_len)
        mask_self_attention = torch.logical_or(mask_self_attention, mask_queries.unsqueeze(1).unsqueeze(1))
        if self._is_stateful:
            self.running_mask_self_attention = torch.cat([self.running_mask_self_attention, mask_self_attention], -1)
            mask_self_attention = self.running_mask_self_attention

        seq = torch.arange(1, seq_len + 1).view(1, -1).expand(b_s, -1).to(input.device)  # (b_s, seq_len)
        seq = seq.masked_fill(mask_queries, 0)
        if self._is_stateful:
            self.running_seq.add_(1)
            seq = self.running_seq

        out = self.word_emb(input) + self.pos_emb(seq)
        for layer in self.layers:
            out = layer(out, encoder_output, mask_pad=mask_queries.unsqueeze(-1), mask_self_att=mask_self_attention, mask_enc_att=mask_encoder)

        out = self.fc(out)
        return F.log_softmax(out, dim=-1)

class MeshedDecoder(Module):
    def __init__(self, vocab_size, max_len, N_enc, N_dec, padding_idx, d_model=512, d_k=64, d_v=64, h=8, d_ff=2048, dropout=.1,
                 use_aoa=False, self_att_module=None, enc_att_module=None, self_att_module_kwargs=None, enc_att_module_kwargs=None):
        super(MeshedDecoder, self).__init__()
        self.d_model = d_model
        self.word_emb = nn.Embedding(vocab_size, d_model, padding_idx=padding_idx)
        self.pos_emb = nn.Embedding.from_pretrained(sinusoid_encoding_table(max_len + 1, d_model, 0), freeze=True)
        self.layers = ModuleList(
            [MeshedDecoderLayer(N_enc, d_model, d_k, d_v, h, d_ff, dropout, self_att_module=self_att_module, use_aoa=use_aoa,
                                enc_att_module=enc_att_module, self_att_module_kwargs=self_att_module_kwargs,
                                enc_att_module_kwargs=enc_att_module_kwargs) for _ in range(N_dec)])
        self.fc = nn.Linear(d_model, vocab_size, bias=False)
        self.max_len = max_len
        self.padding_idx = padding_idx
        self.N = N_dec

        self.register_state('running_mask_self_attention', torch.zeros((1, 1, 0)).bool())
        self.register_state('running_seq', torch.zeros((1,)).long())

    def forward(self, input, encoder_output, mask_encoder=None):
        # input (b_s, seq_len)
        b_s, seq_len = input.shape[:2]
        mask_queries = generate_padding_mask(input, self.padding_idx).to(input.device)  # (b_s, seq_len)
        mask_self_attention = generate_sequential_mask(seq_len).to(input.device)
        mask_self_attention = mask_self_attention.unsqueeze(0).unsqueeze(0)  # (1, 1, seq_len, seq_len)
        mask_self_attention = torch.logical_or(mask_self_attention, mask_queries.unsqueeze(1).unsqueeze(1))
        if self._is_stateful:
            self.running_mask_self_attention = torch.cat([self.running_mask_self_attention, mask_self_attention], -1)
            mask_self_attention = self.running_mask_self_attention

        seq = torch.arange(1, seq_len + 1).view(1, -1).expand(b_s, -1).to(input.device)  # (b_s, seq_len)
        seq = seq.masked_fill(mask_queries, 0)
        if self._is_stateful:
            self.running_seq.add_(1)
            seq = self.running_seq

        out = self.word_emb(input) + self.pos_emb(seq)
        for layer in self.layers:
            out = layer(out, encoder_output, mask_pad=mask_queries.unsqueeze(-1), mask_self_att=mask_self_attention, mask_enc_att=mask_encoder)

        out = self.fc(out)
        return F.log_softmax(out, dim=-1)

# class AdaptiveDecoder(Module):
#     def __init__(self, vocab_size, max_len, N_dec, padding_idx, language_model_path='../../saved_language_models/bert_language_best.pth', d_model=512, d_k=64, d_v=64, h=8, d_ff=2048, bert_hidden_size=768, dropout=.1,
#                  self_att_module=None, enc_att_module=None, self_att_module_kwargs=None, enc_att_module_kwargs=None):
#         super(AdaptiveDecoder, self).__init__()
#         self.d_model = d_model
#         self.word_emb = nn.Embedding(vocab_size, d_model, padding_idx=padding_idx)
#         self.pos_emb = nn.Embedding.from_pretrained(sinusoid_encoding_table(max_len + 1, d_model, 0), freeze=True)
#         self.layers = ModuleList(
#             [DecoderLayer(d_model, d_k, d_v, h, d_ff, dropout, self_att_module=self_att_module, enc_att_module=enc_att_module, self_att_module_kwargs=self_att_module_kwargs, enc_att_module_kwargs=enc_att_module_kwargs) if i < N_dec else DecoderAdaptiveLayer(d_model, d_k, d_v, h, d_ff, dropout, self_att_module=self_att_module, enc_att_module=enc_att_module, self_att_module_kwargs=self_att_module_kwargs, enc_att_module_kwargs=enc_att_module_kwargs) for i in range(N_dec + 1)])
#         self.fc = nn.Linear(d_model, vocab_size, bias=False)

#         # load and froze language model  
#         self.language_model = LanguageModel(padding_idx=padding_idx, bert_hidden_size=bert_hidden_size, vocab_size=vocab_size, max_len=max_len)
#         model_file = torch.load(language_model_path)
#         self.language_model.load_state_dict(model_file['state_dict'], strict=False)
#         for p in self.language_model.parameters():
#             p.requires_grad = False

#         self.max_len = max_len
#         self.padding_idx = padding_idx
#         self.N = N_dec

#         self.register_state('running_mask_self_attention', torch.zeros((1, 1, 0)).byte())
#         self.register_state('running_seq', torch.zeros((1,)).long())

#     def forward(self, input, encoder_output, mask_encoder, pos):
#         # input (b_s, seq_len)
#         b_s, seq_len = input.shape[:2]
#         mask_queries = (input != self.padding_idx).unsqueeze(-1).float()  # (b_s, seq_len, 1)
#         mask_self_attention = torch.triu(torch.ones((seq_len, seq_len), dtype=torch.uint8, device=input.device),
#                                          diagonal=1)
#         mask_self_attention = mask_self_attention.unsqueeze(0).unsqueeze(0)  # (1, 1, seq_len, seq_len)
#         mask_self_attention = mask_self_attention + (input == self.padding_idx).unsqueeze(1).unsqueeze(1).byte()
#         mask_self_attention = mask_self_attention.gt(0)  # (b_s, 1, seq_len, seq_len)
#         if self._is_stateful:
#             self.running_mask_self_attention = torch.cat([self.running_mask_self_attention.type_as(mask_self_attention), mask_self_attention], -1)
#             mask_self_attention = self.running_mask_self_attention

#         seq = torch.arange(1, seq_len + 1).view(1, -1).expand(b_s, -1).to(input.device)  # (b_s, seq_len)
#         seq = seq.masked_fill(mask_queries.squeeze(-1) == 0, 0)
#         if self._is_stateful:
#             self.running_seq.add_(1)
#             seq = self.running_seq

#         out = self.word_emb(input) + self.pos_emb(seq)
#         _, language_feature = self.language_model(input)

#         # special process for the beam search of inference
#         if encoder_output.shape[0] > pos.shape[0]:
#             assert encoder_output.shape[0] % pos.shape[0] == 0
#             beam_size = int(encoder_output.shape[0] / pos.shape[0])
#             pos = pos.unsqueeze(1)  # (bs, 1, 50, 512)
#             pos = pos.expand(pos.shape[0], pos.shape[1]*beam_size, pos.shape[2], pos.shape[3])  # (bs, 5, 50, 512)
#             pos = pos.contiguous().flatten(0, 1)  # (bs*5, 50, 512)

#         for i, l in enumerate(self.layers):
#             if i < self.N:
#                 out = l(out, encoder_output, mask_queries, mask_self_attention, mask_encoder, pos=pos)
#             else:
#                 out = l(out, encoder_output, mask_queries, mask_self_attention, mask_encoder, language_feature, pos=pos)

#         out = self.fc(out)
#         return F.log_softmax(out, dim=-1)
