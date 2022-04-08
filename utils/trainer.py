from torch.nn import NLLLoss
from torch.optim import Adam
from torch.optim.lr_scheduler import LambdaLR

from data_utils.vocab import Vocab
from data_utils.utils import *
from models.modules.transformer import Transformer
from data_utils.dataset import *
import evaluation
from evaluation import Cider, PTBTokenizer

import config

import multiprocessing
from tqdm import tqdm
import itertools
from typing import Tuple, Union
import random
from shutil import copyfile

device = "cuda" if torch.cuda.is_available() else "cpu"

class Trainer:
    def __init__(self,  model: Transformer, 
                        train_datasets: Tuple[FeatureDataset, DictionaryDataset],
                        val_datasets: Tuple[FeatureDataset, DictionaryDataset],
                        test_datasets: Tuple[Union[FeatureDataset, None], Union[DictionaryDataset, None]],
                        vocab: Vocab,
                        collate_fn=collate_fn):
        self.model = model
        self.vocab = vocab
        self.optim = Adam(model.parameters(), lr=1, betas=(0.9, 0.98))
        self.scheduler = LambdaLR(self.optim, self.lambda_lr)
        self.loss_fn = NLLLoss(ignore_index=self.vocab.padding_idx)
        
        self.epoch = 0

        self.train_dataset, self.train_dict_dataset = train_datasets
        self.val_dataset, self.val_dict_dataset = val_datasets

        # creating iterable-dataset data loader
        self.train_dataloader = data.DataLoader(
            dataset=self.train_dataset,
            batch_size=config.batch_size,
            shuffle=True,
            num_workers=config.workers,
            collate_fn=collate_fn
        )
        self.val_dataloader = data.DataLoader(
            dataset=self.val_dataset,
            batch_size=config.batch_size,
            shuffle=True,
            num_workers=config.workers,
            collate_fn=collate_fn
        )

        # creating dictionary iterable-dataset data loader
        self.train_dict_dataloader = data.DataLoader(
            dataset=self.train_dict_dataset,
            batch_size=config.batch_size // config.beam_size,
            shuffle=True,
            collate_fn=collate_fn
        )
        self.val_dict_dataloader = data.DataLoader(
            dataset=self.val_dict_dataset,
            batch_size=config.batch_size // config.beam_size,
            shuffle=True,
            collate_fn=collate_fn
        )
        
        self.test_dataset, self.test_dict_dataset = test_datasets

        if self.test_dataset is not None:
            self.test_dataloader = data.DataLoader(
                dataset=self.test_dataset,
                batch_size=config.batch_size,
                shuffle=True,
                num_workers=config.workers,
                collate_fn=collate_fn
            )
        else:
            self.test_dataloader = None

        if self.test_dict_dataset is not None:
            self.test_dict_dataloader = data.DataLoader(
                dataset=self.test_dict_dataset,
                batch_size=config.batch_size // config.beam_size,
                shuffle=True,
                collate_fn=collate_fn
            )
        else:
            self.test_dict_dataloader = None

        self.cider_train = Cider(PTBTokenizer.tokenize(self.train_dataset.captions))

    def evaluate_loss(self, dataloader: data.DataLoader):
        # Calculating validation loss
        self.model.eval()
        running_loss = .0
        with tqdm(desc='Epoch %d - Validation' % self.epoch, unit='it', total=len(dataloader)) as pbar:
            with torch.no_grad():
                for it, sample in enumerate(dataloader):
                    features = sample["features"].to(device)
                    tokens = sample["tokens"].to(device)
                    shifted_right_tokens = sample["shifted_right_tokens"].to(device)
                    out = self.model(features, tokens).contiguous()
                    loss = self.loss_fn(out.view(-1, len(self.vocab)), shifted_right_tokens.view(-1))
                    this_loss = loss.item()
                    running_loss += this_loss

                    pbar.set_postfix(loss=running_loss / (it + 1))
                    pbar.update()

        val_loss = running_loss / len(dataloader)

        return val_loss

    def evaluate_metrics(self, dataloader: data.DataLoader):
        self.model.eval()
        gen = {}
        gts = {}
        with tqdm(desc='Epoch %d - Evaluation' % self.epoch, unit='it', total=len(dataloader)) as pbar:
            for it, sample in enumerate(dataloader):
                features = sample["features"].to(device)
                caps_gt = sample["captions"]
                with torch.no_grad():
                    out, _ = self.model.beam_search(features, max_len=self.vocab.max_caption_length, eos_idx=self.vocab.eos_idx, 
                                                beam_size=config.beam_size, out_size=1)
                caps_gen = self.vocab.decode_caption(out, join_words=False)
                for i, (gts_i, gen_i) in enumerate(zip(caps_gt, caps_gen)):
                    gen_i = ' '.join([k for k, g in itertools.groupby(gen_i)])
                    gen['%d_%d' % (it, i)] = [gen_i, ]
                    gts['%d_%d' % (it, i)] = gts_i
                pbar.update()

        gts = evaluation.PTBTokenizer.tokenize(gts)
        gen = evaluation.PTBTokenizer.tokenize(gen)
        scores, _ = evaluation.compute_scores(gts, gen)

        return scores

    def train_xe(self):
        # Training with cross-entropy loss
        self.model.train()

        running_loss = .0
        with tqdm(desc='Epoch %d - Training with cross-entropy loss' % self.epoch, unit='it', total=len(self.train_dataloader)) as pbar:
            for it, sample in enumerate(self.train_dataloader):
                features = sample["features"].to(device)
                tokens = sample["tokens"].to(device)
                shifted_right_tokens = sample["shifted_right_tokens"].to(device)
                out = self.model(features, tokens).contiguous()
                self.optim.zero_grad()
                loss = self.loss_fn(out.view(-1, len(self.vocab)), shifted_right_tokens.view(-1))
                loss.backward()

                self.optim.step()
                this_loss = loss.item()
                running_loss += this_loss

                pbar.set_postfix(loss=running_loss / (it + 1))
                pbar.update()
                self.scheduler.step()
    
    def train_scst(self):
        # Training with self-critical learning
        tokenizer_pool = multiprocessing.Pool()
        running_reward = .0
        running_reward_baseline = .0

        vocab = self.train_dataset.vocab

        self.model.train()

        running_loss = .0
        with tqdm(desc='Epoch %d - Training with self-critical learning' % self.epoch, unit='it', total=len(self.train_dict_dataloader)) as pbar:
            for it, sample in enumerate(self.train_dict_dataloader):
                features = sample["features"].to(device)
                boxes = sample["boxes"].to(device)
                outs, log_probs = self.model.beam_search(features, boxes=boxes, max_len=vocab.max_caption_length, eos_idx=vocab.eos_idx,
                                                    beam_size=config.batch_size, out_size=config.beam_size)
                self.optim.zero_grad()

                # Rewards
                caps_gen = vocab.decode_caption(outs.contiguous().view(-1, vocab.max_caption_length), join_words=True)
                caps_gt = list(itertools.chain(*([c, ] * config.beam_size for c in caps_gt)))
                caps_gen, caps_gt = tokenizer_pool.map(evaluation.PTBTokenizer.tokenize, [caps_gen, caps_gt])
                reward = self.train_cider.compute_score(caps_gt, caps_gen)[1].astype(np.float32)
                reward = torch.from_numpy(reward).to(device).view(features.shape[0], config.beam_size)
                reward_baseline = torch.mean(reward, dim=-1, keepdim=True)
                loss = -torch.mean(log_probs, -1) * (reward - reward_baseline)

                loss = loss.mean()
                loss.backward()
                self.optim.step()

                running_loss += loss.item()
                running_reward += reward.mean().item()
                running_reward_baseline += reward_baseline.mean().item()
                pbar.set_postfix(loss=running_loss / (it + 1), reward=running_reward / (it + 1),
                                reward_baseline=running_reward_baseline / (it + 1))
                pbar.update()

    def lambda_lr(self, step):
        warm_up = config.warmup
        step += 1
        return (self.model.d_model ** -.5) * min(step ** -.5, step * warm_up ** -1.5)

    def load_checkpoint(self, fname) -> dict:
        if not os.path.exists(fname):
            return None

        checkpoint = torch.load(fname)

        torch.set_rng_state(checkpoint['torch_rng_state'])
        torch.cuda.set_rng_state(checkpoint['cuda_rng_state'])
        np.random.set_state(checkpoint['numpy_rng_state'])
        random.setstate(checkpoint['random_rng_state'])

        self.model.load_state_dict(checkpoint['state_dict'], strict=False)
        self.optim.load_state_dict(checkpoint['optimizer'])
        self.scheduler.load_state_dict(checkpoint['scheduler'])

        print(f"resuming from epoch {checkpoint['epoch']} - validation loss {checkpoint['val_loss']} - best cider on val {checkpoint['best_val_cider']} - best cider on test {checkpoint['best_test_cider']}")

        return {
            "use_rl": checkpoint['use_rl'],
            "best_val_cider": checkpoint['best_val_cider'],
            "best_test_cider": checkpoint['best_test_cider'],
            "patience": checkpoint['patience']
        }

    def save_checkpoint(self, dict_for_updating: dict) -> None:
        dict_for_saving = {
            'torch_rng_state': torch.get_rng_state(),
            'cuda_rng_state': torch.cuda.get_rng_state(),
            'numpy_rng_state': np.random.get_state(),
            'random_rng_state': random.getstate(),
            'epoch': self.epoch,
            'state_dict': self.model.state_dict(),
            'optimizer': self.optim.state_dict(),
            'scheduler': self.scheduler.state_dict()
        }

        for key, value in dict_for_updating.items():
            dict_for_saving[key] = value

    def train(self, checkpoint_filename: str = None):
        while True:
            if checkpoint_filename is not None:
                checkpoint = self.load_checkpoint(checkpoint_filename)
                use_rl = checkpoint["use_rl"]
                best_val_cider = checkpoint["best_val_cider"]
                best_test_cider = checkpoint["best_test_cider"]
                patience = checkpoint["patience"]
            else:
                use_rl = False
                best_val_cider = .0
                best_test_cider = .0
                patience = 0

            if not use_rl:
                self.train_xe()
            else:
                self.train_scst()

            val_loss = self.evaluate_loss(self.val_dataloader)

            # val scores
            scores = self.evaluate_metrics(self.val_dict_dataloader)
            print("Validation scores", scores)
            val_cider = scores['CIDEr']

            # Prepare for next epoch
            best = False
            if val_cider >= best_val_cider:
                best_val_cider = val_cider
                patience = 0
                best = True
            else:
                patience += 1

            switch_to_rl = False
            exit_train = False

            if patience == 5:
                if not use_rl:
                    use_rl = True
                    switch_to_rl = True
                    patience = 0
                    self.optim = Adam(self.model.parameters(), lr=5e-6)
                    print("Switching to RL")
                else:
                    print('patience reached.')
                    exit_train = True

            if switch_to_rl and not best:
                checkpoint = self.load_checkpoint(os.path.join(config.checkpoint_path, config.model_name, "best_val_model.pth"))
                print('Resuming from epoch %d, validation loss %f, best_val_cider %f, and best test_cider %f' % (
                    checkpoint['epoch'], checkpoint['val_loss'], checkpoint['best_val_cider'], checkpoint['best_test_cider']))

            torch.save({
                'val_loss': val_loss,
                'val_cider': val_cider,
                'patience': patience,
                'best_val_cider': best_val_cider,
                'best_test_cider': best_test_cider,
                'use_rl': use_rl,
            }, os.path.join(config.checkpoint_path, config.model_name, "last_model.pth"))

            if best:
                copyfile(os.path.join(config.checkpoint_path, config.model_name, "last_model.pth"), os.path.join(config.checkpoint_path, config.model_name, "best_val_model.pth"))

            if exit_train:
                break

            print("+"*10)

    def get_predictions(self, dataset: DictionaryDataset):
        self.model.eval()
        results = []
        with tqdm(desc='Evaluating: ', unit='it', total=len(dataset)) as pbar:
            for it, sample in enumerate(dataset):
                image_id = sample["image_id"]
                filename = sample["filename"]
                features = sample["features"]
                boxes = sample["boxes"]
                caps_gt = sample["captions"]
                with torch.no_grad():
                    out, _ = self.model.beam_search(features, boxes=boxes, max_len=self.vocab.max_caption_length, eos_idx=self.vocab.eos_idx, 
                                                beam_size=config.beam_size, out_size=1)
                caps_gen = self.vocab.decode_caption(out, join_words=False)
                gens = []
                gts = []
                for i, (gts_i, gen_i) in enumerate(zip(caps_gt, caps_gen)):
                    gen_i = ' '.join([key for key, group in itertools.groupby(gen_i)])
                    gens.append(gen_i)
                    gts.append(gts_i)

                results.append({
                    "image_id": image_id,
                    "filename": filename,
                    "gen": gens,
                    "gts": gts
                })
                pbar.update()

        return results

    def convert_results(self, sample_submisison_json, results, split="public"):
        sample_json_data = json.load(open(sample_submisison_json))
        for sample_item in tqdm(sample_json_data, desc="Converting results: "):
            for item in results:
                if sample_item["id"] == item["filename"]:
                    sample_item["captions"] = item["gen"][0]

        json.dump(sample_json_data, open(os.path.join(config.checkpoint_path, config.model_name, f"{split}_results.json"), "w+"), ensure_ascii=False)