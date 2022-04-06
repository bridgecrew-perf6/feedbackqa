#!/usr/bin/env python
import sys
import os
from pathlib import Path
this_path = os.getcwd()
sys.path.append('/'.join(this_path.split('/')[:4]))
sys.path.append('/'.join(this_path.split('/')[:3]))
sys.path.insert(2, str(Path(__file__).resolve().parents[1]))
import argparse
import logging
import pickle
import random
from logging.handlers import WatchedFileHandler
from scipy.sparse import issparse ## stupud bug in computecanada get's solved using this

import numpy as np
import pytorch_lightning as pl
import json
import torch
import yaml
from pytorch_lightning import loggers
from pytorch_lightning.callbacks import ModelCheckpoint, EarlyStopping
from pytorch_lightning.loggers import WandbLogger
from transformers import AutoTokenizer
from yaml import load
from parlai.core.params import ParlaiParser
from parlai.scripts.script import ParlaiScript
from parlai.scripts.build_dict import setup_args as setup_dict_args
from parlai.core.dict import DictionaryAgent

from feedbackQA.reason_ce import get_data_loaders as get_reason_data_loaders
from feedbackQA.data.data_loader import generate_dataloader,generate_dataloader_multi_files
from feedbackQA.data.predict_rerank import generate_embeddings, PredictorWithOutlierDetector
from feedbackQA.data.predict_rerank import Predictor as RerankPredictor
from feedbackQA.data.predict_qa import Predictor
from feedbackQA.data.reason_dataloader import encode_sentence_pair
from feedbackQA.models.cache_manager import CacheManagerCallback
from feedbackQA.models.load_model import load_model
from feedbackQA.models.pl_model_loader import try_to_restore_model_weights
from feedbackQA.models.retriever_trainer import RetrieverTrainer
from feedbackQA.models.reasoning_ce_trainer import ReasonTrainer
#from feedbackQA.models.reasoning_t5_trainer import ReasonTrainer as T5ReasonTrainer
#from feedbackQA.models.reasoning_mtl_trainer import ReasonTrainer as T5ReasonTrainer
from feedbackQA.models.rating_ce_trainer import RatingTrainer
from feedbackQA.models.sklearn_outliers_model import SKLEARN_MODEL_FILE_NAME
from feedbackQA.utils.hp_utils import check_and_log_hp
from feedbackQA.utils.logging_utils import LoggerWriter
from feedbackQA.scripts_prak.tokenizer_cutoff import evaluate_tokenizer_cutoff

from transformers import BartTokenizerFast
logger = logging.getLogger(__name__)


def setup_parlai_args(parser=None) -> ParlaiParser:
    parser = ParlaiParser(False, True, 'Train a model')

    train = parser.add_argument_group('Training Loop Arguments')
    train.add_argument(
        '-lfc',
        '--load-from-checkpoint',
        type='bool',
        default=False,
        hidden=True,
        help='load model from checkpoint if available',
    )
    parser = setup_dict_args(parser)
    return parser

