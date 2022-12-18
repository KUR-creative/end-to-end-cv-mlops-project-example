"""This module implements automatic hyperparameter optimization with Optuna."""

import logging
import random
from pathlib import Path

import numpy as np
import optuna
import torch
import yaml

from image_dataloader import create_dataloaders
from object_detection_model import faster_rcnn_mob_model_for_n_classes
from train_inference_fns import eval_one_epoch, train_one_epoch
from utils import get_config_yml, get_device

# Set partial reproducibility
SEED = 0
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed_all(SEED)

class Objective:
    """The class to be used for a hyperparameter optimization."""

    def __init__(self, train_dl, val_dl, model, hyper_opt_params_conf, 
                 eval_iou_thresh=0.5, eval_beta=1, device=torch.device('cpu')):
        self.train_dl = train_dl
        self.val_dl = val_dl
        self.model = model
        self.hyper_opt_params_conf = hyper_opt_params_conf
        self.eval_iou_thresh = eval_iou_thresh
        self.eval_beta = eval_beta
        self.device = device

    def __call__(self, trial):
            
        trials_suggest = {'cat': trial.suggest_categorical,
                          'int': trial.suggest_int,
                          'float': trial.suggest_float}

        # Get hyperparameters from configurations
        hyperparams= self.hyper_opt_params_conf['hyperparameters']

        # Construct a training optimizer and a lr_scheduler
        train_model_params = [p for p in self.model.parameters() if p.requires_grad]
        optimizer_name = trial.suggest_categorical('optimizer', list(hyperparams['optimizers']))
        optim_params = {k: trials_suggest[v[1]](k, **v[0]) for k, v in hyperparams['optimizers'][optimizer_name].items()}    
        train_model_params = [p for p in self.model.parameters() if p.requires_grad]
        optimizer = getattr(torch.optim, optimizer_name)(train_model_params, **optim_params)
        
        lr_scheduler_name = trial.suggest_categorical('lr_scheduler', list(hyperparams['lr_schedulers']))
        if lr_scheduler_name != 'None':            
            lr_scheduler_params = {k: trials_suggest[v[1]](k, **v[0]) for k, v in hyperparams['lr_schedulers'][lr_scheduler_name].items()}
            lr_scheduler = getattr(torch.optim.lr_scheduler, lr_scheduler_name)(optimizer, **lr_scheduler_params)
        else:
            lr_scheduler = None

        # Train the model
        for epoch in range(1, self.hyper_opt_params_conf['epochs']+1):
            _ = train_one_epoch(self.train_dl, self.model, optimizer, self.device)

            if lr_scheduler is not None:
                lr_scheduler.step()        

            eval_res = eval_one_epoch(self.val_dl, self.model, self.eval_iou_thresh, self.eval_beta, self.device)
            opt_score = eval_res['epoch_scores'][self.hyper_opt_params_conf['metric']]
            trial.report(opt_score, epoch)
            
            # Handle pruning
            if trial.should_prune():
                raise optuna.TrialPruned()

        return opt_score

def save_best_hyper_params(study, hyper_opt_params_conf, save_path):
    """Save the best parameters found during a hyperparameter optimization."""
    save_path.parent.mkdir(exist_ok=True)  
    hp_conf = hyper_opt_params_conf['hyperparameters']  
    best_params = {hyper_opt_params_conf['metric']: round(study.best_value, 2)}
    
    for hp in ['optimizer', 'lr_scheduler']:
        hp_name = study.best_params[hp]
        if hp_name != 'None':
            hps = {}
            for k in hp_conf[hp+'s'][study.best_params[hp]]:
                hps[k] = study.best_params[k]
        else:
            hp_name = None
            hps = None            
        best_params[hp] = {'name': hp_name,
                           'parameters': hps}

    with open(save_path, 'w') as f:
        yaml.safe_dump(best_params, f)

