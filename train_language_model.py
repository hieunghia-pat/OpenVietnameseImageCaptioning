import torch
import os
import dill as pickle
import numpy as np
import random
import argparse

from training_utils.captioning_model_trainer import Trainer
from configs.utils import get_config
from data_utils.vocab import Vocab
from data_utils.dataset import FeatureDataset, DictionaryDataset
from data_utils.utils import collate_fn
from models.language_models import get_language_model

random.seed(13)
torch.manual_seed(13)
np.random.seed(13)

parser = argparse.ArgumentParser()
parser.add_argument("--config-file", type=str, required=True)
args = parser.parse_args()

config = get_config(args.config_file)

device = "cuda" if torch.cuda.is_available() else "cpu"

# creating checkpoint directory
if not os.path.isdir(os.path.join(config.training.checkpoint_path, 
                                    f"{config.model.name}_using_{config.training.using_features}")):
    os.makedirs(os.path.join(config.training.checkpoint_path, 
                                f"{config.model.name}_using_{config.training.using_features}"))

# Creating vocabulary and dataset
if not os.path.isfile(os.path.join(config.training.checkpoint_path, 
                                    f"{config.model.name}_using_{config.training.using_features}", "vocab.pkl")):
    vocab = Vocab([config.path.train_json_path, config.path.dev_json_path], tokenizer_name=config.dataset.tokenizer, 
                    pretrained_language_model_name=config.model.pretrained_language_model_name)
    pickle.dump(vocab, open(os.path.join(config.training.checkpoint_path, 
                            f"{config.model.name}_using_{config.training.using_features}", "vocab.pkl"), "wb"))
else:
    vocab = pickle.load(open(os.path.join(config.training.checkpoint_path, 
                                            f"{config.model.name}_using_{config.training.using_features}", "vocab.pkl"), "rb"))

# creating iterable dataset
train_dataset = FeatureDataset(config.path.train_json_path, config.path.image_features_path, vocab) # for training with cross-entropy loss

val_dataset = FeatureDataset(config.path.dev_json_path, config.path.image_features_path, vocab) # for training with cross-entropy loss

if config.path.public_test_json_path is not None:
    public_test_dataset = FeatureDataset(config.path.public_test_json_path, config.path.image_features_path, vocab) # for training with cross-entropy loss
else:
    public_test_dataset = None

# creating dictionary dataset
train_dict_dataset = DictionaryDataset(config.path.train_json_path, config.path.image_features_path, vocab) # for training with self-critical learning
val_dict_dataset = DictionaryDataset(config.path.dev_json_path, config.path.image_features_path, vocab) # for calculating metricsn validation set

if config.path.public_test_json_path is not None:
    public_test_dict_dataset = DictionaryDataset(config.path.public_test_json_path, config.path.image_features_path, vocab=vocab)
else:
    public_test_dict_dataset = None

if config.path.private_test_json_path is not None:
    private_test_dict_dataset = DictionaryDataset(config.path.private_test_json_path, config.path.image_features_path, vocab=vocab)
else:
    private_test_dict_dataset = None

# init Transformer model.
model = get_language_model(vocab, config)

# Define Trainer
trainer = Trainer(model=model, train_datasets=(train_dataset, train_dict_dataset), val_datasets=(val_dataset, val_dict_dataset),
                    test_datasets=(public_test_dataset, public_test_dict_dataset), vocab=vocab, config=config, collate_fn=collate_fn)

# Training
if config.training.start_from:
    trainer.train(os.path.join(config.training.checkpoint_path, 
                                f"{config.model.name}_using_{config.training.using_features}", 
                                config.training.start_from))
else:
    trainer.train()