class OptGeneration(ParlaiScript):
    @classmethod
    def setup_args(cls):
        return setup_parlai_args()

    def run(self):
        return self.opt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        help="config file with generic hyper-parameters,  such as optimizer, "
        "batch_size, ... -  in yaml format",
        required=True,
    )
    parser.add_argument(
        "--reason_config",
        help="config file with generic hyper-parameters,  such as optimizer, "
        "batch_size, ... -  in yaml format",
        required=True,
    )
    parser.add_argument(
        "--gpu",
        help="gpu id to use. default is cpu. example: --gpu 0",
        type=str,
    )
    parser.add_argument(
        "--validation-interval",
        help="how often to run validation in one epoch - "
        "e.g., 0.5 means halfway - default 0.5",
        type=float,
        default=0.5,
    )
    parser.add_argument("--output", help="where to store models", required=True)
    parser.add_argument("--reason_output", help="where to store models")
    parser.add_argument(
        "--no-model-restoring",
        help="will not restore any previous model weights (even if present)",
        action="store_true",
    )
    parser.add_argument(
        "--no-reason-model-restoring",
        help="will not restore any previous model weights (even if present)",
        action="store_true",
    )    
    parser.add_argument(
        "--train",
        help="will train a model",
        action="store_true",
    )
    parser.add_argument(
        "--test",
        help="will test a model",
        action="store_true",
    )
    parser.add_argument(
        "--predict", help="will predict on the json file you provide as an arg"
    )
    parser.add_argument(
        "--use-original-model-parameters",
        help="to use with predict/generate embeddings - it will not load the fine-tuned parameters "
             "(only the pre-trained ones)",
        action="store_true"
    )
    parser.add_argument(
        "--use-original-reason-model-parameters",
        action="store_true"
    )
    parser.add_argument(
        "--write-fix-report", help="will also generate a json useful to help with annotation fix",
        action="store_true"
    )
    parser.add_argument(
        "--predict-outliers", help="will use the sklearn model to predict outliers "
                                   "(only during --predict)",
        action="store_true"
    )
    parser.add_argument(
        "--file-to-emb", help="will use this file as input to generate embeddings"
    )
    parser.add_argument(
        "--write-emb-to", help="will write question embeddings to this file"
    )
    parser.add_argument("--feedback_output_file", type=str)
    parser.add_argument(
        "--save-weights-to",
        help="will save ONLY the model weights (not the pytorch lightning object)"
        " to this file",
    )
    parser.add_argument("--predict-to", help="will write predictions here)")
    parser.add_argument(
        "--redirect-log",
        help="will intercept any stdout/err and log it",
        action="store_true",
    )
    parser.add_argument(
        "--print-sentence-stats",
        help="will print stats on the data",
        action="store_true",
    )
    parser.add_argument(
        "--multiple-thresholds",
        help="will print results for various thresholds (only when doing --predict)",
        action="store_true",
    )
    parser.add_argument('--rate_only', action="store_true")
    parser.add_argument('--log', help='log to this file (in addition to stdout/err)')
    parser.add_argument("--debug", help="will log more info", action="store_true")
    parser.add_argument("--do_rerank", help="will log more info", action="store_true")
    parser.add_argument("--alpha", type=float, default=0.5)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    # will log to a file if provided (useful for orion on cluster)
    if args.log is not None:
        handler = WatchedFileHandler(args.log)
        formatter = logging.Formatter(logging.BASIC_FORMAT)
        handler.setFormatter(formatter)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
    if args.redirect_log or args.log:
        sys.stdout = LoggerWriter(logger.info)
        sys.stderr = LoggerWriter(logger.warning)

    with open(args.config, "r") as stream:
        hyper_params = load(stream, Loader=yaml.FullLoader)

    with open(args.reason_config, "r") as stream:
        hyper_params_reason = load(stream, Loader=yaml.FullLoader)
    
    if hyper_params['use_parlai']:
        parlai_optgen = OptGeneration(None)
        parlai_opt = parlai_optgen._run_kwargs(hyper_params['parlai'])
        hyper_params['opt'] = parlai_opt
        parlai_dict = DictionaryAgent(parlai_opt)
        hyper_params['parlai_dict'] = parlai_dict
        hyper_params['parlai_null_idx'] = parlai_dict[parlai_dict.null_token]
    
    if args.gpu is not None:
        # PyTorch Lightning wants a list
        gpu = (args.gpu).split(',')
        gpu = [int(item) for item in gpu]
    else:
        gpu = None
    logger.info('using GPU {}'.format(gpu))

    ckpt_to_resume, ret_trainee, trainer, tokenizer = init_model(
        hyper_params,
        0,
        args.output,
        args.validation_interval,
        gpu,
        args.no_model_restoring,
        args.debug,
        args.print_sentence_stats
    )
    
    reason_ckpt_to_resume, reason_trainee, reason_trainer, reason_tokenizer = init_reason_model(
        hyper_params_reason,
        0,
        args.reason_output,
        args.validation_interval,
        gpu,
        args.no_reason_model_restoring,
        args.debug,
        args.print_sentence_stats,
        rate_only=args.rate_only
    )
    
    if args.train:
        raise NotImplementedError()
    elif args.test:
        if not args.use_original_model_parameters:
            model_ckpt = torch.load(ckpt_to_resume, map_location=torch.device("cpu"))
            ret_trainee.load_state_dict(model_ckpt["state_dict"])
        else:
            logger.warning("will NOT load the model parameters - just use the pre-trained model")
        trainer.test(ret_trainee)
    elif args.predict:
        if not args.predict_to:
            raise ValueError('--predict also requires --predict-to')

        if not args.use_original_model_parameters:
            model_ckpt = torch.load(ckpt_to_resume, map_location=torch.device("cpu"))
            ret_trainee.load_state_dict(model_ckpt["state_dict"])
        else:
            logger.warning("will NOT load the model parameters - just use the pre-trained model")

        if not args.use_original_reason_model_parameters:
            model_ckpt = torch.load(reason_ckpt_to_resume, map_location=torch.device("cpu"))
            reason_trainee.load_state_dict(model_ckpt["state_dict"])
        else:
            logger.warning("will NOT load the REASON model parameters - just use the pre-trained model")

        if args.predict_outliers:
            with open(os.path.join(args.output, SKLEARN_MODEL_FILE_NAME), 'rb') as file:
                sklearn_model = pickle.load(file)
            predictor = PredictorWithOutlierDetector(ret_trainee, sklearn_model)
        else:
            if args.do_rerank:
                reason_trainee.to('cuda')
                rerank_pair_encode = encode_sentence_pair
                predictor = RerankPredictor(
                    ret_trainee, reason_trainee, tokenizer, reason_tokenizer, rerank_pair_encode, topk=5, alpha=args.alpha)
            else:
                assert False
                predictor = Predictor(ret_trainee)

        source2pred, source2label, source2oppo, questions = predictor.generate_predictions(
            json_file=args.predict,
            predict_to=args.predict_to,
            multiple_thresholds=args.multiple_thresholds,
            write_fix_report=args.write_fix_report
        )
        
        with open(args.predict_to, 'w+', encoding='utf-8') as f:
            for src, pred in source2pred.items():
                label = np.array(source2label[src])
                for pp, ll, qq in zip(pred, label, questions):
                    f.write('{0}\t{1}\t{2}\n'.format(qq, pp, ll))
                pred = np.array(pred)
                acc = (pred==label).sum() * 1. / len(pred)
                src = args.predict.split('/')[-2] + ':\t' + args.predict.split('/')[-1]
                logger.info('the accuracy of domain {0} is:\t{1}'.format(src, acc))
                logger.info('Upperbound of reranker is {}'.format(source2oppo[src]))
    elif args.file_to_emb:
        raise NotImplementedError()
        if args.write_emb_to is None:
            raise ValueError('please specify also --write-emb-to')

        if not args.use_original_model_parameters:
            model_ckpt = torch.load(ckpt_to_resume, map_location=torch.device("cpu"))
            ret_trainee.load_state_dict(model_ckpt["state_dict"])
        else:
            logger.warning("will NOT load the model parameters - just use the pre-trained model")

        _ = generate_embeddings(
            ret_trainee,
            input_file=args.file_to_emb,
            out_file=args.write_emb_to
        )
    elif args.save_weights_to is not None:
        torch.save(ret_trainee.retriever.state_dict(), args.save_weights_to)
    else:
        logger.warning("please select one between --train / --predict / --file-to-emb")

