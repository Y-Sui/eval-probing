from typing import List
import subprocess
import itertools
from argparse import ArgumentParser
from pathlib import Path
import torch
from experiments.exp_def import (
    Experiment,
    LingualSetting,
    TaskDefs
)

def probe_heads(setting: LingualSetting,
                finetuned_task: Experiment,
                task: Experiment,
                models_per_gpu: int = 2,
                devices: List = list(range(torch.cuda.device_count()))):
    """
    Probe heads for a model.

    Args:
    setting: cross, multi, or base, which finetuned model to probe. If base, just pretrained BERT.
    finetuned_task: the task that the model was finetuned on.
    task: the task to probe heads on.
    models_per_gpu: how many models should each gpu process?
    devices: devices to use.
    """
    # where all the data and task_def are stored.
    task_root = Path(f'experiments/{task.name}')

    # programmatically get n_classes for task
    task_def_path = task_root.joinpath('task_def.yaml')
    task_def = TaskDefs(task_def_path).get_task_def(task.name.lower())
    n_classes = task_def.n_class
    checkpoint_dir = Path(f'checkpoint/head_probing/{finetuned_task.name}').joinpath(task.name) # where the probed checkpoints will be

    # only probe heads that we haven't already probed.
    heads_to_probe = []
    for hl, hi in itertools.product(range(12), repeat=2):
        dir_for_head = checkpoint_dir.joinpath(setting.name.lower(), str(hl), str(hi))
        if len(list(dir_for_head.rglob('model_1_*.pt'))) == 0:
            heads_to_probe.append((hl, hi))
    
    if len(heads_to_probe) == 0:
        return
    
    # distribute heads to probe to different gpus.
    device_ids = []
    for i, _ in enumerate(heads_to_probe):
        device_ids.append(devices[i % len(devices)])
    
    print('heads to probe:')
    for i, hp in enumerate(heads_to_probe):
        print(setting.name.lower(), hp, f'GPU: {device_ids[i]}')
    print("\n")

    # Run commands in parallel
    processes = []
    for i, (hl, hi) in enumerate(heads_to_probe):
        did = device_ids[i]
        checkpoint_dir_for_head = checkpoint_dir.joinpath(setting.name.lower(), str(hl), str(hi))
        checkpoint_dir_for_head.mkdir(parents=True, exist_ok=True)

        template = f'python train.py --local_rank -1 '
        template += f'--dataset_name {task.name}/cross ' # always train head probes using cross-ling setting
        
        if setting is not LingualSetting.BASE:
            finetuned_checkpoint_dir = Path(f'checkpoint/{finetuned_task.name}_{setting.name.lower()}')
            finetuned_checkpoint = list(finetuned_checkpoint_dir.rglob('model_5*.pt'))[0]
            template += f"--resume --model_ckpt {finetuned_checkpoint} "
        
        template += f"--epochs 2 --output_dir {checkpoint_dir_for_head} "
        template += f"--init_checkpoint bert-base-multilingual-cased --devices {did} "
        template += f'--head_probe --head_probe_layer {hl} --head_probe_idx {hi} --head_probe_n_classes {n_classes}'

        print(f'[{setting.name}] [GPU {did}] {finetuned_task.name}->{task.name} {(hl, hi)}')
        process = subprocess.Popen(template, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        processes.append((hl, hi, did, process))

        # wait if filled
        if len(processes) == len(devices) * models_per_gpu:
            results = [p[-1].communicate() for p in processes]
            for ri, r in enumerate(results):
                prefix_str = f'[{setting.name}] [GPU {processes[ri][2]}] {(processes[ri][:2])}:'

                if (r[1] is not None) and (processes[ri][-1].returncode == 1):
                    errors = r[1].decode('utf-8').split('\n')
                    print(prefix_str)
                    for e in errors:
                        print(f'\t{e}')
                    raise ValueError
                else:
                    print(f'{prefix_str} completed')
            processes = []

    # Collect statuses
    results = [p[-1].communicate() for p in processes]

def probe_model(finetuned_setting: LingualSetting,
                finetuned_task: Experiment,
                downstream_setting: LingualSetting,
                downstream_task: Experiment,
                devices: List[int]):
    task_info_str = f'{finetuned_task.name}_{setting.name.lower()} -> {downstream_task.name}, {downstream_setting.name.lower()}'
    devices = ' '.join([str(d) for d in devices])

    # where all the data and task_def are stored.
    task_root = Path(f'experiments/{downstream_task.name}')

    # programmatically get n_classes for task
    task_def_path = task_root.joinpath('task_def.yaml')
    task_def = TaskDefs(task_def_path).get_task_def(downstream_task.name.lower())
    n_classes = task_def.n_class

    if finetuned_setting is LingualSetting.BASE:
        checkpoint_dir = Path('checkpoint/full_model_probe').joinpath(
            f'{downstream_setting.name.lower()}_head_training',
            'mBERT',
            downstream_task.name)
    else:
        checkpoint_dir = Path('checkpoint/full_model_probe').joinpath(
            f'{downstream_setting.name.lower()}_head_training',
            finetuned_task.name,
            finetuned_setting.name.lower(),
            downstream_task.name)

    if checkpoint_dir.is_dir():
        print(f'{checkpoint_dir} exists, skipping {task_info_str}')
        return

    template = f'python train.py --local_rank -1 '
    template += f'--dataset_name {downstream_task.name}/{downstream_setting.name.lower()} '
    
    if setting is not LingualSetting.BASE:
        finetuned_checkpoint_dir = Path(f'checkpoint/{finetuned_task.name}_{finetuned_setting.name.lower()}')
        finetuned_checkpoint = list(finetuned_checkpoint_dir.rglob('model_5*.pt'))[0]
        template += f"--resume --model_ckpt {finetuned_checkpoint} "
    
    template += f"--epochs 2 --output_dir {checkpoint_dir} "
    template += f"--init_checkpoint bert-base-multilingual-cased --devices {devices} "
    template += f'--model_probe --model_probe_n_classes {n_classes}' # layer 12, head 0 is final CLS

    print(task_info_str)
    process = subprocess.Popen(template, shell=True)
    results = process.communicate()

if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('--parse_mode', type=str, default='model')
    parser.add_argument('--downstream_task', type=str, default='')
    parser.add_argument('--downstream_setting', type=str, default='multi')
    parser.add_argument('--finetuned_task', type=str, default='NLI')
    parser.add_argument('--finetuned_setting', type=str, default='')
    parser.add_argument('--devices', nargs='+')
    parser.add_argument('--models_per_gpu', type=int, default=1)
    args = parser.parse_args()

    if args.devices is not None:
        devices = [int(d) for d in args.devices]
    else:
        devices = list(range(torch.cuda.device_count()))

    if args.downstream_task != '':
        downstream_tasks = [Experiment[args.downstream_task.upper()]]
    else:
        downstream_tasks = list(Experiment)
        downstream_tasks.remove(Experiment.NLI)
    
    if args.finetuned_setting == '':
        finetuned_settings = list(LingualSetting)
    else:
        finetuned_settings = [LingualSetting[args.finetuned_setting.upper()]]
    
    for downstream_task in downstream_tasks:
        for setting in finetuned_settings:            
            if args.parse_mode == 'heads':
                probe_heads(
                    setting=setting,            
                    finetuned_task=Experiment[args.finetuned_task.upper()],
                    task=downstream_task,
                    devices=devices,
                    models_per_gpu=args.models_per_gpu
                )
            elif args.parse_mode == 'model':
                probe_model(
                    finetuned_setting=setting,
                    finetuned_task=Experiment[args.finetuned_task.upper()], # {finetuned_task}_{setting}
                    downstream_setting=LingualSetting[args.downstream_setting.upper()], # trained on downstream task's downstream_task_setting dataset
                    downstream_task=downstream_task,
                    devices=devices
                )