def save_study_plots(study, study_name, save_path):
    """Save study result plots."""
    save_path = Path(save_path) / study_name / 'plots'
    save_path.mkdir(parents=True, exist_ok=True)

    plots = [optuna.visualization.plot_optimization_history,
             optuna.visualization.plot_intermediate_values,
             optuna.visualization.plot_parallel_coordinate,
             optuna.visualization.plot_contour,
             optuna.visualization.plot_slice,
             optuna.visualization.plot_param_importances,
             optuna.visualization.plot_edf]
    
    for plot in plots:
        fig = plot(study)
        fname = plot.__name__[5:]     
        fig.write_image(save_path / f'{fname}.jpg')

def main(project_path, config):
    """Run an optimization study.""" 
    logging.basicConfig(level=logging.INFO, filename='app.log',
                        format="[%(levelname)s]: %(message)s")

    # Get configurations for a hyperparameter optimization
    img_data_paths = config['image_data_paths']
    batch_size = config['image_dataset_conf']['batch_size']
    model_params = config['object_detection_model']['load_parameters']
    num_classes = config['object_detection_model']['number_classes']     
    eval_iou_thresh = config['model_training_inference_conf']['evaluation_iou_threshold']
    eval_beta = config['model_training_inference_conf']['evaluation_beta']
    device = get_device(config['model_training_inference_conf']['device_cuda'])
    HYPER_OPT_PARAMS = config['hyperparameter_optimization']
    sampler = HYPER_OPT_PARAMS['sampler']
    pruner = HYPER_OPT_PARAMS['pruner']
    
    hyper_opt_path = project_path / HYPER_OPT_PARAMS['save_study_dir']
    hyper_opt_path.mkdir(exist_ok=True)

    # Get dataloaders
    imgs_path, train_csv_path, bbox_csv_path = [
        project_path / fpath for fpath in [img_data_paths['images'], 
                                           img_data_paths['train_csv_file'], 
                                           img_data_paths['bboxes_csv_file']]]
    train_dl, val_dl = create_dataloaders(imgs_path, train_csv_path, bbox_csv_path, 
                                          batch_size, train_test_split_data=True)
    
    # Create model
    frcnn_mob_model = faster_rcnn_mob_model_for_n_classes(num_classes, **model_params)
    frcnn_mob_model.to(device)

    # Set study parameters
    study_callbacks = None
    if str(device) == 'cuda':
        study_callbacks = [lambda study, trial: torch.cuda.empty_cache()]
    study_storage = optuna.storages.RDBStorage(url='sqlite:///{}'.format(
                                                   hyper_opt_path / 'hyper_opt_studies.db'))
    sampler_pruner = []
    for osp, sp in zip((optuna.samplers, optuna.pruners), (sampler, pruner)):
        sp_params = sp['parameters'] if sp['parameters'] else {}
        sampler_pruner.append(getattr(osp, sp['name'])(**sp_params) if sp['name'] is not None else None)

    # Run a optimization session
    study = optuna.create_study(direction='maximize', sampler=sampler_pruner[0],
                                pruner=sampler_pruner[1], storage=study_storage, 
                                study_name=HYPER_OPT_PARAMS['study_name'], 
                                load_if_exists=True)

    study.optimize(Objective(train_dl, val_dl, frcnn_mob_model, HYPER_OPT_PARAMS, 
                             eval_iou_thresh, eval_beta, device), 
                   n_trials=HYPER_OPT_PARAMS['n_trials'], timeout=HYPER_OPT_PARAMS['timeout'], 
                   callbacks=study_callbacks, gc_after_trial=True)
    
    save_best_params_path = project_path / HYPER_OPT_PARAMS['save_best_parameters_path']
    save_best_hyper_params(study, HYPER_OPT_PARAMS, save_best_params_path)    
    logging.info("The best parameters are saved.")  
    
    # Save study visualizations
    save_study_plots(study, HYPER_OPT_PARAMS['study_name'], hyper_opt_path)
    logging.info("Plots are saved.")

if __name__ == '__main__':
    project_path = Path.cwd()
    config = get_config_yml()
    main(project_path, config)