def init_model(
    hyper_params,
    num_workers,
    output,
    validation_interval,
    gpu,
    no_model_restoring,
    debug,
    print_sentence_stats
):

    check_and_log_hp(
        [
            "train_file",
            "dev_files",
            "test_file",
            "batch_size",
            "valid_batch_size",
            "tokenizer_name",
            "model",
            "max_question_len",
            "max_paragraph_len",
            "patience",
            "gradient_clipping",
            "max_epochs",
            "loss_type",
            "optimizer",
            "precision",
            "accumulate_grad_batches",
            "seed",
            "logging",
            "keep_ood"
        ],
        hyper_params,
    )

    if hyper_params["seed"] is not None:
        # fix the seed
        torch.manual_seed(hyper_params["seed"])
        np.random.seed(hyper_params["seed"])
        random.seed(hyper_params["seed"])
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    tokenizer_name = hyper_params["tokenizer_name"]
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
    ret = load_model(hyper_params, tokenizer, debug)

    os.makedirs(output, exist_ok=True)
    checkpoint_callback = ModelCheckpoint(
        filepath=os.path.join(output, "{epoch}-{val_acc_0:.2f}-{val_loss_0:.2f}"),
        save_top_k=1,
        verbose=True,
        monitor="val_acc_0",
        mode="max",
        period=0,
    )
    early_stopping = EarlyStopping(
        "val_acc_0", mode="max", patience=hyper_params["patience"]
    )

    if (
        hyper_params["model"].get("name") == "bert_encoder"
        and hyper_params["model"].get("cache_size", 0) > 0
    ):
        cbs = [CacheManagerCallback(ret, output)]
    else:
        cbs = []

    if hyper_params["precision"] not in {16, 32}:
        raise ValueError("precision should be either 16 or 32")
    if not no_model_restoring:
        ckpt_to_resume = try_to_restore_model_weights(output)
    else:
        ckpt_to_resume = None
        logger.info(
            "will not try to restore previous models because --no-model-restoring"
        )
    if hyper_params["logging"]["logger"] == "tensorboard":
        pl_logger = loggers.TensorBoardLogger("experiment_logs")
        for hparam in list(hyper_params):
            pl_logger.experiment.add_text(hparam, str(hyper_params[hparam]))
    elif hyper_params["logging"]["logger"] == "wandb":
        orion_trial_id = os.environ.get('ORION_TRIAL_ID')
        name = orion_trial_id if orion_trial_id else hyper_params["logging"]["name"]
        pl_logger = WandbLogger(
            name=name,
            project=hyper_params["logging"]["project"],
            group=hyper_params["logging"]["group"],
            entity=hyper_params["logging"]["entity"],
        )
        pl_logger.log_hyperparams(hyper_params)
    else:
        raise ValueError(
            logger.info(
                "logger {} is not implemnted".format(hyper_params["logging"]["logger"])
            )
        )

    trainer = pl.Trainer(
        logger=pl_logger,
        gpus=gpu,
        distributed_backend=hyper_params["distributed_backend"],
        val_check_interval=validation_interval,
        min_epochs=1,
        gradient_clip_val=hyper_params["gradient_clipping"],
        checkpoint_callback=checkpoint_callback,
        early_stop_callback=early_stopping,
        callbacks=cbs,
        precision=hyper_params["precision"],
        resume_from_checkpoint=ckpt_to_resume,
        accumulate_grad_batches=hyper_params["accumulate_grad_batches"],
        max_epochs=hyper_params["max_epochs"],
        replace_sampler_ddp=False if hyper_params["distributed_backend"] == "ddp" else True,
    )
    
    n_gpus  = len(gpu) if gpu is not None else 1
    dev_dataloaders, test_dataloader, train_dataloader = get_data_loaders(
        hyper_params, num_workers, tokenizer, n_gpus
    )

    if print_sentence_stats:
        evaluate_tokenizer_cutoff(
            hyper_params["train_file"],
            tokenizer,
            hyper_params["max_question_len"],
            hyper_params["max_paragraph_len"],
        )

    ret_trainee = RetrieverTrainer(
        ret,
        train_dataloader,
        dev_dataloaders,
        test_dataloader,
        hyper_params["loss_type"],
        hyper_params["optimizer"],
        hyper_params["train_source"],
        hyper_params["eval_source"],
        is_distributed = True if hyper_params["distributed_backend"] == "ddp" else False
    )
    return ckpt_to_resume, ret_trainee, trainer, tokenizer

