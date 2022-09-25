import torch

from builders.encoder_builder import build_encoder
from builders.decoder_builder import build_decoder
from builders.vision_embedding_builder import build_vision_embedding
from builders.model_builder import META_ARCHITECTURE
from utils.instances import Instances
from .base_transformer import BaseTransformer

@META_ARCHITECTURE.register()
class ObjectRelationTransformer(BaseTransformer):
    def __init__(self, config, vocab):
        super().__init__(vocab)

        self.device = torch.device(config.DEVICE)

        self.vision_embedding = build_vision_embedding(config.VISION_EMBEDDING)
        self.encoder = build_encoder(config.ENCODER)
        self.decoder = build_decoder(config.DECODER, vocab)

    def forward(self, input_features: Instances):
        region_features = input_features.region_features
        region_boxes = input_features.region_boxes
        region_features, region_padding_mask = self.vision_embedding(region_features)

        encoder_features = self.encoder(Instances(
            features=region_features,
            features_padding_mask=region_padding_mask,
            boxes=region_boxes
        ))

        caption_tokens = input_features.caption_tokens
        output = self.decoder(Instances(
            caption_tokens=caption_tokens,
            encoder_features=encoder_features,
            encoder_attention_mask=region_padding_mask
        ))

        return output

    def encoder_forward(self, input_features: Instances):
        region_features = input_features.region_features
        region_boxes = input_features.region_boxes
        region_features, region_padding_mask = self.vision_embedding(region_features)

        encoder_features = self.encoder(Instances(
            features=region_features,
            features_padding_mask=region_padding_mask,
            boxes=region_boxes
        ))

        return encoder_features, region_padding_mask