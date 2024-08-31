if __name__ == "__main__":
    import sys
    import os
    import pathlib

    ROOT_DIR = str(pathlib.Path(__file__).parent.parent.parent)
    sys.path.append(ROOT_DIR)
    os.chdir(ROOT_DIR)

import os
import hydra
import torch
from omegaconf import OmegaConf
import pathlib
from torch.utils.data import DataLoader
import copy
import random
import wandb
import numpy as np
from diffusion_policy.workspace.base_workspace import BaseWorkspace
from diffusion_policy.dataset.base_dataset import BaseImageDataset
import argparse, os, sys, datetime, glob, importlib
from omegaconf import OmegaConf
import numpy as np
from PIL import Image
import torch
import torchvision
from torch.utils.data import random_split, DataLoader
import pytorch_lightning as pl
from pytorch_lightning.trainer import Trainer
from pytorch_lightning.utilities import rank_zero_only
from pytorch_lightning.callbacks import ModelCheckpoint, Callback, LearningRateMonitor
import wandb
import copy
from VILP.taming.models.vqgan import VQModel
from VILP.dataset.robomimic_ae_dataset import RobomimicAeDataset
from VILP.workspace.utils import config_to_parser, instantiate_from_config, HumanDemonstrationsWrapper

OmegaConf.register_new_resolver("eval", eval, replace=True)
class TrainVqganWorkspace(BaseWorkspace):
    include_keys = ['global_step', 'epoch']

    def __init__(self, cfg: OmegaConf):
        super().__init__(cfg)

        # set seed
        seed = cfg.seed
        torch.manual_seed(seed)
        np.random.seed(seed)
        random.seed(seed)
        trainer_config = {}
        trainer_config["gpus"] = cfg.gpus
        # configure model

        model = VQModel(**cfg.model)

        self.model = model
        # print model size
        print("Model size: {:.2f}M".format(sum(p.numel() for p in model.parameters()) / 1e6))
        now = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        sys.path.append(os.getcwd())
        parser = argparse.ArgumentParser()
        parser = Trainer.add_argparse_args(parser)
        parser = config_to_parser(cfg.trainer, parser)
        opt, unknown = parser.parse_known_args()

        nowname = now+opt.postfix
        logdir = os.path.join("vq_models", nowname)
        ckptdir = os.path.join(logdir, "checkpoints")
        cfgdir = os.path.join(logdir, "configs")
        configs = {}
        cli = OmegaConf.from_dotlist(unknown)
        config = OmegaConf.merge(*configs, cli)
        lightning_config = config.pop("lightning", OmegaConf.create())
        trainer_opt = argparse.Namespace(**trainer_config)
        lightning_config.trainer = trainer_config
        # trainer and callbacks
        trainer_kwargs = dict()

        # add callback which sets up log directory
        default_callbacks_cfg = {
            "setup_callback": {
                "target": "VILP.workspace.utils.SetupCallback",
                "params": {
                    "resume": opt.resume,
                    "now": now,
                    "logdir": logdir,
                    "ckptdir": ckptdir,
                    "cfgdir": cfgdir,
                    "config": config,
                    "lightning_config": lightning_config,
                }
            },
            "image_logger": {
                "target": "VILP.workspace.utils.ImageLogger",
                "params": {
                    "batch_frequency": 750,
                    "max_images": 4,
                    "clamp": True,
                    "home_dir": logdir
                }
            },
        }

        default_callbacks_cfg["periodic_checkpoint"] = {
            "target": "VILP.workspace.utils.PeriodicCheckpointCallback",
            "params": {
                "ckptdir": ckptdir,
                "save_interval": 1,  
                "save_last_n": 3,   
                "verbose": True,
            }
        }
        
        callbacks_cfg = lightning_config.get('callbacks') or OmegaConf.create()
        callbacks_cfg = OmegaConf.merge(default_callbacks_cfg, callbacks_cfg)
        trainer_kwargs["callbacks"] = [instantiate_from_config(callbacks_cfg[k]) for k in callbacks_cfg]

        trainer = Trainer.from_argparse_args(trainer_opt, **trainer_kwargs)
        self.trainer = trainer
        self.lightning_config = lightning_config
        self.cpktdir = ckptdir
        self.opt = opt
    def run(self):
        cfg = copy.deepcopy(self.cfg)
        lightning_config = self.lightning_config
        opt = self.opt
        human_demonstrations_img: BaseImageDataset = hydra.utils.instantiate(cfg.dataset)
        data = HumanDemonstrationsWrapper(human_demonstrations_img,cfg)
        bs, base_lr = cfg.batch_size, cfg.base_learning_rate
        ngpu = len(lightning_config.trainer.gpus.strip(",").split(','))
        accumulate_grad_batches = lightning_config.trainer.get('accumulate_grad_batches', 1)
        self.model.learning_rate = accumulate_grad_batches * ngpu * bs * base_lr
        print("Setting learning rate to {:.2e} = {} (accumulate_grad_batches) * {} (num_gpus) * {} (batchsize) * {:.2e} (base_lr)".format(
            self.model.learning_rate, accumulate_grad_batches, ngpu, bs, base_lr))

        if opt.train:
            try:
                self.trainer.fit(self.model, data)
            except Exception:
                raise
        if not opt.no_test and not self.trainer.interrupted:
            self.trainer.test(self.model, data)

@hydra.main(
    version_base=None,
    config_path=str(pathlib.Path(__file__).parent.parent.joinpath("config")), 
    config_name=pathlib.Path(__file__).stem)
def main(cfg):
    workspace = TrainVqganWorkspace(cfg)
    workspace.run()

if __name__ == "__main__":
    main()