def _init_model(
    hyper_params,
    num_workers,
    output,
    validation_interval,
    gpu,
    no_model_restoring,
    debug,
    print_sentence_stats
):

    check_and_log_hp(
        [
            "train_file",
            "dev_files",
            "test_file",
            "batch_size",
            "valid_batch_size",
            "tokenizer_name",
            "model",
            "max_question_len",
            "max_paragraph_len",
            "patience",
            "gradient_clipping",
            "max_epochs",
            "loss_type",
            "optimizer",
            "precision",
            "accumulate_grad_batches",
            "seed",
            "logging",
            "keep_ood"
        ],
        hyper_params,
    )

    if hyper_params["seed"] is not None:
        # fix the seed
        torch.manual_seed(hyper_params["seed"])
        np.random.seed(hyper_params["seed"])
        random.seed(hyper_params["seed"])
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    tokenizer_name = hyper_params["tokenizer_name"]
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
    ret = load_model(hyper_params, tokenizer, debug)

    os.makedirs(output, exist_ok=True)
    checkpoint_callback = ModelCheckpoint(
        filepath=os.path.join(output, "{epoch}-{val_acc_0:.2f}-{val_loss_0:.2f}"),
        save_top_k=1,
        verbose=True,
        monitor="val_acc_0",
        mode="max",
        period=0,
    )
    early_stopping = EarlyStopping(
        "val_acc_0", mode="max", patience=hyper_params["patience"]
    )

    if (
        hyper_params["model"].get("name") == "bert_encoder"
        and hyper_params["model"].get("cache_size", 0) > 0
    ):
        cbs = [CacheManagerCallback(ret, output)]
    else:
        cbs = []

    if hyper_params["precision"] not in {16, 32}:
        raise ValueError("precision should be either 16 or 32")
    if not no_model_restoring:
        ckpt_to_resume = try_to_restore_model_weights(output)
    else:
        ckpt_to_resume = None
        logger.info(
            "will not try to restore previous models because --no-model-restoring"
        )
    if hyper_params["logging"]["logger"] == "tensorboard":
        pl_logger = loggers.TensorBoardLogger("experiment_logs")
        for hparam in list(hyper_params):
            pl_logger.experiment.add_text(hparam, str(hyper_params[hparam]))
    elif hyper_params["logging"]["logger"] == "wandb":
        orion_trial_id = os.environ.get('ORION_TRIAL_ID')
        name = orion_trial_id if orion_trial_id else hyper_params["logging"]["name"]
        pl_logger = WandbLogger(
            name=name,
            project=hyper_params["logging"]["project"],
            group=hyper_params["logging"]["group"],
            entity=hyper_params["logging"]["entity"],
            offline=True
        )
        pl_logger.log_hyperparams(hyper_params)
    else:
        raise ValueError(
            logger.info(
                "logger {} is not implemnted".format(hyper_params["logging"]["logger"])
            )
        )

    trainer = pl.Trainer(
        logger=pl_logger,
        gpus=gpu,
        distributed_backend=hyper_params["distributed_backend"],
        val_check_interval=validation_interval,
        min_epochs=1,
        gradient_clip_val=hyper_params["gradient_clipping"],
        checkpoint_callback=checkpoint_callback,
        early_stop_callback=early_stopping,
        callbacks=cbs,
        precision=hyper_params["precision"],
        resume_from_checkpoint=ckpt_to_resume,
        accumulate_grad_batches=hyper_params["accumulate_grad_batches"],
        max_epochs=hyper_params["max_epochs"],
        replace_sampler_ddp=False if hyper_params["distributed_backend"] == "ddp" else True,
    )
    
    n_gpus  = len(gpu) if gpu is not None else 1
    dev_dataloaders, test_dataloader, train_dataloader = get_data_loaders(
        hyper_params, num_workers, tokenizer, n_gpus
    )

    if print_sentence_stats:
        evaluate_tokenizer_cutoff(
            hyper_params["train_file"],
            tokenizer,
            hyper_params["max_question_len"],
            hyper_params["max_paragraph_len"],
        )

    ret_trainee = RetrieverTrainer(
        ret,
        train_dataloader,
        dev_dataloaders,
        test_dataloader,
        hyper_params["loss_type"],
        hyper_params["optimizer"],
        hyper_params["train_source"],
        hyper_params["eval_source"],
        is_distributed = True if hyper_params["distributed_backend"] == "ddp" else False
    )
    return ckpt_to_resume, ret_trainee, trainer, tokenizer

def init_reason_model(
    hyper_params,
    num_workers,
    output,
    validation_interval,
    gpu,
    no_model_restoring,
    debug,
    print_sentence_stats,
    rate_only=False
):

    check_and_log_hp(
        [
            "train_file",
            "dev_files",
            "batch_size",
            "valid_batch_size",
            "tokenizer_name",
            "model",
            "max_question_len",
            "max_paragraph_len",
            "patience",
            "gradient_clipping",
            "max_epochs",
            "loss_type",
            "optimizer",
            "precision",
            "accumulate_grad_batches",
            "seed",
            "logging",
            "keep_ood"
        ],
        hyper_params,
    )

    if hyper_params["seed"] is not None:
        # fix the seed
        torch.manual_seed(hyper_params["seed"])
        np.random.seed(hyper_params["seed"])
        random.seed(hyper_params["seed"])
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    tokenizer_name = hyper_params["tokenizer_name"]
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
    ret = load_model(hyper_params, tokenizer, debug)
    ret.bert_paragraph_encoder = None
    os.makedirs(output, exist_ok=True)
    checkpoint_callback = ModelCheckpoint(
        filepath=os.path.join(output, "{epoch}-{val_acc_0:.2f}-{val_loss_0:.2f}"),
        save_top_k=1,
        verbose=True,
        monitor="val_acc_0",
        mode="max",
        period=0,
    )
    early_stopping = EarlyStopping(
        "val_acc_0", mode="max", patience=hyper_params["patience"]
    )

    if (
        hyper_params["model"].get("name") == "bert_encoder"
        and hyper_params["model"].get("cache_size", 0) > 0
    ):
        cbs = [CacheManagerCallback(ret, output)]
    else:
        cbs = []

    if hyper_params["precision"] not in {16, 32}:
        raise ValueError("precision should be either 16 or 32")
    if not no_model_restoring:
        ckpt_to_resume = try_to_restore_model_weights(output)
    else:
        ckpt_to_resume = None
        logger.info(
            "will not try to restore previous models because --no-model-restoring"
        )


    if hyper_params["logging"]["logger"] == "tensorboard":
        pl_logger = loggers.TensorBoardLogger("experiment_logs")
        for hparam in list(hyper_params):
            pl_logger.experiment.add_text(hparam, str(hyper_params[hparam]))
    elif hyper_params["logging"]["logger"] == "wandb":
        orion_trial_id = os.environ.get('ORION_TRIAL_ID')
        name = orion_trial_id if orion_trial_id else hyper_params["logging"]["name"]
        pl_logger = WandbLogger(
            name=name,
            project=hyper_params["logging"]["project"],
            group=hyper_params["logging"]["group"],
            entity=hyper_params["logging"]["entity"],
        )
        #pl_logger.log_hyperparams(hyper_params)
    else:
        raise ValueError(
            logger.info(
                "logger {} is not implemnted".format(hyper_params["logging"]["logger"])
            )
        )
    trainer = pl.Trainer(
        logger=pl_logger,
        gpus=gpu,
        distributed_backend=hyper_params["distributed_backend"],
        val_check_interval=validation_interval,
        min_epochs=1,
        gradient_clip_val=hyper_params["gradient_clipping"],
        checkpoint_callback=checkpoint_callback,
        early_stop_callback=early_stopping,
        callbacks=cbs,
        precision=hyper_params["precision"],
        resume_from_checkpoint=ckpt_to_resume,
        accumulate_grad_batches=hyper_params["accumulate_grad_batches"],
        max_epochs=hyper_params["max_epochs"],
        replace_sampler_ddp=False if hyper_params["distributed_backend"] == "ddp" else True,
    )
    
    n_gpus  = len(gpu) if gpu is not None else 1
    dev_dataloaders, train_dataloader, train_sampler = get_reason_data_loaders(
        hyper_params, num_workers, tokenizer, n_gpus, fb_for_rate=False
        )

    if print_sentence_stats:
        evaluate_tokenizer_cutoff(
            hyper_params["train_file"],
            tokenizer,
            hyper_params["max_question_len"],
            hyper_params["max_paragraph_len"],
        )
    if rate_only:
        ModelTrainer = RatingTrainer
    else:
        ModelTrainer = ReasonTrainer
    reason_trainee = ModelTrainer(
        ret,
        train_dataloader,
        train_sampler,
        dev_dataloaders,
        hyper_params["loss_type"],
        hyper_params["optimizer"],
        hyper_params["train_source"],
        hyper_params["eval_source"],
        is_distributed = True if hyper_params["distributed_backend"] == "ddp" else False,
        pad_id=tokenizer.pad_token_id,
        tokenizer=tokenizer
    )
    return ckpt_to_resume, reason_trainee, trainer, tokenizer

def get_data_loaders(hyper_params, num_workers, tokenizer, n_gpus):
    if type(hyper_params["train_file"]) == dict :
        train_dataloader = generate_dataloader_multi_files(
            hyper_params["train_file"].values(),
            hyper_params["max_question_len"],
            hyper_params["max_paragraph_len"],
            tokenizer,
            hyper_params["batch_size"],
            n_gpus=n_gpus,
            num_workers=num_workers,
            shuffle=True,
            keep_ood=hyper_params["keep_ood"],
            sampling_ratios=hyper_params.get("sampling_ratios", None)
        )
    else:
        train_dataloader = generate_dataloader(
            hyper_params["train_file"],
            hyper_params["max_question_len"],
            hyper_params["max_paragraph_len"],
            tokenizer,
            hyper_params["batch_size"],
            num_workers=num_workers,
            shuffle=True,
            keep_ood=hyper_params["keep_ood"]
        )
    dev_dataloaders = []
    for dev_file in hyper_params["dev_files"].values():
        dev_dataloaders.append(
            generate_dataloader(
                dev_file,
                hyper_params["max_question_len"],
                hyper_params["max_paragraph_len"],
                tokenizer,
                hyper_params["valid_batch_size"],
                num_workers=num_workers,
                shuffle=False,
                keep_ood=hyper_params["keep_ood"]
            )
        )
    test_dataloader = generate_dataloader(
        hyper_params["test_file"],
        hyper_params["max_question_len"],
        hyper_params["max_paragraph_len"],
        tokenizer,
        hyper_params["valid_batch_size"],
        num_workers=num_workers,
        shuffle=False,
        keep_ood=hyper_params["keep_ood"]
    )
    return dev_dataloaders, test_dataloader, train_dataloader


if __name__ == "__main__":
    main()
